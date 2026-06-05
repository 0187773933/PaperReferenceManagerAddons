"""
src/pdf/ocr.py

PER-BBOX OCR. For each YOLO detection of a text-bearing type ,
extract the text and pin it onto that detection.

This module does ONE thing : turn a YOLO detection ( bbox ) into a
text string. It does NOT classify sections , merge paragraphs , dedupe
blocks , or anything downstream of "what text is in this rectangle".
Those concerns live elsewhere and can be derived from this output.

Allowed detection types ( anything else is skipped ) :
    title , plain text , figure_caption ,
    table_caption , table_footnote , formula_caption

Three-stage cascade per detection :
  1) pypdfium2.get_text_bounded() on the bbox ( free , exact when the
     PDF has a real text layer ) ;
  2) If embedded text is empty , too short , garbled ( CID-without-
     ToUnicode ) , or has lost its inter-word spacing , fall back to
     the configured OCR engine ;
  3) The engine runs ONCE per page ( page-level OCR is much faster
     than per-bbox model invocations ) and we filter its returned
     text-lines into each bbox by spatial containment.

Engines :
  rapid     - RapidOCR ( PP-OCRv5 on ONNX Runtime ; default ; ~0.2s/page )
  paddle    - PaddleOCR ( PP-OCRv5 on paddlepaddle ; same models , slower runtime )
  surya     - Surya OCR ( transformer ; best on complex pages ; slow )
  tesseract - the 'tesseract' binary ( last-resort fallback )

Top-level entry point :

  ocr_paper( pdf_path , yolo_data ,
             engine='rapid' , lang='en' ,
             force_ocr=False , force=False ,
             max_pages=None )

  Mutates yolo_data[ 'pages' ] in place. For each allowed detection ,
  sets det[ 'ocr' ][ engine ] = '<extracted text>'. Returns the count
  of detections that received new text.

Output shape per detection :
  {
    'type': 'plain text' , 'class_id': 1 ,
    'bbox': [x1,y1,x2,y2] , 'bbox_area': N , 'confidence': 0.45 ,
    'ocr': { 'rapid': 'extracted text blob...' , ... }    # NEW
  }
"""

import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from PIL import Image

from . import pdf as PDF
from ..utils import utils


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# DPI to render pages at when falling back to OCR. 300 is the tesseract
# sweet spot ; higher just burns CPU for negligible accuracy.
OCR_DPI = 300

# YOLO detection classes we actually OCR. Everything else ( figure ,
# table , abandon page-furniture , isolate_formula , etc. ) is skipped.
# Both 'plain text' ( with space , doclayout-yolo's class label ) and
# 'plain_text' ( underscore , some downstream renames ) are accepted.
TEXT_DETECTION_CLASSES = {
	"title" ,
	"plain text" , "plain_text" ,
	"figure_caption" ,
	"table_caption" , "table_footnote" ,
	"formula_caption" ,
}

# Embedded-text gates : trust the PDF text layer only if it returned
# enough chars AND doesn't look like CID-without-ToUnicode garbage.
MIN_EMBEDDED_TEXT        = 8
MAX_GARBLED_BAD_RATIO    = 0.05
MIN_GARBLED_LETTER_RATIO = 0.20

# Unspaced-text detection : CID extracts and some OCR outputs lose
# inter-word spacing ( 'furthertechnicaladvantageof' ) -- if it looks
# like that , fall through to OCR.
STUCK_WORD_AVG_LEN_THRESHOLD = 12.0
STUCK_WORD_LONG_FRACTION     = 0.15
STUCK_WORD_LONG_LEN          = 18
STUCK_WORD_MIN_LEN           = 9

# Below this many chars , OCR output is almost certainly hallucinated
# from background noise -- discard.
MIN_OCR_TEXT = 4


# ---------------------------------------------------------------------------
# Engine identifiers + per-backend language code mapping
# ---------------------------------------------------------------------------

ENGINE_RAPID     = "rapid"
ENGINE_PADDLE    = "paddle"
ENGINE_SURYA     = "surya"
ENGINE_TESSERACT = "tesseract"
DEFAULT_ENGINE   = ENGINE_RAPID

LANG_ALIASES = {
	"en":  { "rapid": "en"  , "paddle": "en"     , "surya": "en" , "tesseract": "eng"     } ,
	"eng": { "rapid": "en"  , "paddle": "en"     , "surya": "en" , "tesseract": "eng"     } ,
	"zh":  { "rapid": "ch"  , "paddle": "ch"     , "surya": "zh" , "tesseract": "chi_sim" } ,
	"fr":  { "rapid": "fr"  , "paddle": "fr"     , "surya": "fr" , "tesseract": "fra"     } ,
	"de":  { "rapid": "de"  , "paddle": "german" , "surya": "de" , "tesseract": "deu"     } ,
	"es":  { "rapid": "es"  , "paddle": "es"     , "surya": "es" , "tesseract": "spa"     } ,
}

def _lang_for( canonical , backend ):
	row = LANG_ALIASES.get( canonical , LANG_ALIASES[ "en" ] )
	return row[ backend ]


# ---------------------------------------------------------------------------
# Text cleanup : ligatures , control chars , stuck words.
# ---------------------------------------------------------------------------

_LIGATURES = {
	"ﬀ": "ff" , "ﬁ": "fi" , "ﬂ": "fl" ,
	"ﬃ": "ffi" , "ﬄ": "ffl" , "ﬅ": "ft" ,
}
_FANCY_QUOTES = { "‘": "'" , "’": "'" , "“": '"' , "”": '"' }
_SOFT_HYPHEN  = "­"
_NB_HYPHEN    = "‑"


def _normalize_text( text ):
	"""Unicode-normalize , de-ligature , de-fancy-quote , strip control
	chars and collapse excess whitespace. Returns a single string."""
	if not text:
		return ""
	text = unicodedata.normalize( "NFKC" , text )
	for k , v in _LIGATURES.items():
		text = text.replace( k , v )
	for k , v in _FANCY_QUOTES.items():
		text = text.replace( k , v )
	text = text.replace( _SOFT_HYPHEN , "" )
	text = text.replace( _NB_HYPHEN , "-" )
	# Drop control chars except \n / \t.
	text = "".join(
		ch for ch in text
		if ch in "\n\t" or unicodedata.category( ch )[ 0 ] != "C"
	)
	out = []
	for raw in text.splitlines():
		out.append( re.sub( r"[ \t]+" , " " , raw ).strip() )
	# Collapse runs of blank lines.
	collapsed = []
	prev_blank = False
	for ln in out:
		if not ln:
			if prev_blank:
				continue
			prev_blank = True
		else:
			prev_blank = False
		collapsed.append( ln )
	return "\n".join( collapsed ).strip()


def _is_garbled( text ):
	"""True if `text` is dense in private-use Unicode , replacement
	chars , or control chars ( typical CID-without-ToUnicode garbage ) ,
	OR has too few real letters to be readable."""
	if not text:
		return False
	n = len( text )
	bad , letters = 0 , 0
	for ch in text:
		cp = ord( ch )
		if 0xE000 <= cp <= 0xF8FF or cp == 0xFFFD or 0xD800 <= cp <= 0xDFFF:
			bad += 1
			continue
		if cp < 0x20 and ch not in "\t\n\r":
			bad += 1
			continue
		if ch.isalpha():
			letters += 1
	if bad / n > MAX_GARBLED_BAD_RATIO:
		return True
	if letters / n < MIN_GARBLED_LETTER_RATIO:
		return True
	return False


def _looks_unspaced( text ):
	"""True if word boundaries appear to have been collapsed ( CID-
	without-spacing , or scanned-OCR that lost inter-word gaps )."""
	tokens = text.split()
	if len( tokens ) < 4:
		return False
	avg_len = sum( len( t ) for t in tokens ) / len( tokens )
	if avg_len > STUCK_WORD_AVG_LEN_THRESHOLD:
		return True
	long_n = sum( 1 for t in tokens if len( t ) > STUCK_WORD_LONG_LEN )
	if long_n / len( tokens ) > STUCK_WORD_LONG_FRACTION:
		return True
	return False


_WORDNINJA = None
_WORDNINJA_FAILED = False

def _get_wordninja():
	global _WORDNINJA , _WORDNINJA_FAILED
	if _WORDNINJA_FAILED:
		return None
	if _WORDNINJA is not None:
		return _WORDNINJA
	try:
		import wordninja
		_WORDNINJA = wordninja
		return _WORDNINJA
	except Exception:
		_WORDNINJA_FAILED = True
		return None


def _split_stuck_token( token ):
	"""Try to split 'furthertechnicaladvantage' into ['further',
	'technical','advantage']. Guarded so real long words ( CamelCase
	branded names , dictionary words ) survive untouched."""
	if len( token ) < STUCK_WORD_MIN_LEN:
		return [ token ]
	wn = _get_wordninja()
	if wn is None:
		return [ token ]
	prefix , suffix , inner = "" , "" , token
	while inner and not inner[ 0 ].isalpha():
		prefix += inner[ 0 ] ; inner = inner[ 1: ]
	while inner and not inner[ -1 ].isalpha():
		suffix = inner[ -1 ] + suffix ; inner = inner[ :-1 ]
	if len( inner ) < STUCK_WORD_MIN_LEN or not inner.isalpha():
		return [ token ]
	if any( c.isupper() for c in inner[ 1: ] ):
		return [ token ]
	try:
		parts = wn.split( inner.lower() )
	except Exception:
		return [ token ]
	if len( parts ) < 2:
		return [ token ]
	if any( len( p ) <= 1 for p in parts ):
		return [ token ]
	if inner[ 0 ].isupper():
		parts[ 0 ] = parts[ 0 ].capitalize()
	if prefix: parts[ 0 ]  = prefix + parts[ 0 ]
	if suffix: parts[ -1 ] = parts[ -1 ] + suffix
	return parts


def _split_stuck_words( text ):
	"""Apply _split_stuck_token across every whitespace-separated token
	in `text` , join with single spaces. Preserves line breaks."""
	if not text:
		return text
	out_lines = []
	for line in text.split( "\n" ):
		toks = []
		for tok in line.split():
			toks.extend( _split_stuck_token( tok ) )
		out_lines.append( " ".join( toks ) )
	return "\n".join( out_lines )


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _yolo_bbox_to_pdf_rect( bbox , yolo_dpi , page_height_pt ):
	"""Convert a YOLO bbox ( pixel coords , top-left origin ) into a
	pdfium rectangle ( point coords , bottom-left origin )."""
	x1 , y1 , x2 , y2 = bbox
	pt_per_px = 72.0 / yolo_dpi
	left   = x1 * pt_per_px
	right  = x2 * pt_per_px
	top    = page_height_pt - ( y1 * pt_per_px )
	bottom = page_height_pt - ( y2 * pt_per_px )
	return left , bottom , right , top


def _polygon_to_aabb( polygon ):
	"""Convert a 4-point polygon ( [[x,y],[x,y],[x,y],[x,y]] ) to an
	axis-aligned bbox ( x1 , y1 , x2 , y2 )."""
	try:
		xs = [ float( pt[ 0 ] ) for pt in polygon ]
		ys = [ float( pt[ 1 ] ) for pt in polygon ]
		return ( min( xs ) , min( ys ) , max( xs ) , max( ys ) )
	except Exception:
		return None


def _normalize_paddle_bbox( raw ):
	"""Paddle/Rapid bboxes come as either [ x1 , y1 , x2 , y2 ] or a
	4-point polygon. Normalize to AABB tuple."""
	if raw is None:
		return None
	try:
		seq = list( raw )
	except Exception:
		return None
	if len( seq ) == 4:
		first = seq[ 0 ]
		try:
			if all(
				isinstance( v , ( int , float ) ) or (
					hasattr( v , "__float__" ) and not hasattr( v , "__len__" )
				)
				for v in seq
			):
				return tuple( float( v ) for v in seq )
		except Exception:
			pass
		return _polygon_to_aabb( seq )
	if len( seq ) >= 3:
		return _polygon_to_aabb( seq )
	return None


def _crop_page_image_for_bbox( page_image , bbox , yolo_dpi ):
	"""Crop the bbox region out of a pre-rendered page image."""
	scale = OCR_DPI / yolo_dpi
	x1 , y1 , x2 , y2 = bbox
	cx1 = max( 0 , int( x1 * scale ) )
	cy1 = max( 0 , int( y1 * scale ) )
	cx2 = min( page_image.width  , int( x2 * scale ) )
	cy2 = min( page_image.height , int( y2 * scale ) )
	if cx2 <= cx1 or cy2 <= cy1:
		return None
	return page_image.crop( ( cx1 , cy1 , cx2 , cy2 ) )


# ---------------------------------------------------------------------------
# Embedded text ( pdfium text layer )
# ---------------------------------------------------------------------------

def _embedded_text( textpage , bbox , yolo_dpi , page_height_pt ):
	"""Pull text from a YOLO bbox using pdfium's text layer. Returns ''
	if there's nothing there ( scanned page / image-only PDF )."""
	left , bottom , right , top = _yolo_bbox_to_pdf_rect(
		bbox , yolo_dpi , page_height_pt ,
	)
	try:
		text = textpage.get_text_bounded(
			left=left , bottom=bottom , right=right , top=top ,
		) or ""
	except Exception:
		return ""
	return text.strip()


# ---------------------------------------------------------------------------
# Page-level OCR backends. Each returns a list of { 'bbox' , 'text' }
# dicts in OCR_DPI pixel coords ; we filter into YOLO bboxes by spatial
# containment afterwards.
# ---------------------------------------------------------------------------

# --- tesseract -------------------------------------------------------------

_TESSERACT_BIN = None
_TESSERACT_CHECKED = False
_TESSERACT_WARNED = False

def _find_tesseract():
	global _TESSERACT_BIN , _TESSERACT_CHECKED
	if _TESSERACT_CHECKED:
		return _TESSERACT_BIN
	_TESSERACT_CHECKED = True
	_TESSERACT_BIN = shutil.which( "tesseract" )
	return _TESSERACT_BIN


def _ocr_page_tesseract( pil_image , lang ):
	"""Run tesseract once on the full page in TSV mode and reconstruct
	per-line bboxes."""
	global _TESSERACT_WARNED
	tess = _find_tesseract()
	if not tess:
		if not _TESSERACT_WARNED:
			print(
				"OCR :: 'tesseract' binary not found on PATH. "
				"Install with : brew install tesseract  /  apt install tesseract-ocr"
			)
			_TESSERACT_WARNED = True
		return None
	with tempfile.TemporaryDirectory() as tmpdir:
		tmp_in = Path( tmpdir ) / "in.png"
		try:
			pil_image.save( tmp_in )
		except Exception:
			return None
		try:
			proc = subprocess.run(
				[ tess , str( tmp_in ) , "stdout" ,
				  "-l" , _lang_for( lang , "tesseract" ) ,
				  "--psm" , "3" , "tsv" ] ,
				capture_output=True , text=True , timeout=120 ,
			)
		except Exception:
			return None
		if proc.returncode != 0 or not proc.stdout:
			return None
	lines_by_key = {}
	for raw in proc.stdout.splitlines():
		parts = raw.split( "\t" )
		if len( parts ) < 12:
			continue
		try:
			level = int( parts[ 0 ] )
			page_n , block , para , line_n = ( int( parts[ 1 ] ) , int( parts[ 2 ] ) ,
			                                    int( parts[ 3 ] ) , int( parts[ 4 ] ) )
			x , y , w , h = ( int( parts[ 6 ] ) , int( parts[ 7 ] ) ,
			                   int( parts[ 8 ] ) , int( parts[ 9 ] ) )
		except ValueError:
			continue
		text = parts[ 11 ]
		if level != 5 or not text or not text.strip():
			continue
		key = ( page_n , block , para , line_n )
		bucket = lines_by_key.setdefault( key , {
			"bbox": [ x , y , x + w , y + h ] ,
			"words": [ text ] ,
		} )
		if bucket is None: continue
		bb = bucket[ "bbox" ]
		bb[ 0 ] = min( bb[ 0 ] , x )
		bb[ 1 ] = min( bb[ 1 ] , y )
		bb[ 2 ] = max( bb[ 2 ] , x + w )
		bb[ 3 ] = max( bb[ 3 ] , y + h )
		if bucket[ "words" ][ -1 ] != text:
			bucket[ "words" ].append( text )
	out = []
	for bucket in lines_by_key.values():
		text = " ".join( bucket[ "words" ] ).strip()
		if not text:
			continue
		bb = bucket[ "bbox" ]
		out.append( { "bbox": ( float( bb[ 0 ] ) , float( bb[ 1 ] ) ,
		                        float( bb[ 2 ] ) , float( bb[ 3 ] ) ) ,
		              "text": text } )
	return out


# --- RapidOCR ( default ) --------------------------------------------------

_RAPID_OCR = None
_RAPID_FAILED = False

def _get_rapid():
	global _RAPID_OCR , _RAPID_FAILED
	if _RAPID_FAILED:
		return None
	if _RAPID_OCR is not None:
		return _RAPID_OCR
	try:
		from rapidocr_onnxruntime import RapidOCR
	except Exception:
		try:
			from rapidocr import RapidOCR
		except Exception as e:
			print(
				f"OCR :: RapidOCR not installed ( {e} ). "
				f"Install with : pip install rapidocr-onnxruntime"
			)
			_RAPID_FAILED = True
			return None
	try:
		_RAPID_OCR = RapidOCR()
	except Exception as e:
		print( f"OCR :: RapidOCR init failed ( {e} )" )
		_RAPID_FAILED = True
		return None
	return _RAPID_OCR


def _ocr_page_rapid( pil_image , lang ):
	ocr = _get_rapid()
	if ocr is None:
		return None
	arr = np.array( pil_image.convert( "RGB" ) )
	try:
		result , _elapse = ocr( arr )
	except Exception as e:
		print( f"OCR :: rapidocr call failed ( {e} )" )
		return None
	if not result:
		return []
	out = []
	for item in result:
		if not item or len( item ) < 2:
			continue
		poly , text = item[ 0 ] , item[ 1 ]
		if not text:
			continue
		bbox = _polygon_to_aabb( poly )
		if bbox is None:
			continue
		out.append( { "bbox": bbox , "text": str( text ) } )
	return out


# --- PaddleOCR --------------------------------------------------------------

_PADDLE_OCR = None
_PADDLE_LANG = None
_PADDLE_FAILED = False

def _get_paddle( lang ):
	global _PADDLE_OCR , _PADDLE_LANG , _PADDLE_FAILED
	if _PADDLE_FAILED:
		return None
	paddle_lang = _lang_for( lang , "paddle" )
	if _PADDLE_OCR is not None and _PADDLE_LANG == paddle_lang:
		return _PADDLE_OCR
	try:
		from paddleocr import PaddleOCR
	except Exception as e:
		print( f"OCR :: PaddleOCR not installed ( {e} )." )
		_PADDLE_FAILED = True
		return None
	last_err = None
	# Prefer the mobile detector on CPU ( server det is ~5x slower ).
	for kwargs in (
		{ "text_detection_model_name":   "PP-OCRv5_mobile_det" ,
		  "text_recognition_model_name": f"{paddle_lang}_PP-OCRv5_mobile_rec" if paddle_lang == "en" else "PP-OCRv5_mobile_rec" ,
		  "use_doc_orientation_classify": False ,
		  "use_doc_unwarping":            False ,
		  "use_textline_orientation":     False ,
		  "lang":                         paddle_lang } ,
		{ "use_doc_orientation_classify": False ,
		  "use_doc_unwarping":            False ,
		  "use_textline_orientation":     False ,
		  "lang":                         paddle_lang } ,
		{ "use_angle_cls": False , "lang": paddle_lang } ,
		{ "lang": paddle_lang } ,
	):
		try:
			_PADDLE_OCR = PaddleOCR( **kwargs )
			_PADDLE_LANG = paddle_lang
			return _PADDLE_OCR
		except Exception as e:
			last_err = e
			continue
	print( f"OCR :: PaddleOCR init failed ( {last_err} )." )
	_PADDLE_FAILED = True
	return None


def _paddle_result_to_lines( result ):
	if not result:
		return []
	lines = []
	try:
		for page in result:
			rec_texts = rec_boxes = None
			if hasattr( page , "rec_texts" ):
				rec_texts = list( page.rec_texts or [] )
				rec_boxes = list( page.rec_boxes ) if page.rec_boxes is not None else []
			elif isinstance( page , dict ) and "rec_texts" in page:
				rec_texts = page.get( "rec_texts" , [] )
				rec_boxes = page.get( "rec_boxes" , [] )
			if rec_texts is None:
				continue
			for i , t in enumerate( rec_texts ):
				if not t:
					continue
				bbox = None
				if rec_boxes is not None and i < len( rec_boxes ):
					bbox = _normalize_paddle_bbox( rec_boxes[ i ] )
				if bbox is None:
					continue
				lines.append( { "bbox": bbox , "text": str( t ) } )
	except Exception:
		pass
	if lines:
		return lines
	# v2.x shape : list-of-lists -> [ polygon , ( text , conf ) ].
	try:
		for page in result:
			if not page: continue
			for item in page:
				if not item or len( item ) < 2: continue
				poly , txt = item[ 0 ] , item[ 1 ]
				text = txt[ 0 ] if isinstance( txt , ( list , tuple ) ) else txt
				if not text: continue
				bbox = _polygon_to_aabb( poly )
				if bbox is None: continue
				lines.append( { "bbox": bbox , "text": str( text ) } )
	except Exception:
		pass
	return lines


def _ocr_page_paddle( pil_image , lang ):
	ocr = _get_paddle( lang )
	if ocr is None:
		return None
	arr = np.array( pil_image.convert( "RGB" ) )
	try:
		if hasattr( ocr , "predict" ):
			result = ocr.predict( input=arr )
		else:
			result = ocr.ocr( arr , cls=False )
	except Exception as e:
		print( f"OCR :: paddle call failed ( {e} )" )
		return None
	return _paddle_result_to_lines( result )


# --- Surya ------------------------------------------------------------------

_SURYA_REC = None
_SURYA_DET = None
_SURYA_FAILED = False

def _get_surya():
	global _SURYA_REC , _SURYA_DET , _SURYA_FAILED
	if _SURYA_FAILED:
		return None , None
	if _SURYA_REC is not None and _SURYA_DET is not None:
		return _SURYA_REC , _SURYA_DET
	try:
		from surya.recognition import RecognitionPredictor
		from surya.detection import DetectionPredictor
	except Exception as e:
		print( f"OCR :: Surya not installed ( {e} ). Install with : pip install surya-ocr" )
		_SURYA_FAILED = True
		return None , None
	try:
		_SURYA_REC = RecognitionPredictor()
		_SURYA_DET = DetectionPredictor()
	except Exception as e:
		print( f"OCR :: Surya init failed ( {e} )." )
		_SURYA_FAILED = True
		return None , None
	return _SURYA_REC , _SURYA_DET


def _ocr_page_surya( pil_image , lang ):
	rec , det = _get_surya()
	if rec is None:
		return None
	surya_lang = _lang_for( lang , "surya" )
	try:
		preds = rec(
			images=[ pil_image ] , langs=[ [ surya_lang ] ] , det_predictor=det ,
		)
	except TypeError:
		try:
			preds = rec( [ pil_image ] , [ [ surya_lang ] ] , det )
		except Exception as e:
			print( f"OCR :: surya call failed ( {e} )" )
			return None
	except Exception as e:
		print( f"OCR :: surya call failed ( {e} )" )
		return None
	if not preds:
		return []
	pred = preds[ 0 ]
	text_lines = getattr( pred , "text_lines" , None ) or []
	out = []
	for tl in text_lines:
		text = getattr( tl , "text" , "" ) or ""
		if not text:
			continue
		bbox = getattr( tl , "bbox" , None )
		if bbox is None or len( bbox ) < 4:
			continue
		out.append( {
			"bbox": ( float( bbox[ 0 ] ) , float( bbox[ 1 ] ) ,
			          float( bbox[ 2 ] ) , float( bbox[ 3 ] ) ) ,
			"text": text ,
		} )
	return out


# --- Dispatcher -------------------------------------------------------------

def _ocr_page( pil_image , engine , lang ):
	"""Run the chosen engine on a whole page. Returns list of
	{ 'bbox' , 'text' } in OCR_DPI pixel coords. Falls back to
	tesseract if the chosen engine is unavailable."""
	if engine == ENGINE_RAPID:
		lines = _ocr_page_rapid( pil_image , lang )
	elif engine == ENGINE_PADDLE:
		lines = _ocr_page_paddle( pil_image , lang )
	elif engine == ENGINE_SURYA:
		lines = _ocr_page_surya( pil_image , lang )
	else:
		lines = _ocr_page_tesseract( pil_image , lang )
	if lines is None and engine != ENGINE_TESSERACT:
		lines = _ocr_page_tesseract( pil_image , lang )
	return lines or []


# ---------------------------------------------------------------------------
# Bbox -> text-line filtering
# ---------------------------------------------------------------------------

def _line_center( bbox ):
	x1 , y1 , x2 , y2 = bbox
	return ( ( x1 + x2 ) / 2.0 , ( y1 + y2 ) / 2.0 )


def _scale_yolo_bbox( bbox , yolo_dpi , ocr_dpi ):
	scale = ocr_dpi / yolo_dpi
	x1 , y1 , x2 , y2 = bbox
	return ( x1 * scale , y1 * scale , x2 * scale , y2 * scale )


def _lines_in_bbox( all_lines , yolo_bbox , yolo_dpi , ocr_dpi , pad=4.0 ):
	"""Filter and sort text-line dicts whose center falls inside the
	scaled YOLO bbox ( with small padding ). Top-to-bottom , left-to-right."""
	if not all_lines:
		return []
	tx1 , ty1 , tx2 , ty2 = _scale_yolo_bbox( yolo_bbox , yolo_dpi , ocr_dpi )
	tx1 -= pad ; ty1 -= pad ; tx2 += pad ; ty2 += pad
	hits = []
	for line in all_lines:
		cx , cy = _line_center( line[ "bbox" ] )
		if tx1 <= cx <= tx2 and ty1 <= cy <= ty2:
			hits.append( line )
	hits.sort( key=lambda l: ( l[ "bbox" ][ 1 ] , l[ "bbox" ][ 0 ] ) )
	return hits


def _lines_to_text( lines ):
	return "\n".join( l[ "text" ] for l in lines ).strip()


# ---------------------------------------------------------------------------
# Per-detection extraction
# ---------------------------------------------------------------------------

def _extract_text_for_det(
	det , textpage , page_height_pt , yolo_dpi ,
	ocr_lines_getter , force_ocr ,
):
	"""3-stage cascade : embedded -> garbled-check -> OCR fallback.
	`ocr_lines_getter` is a thunk that lazily runs the OCR engine on
	the full page once and returns its lines ; we filter into the
	bbox here."""
	bbox = det.get( "bbox" )
	if not bbox:
		return ""
	if not force_ocr:
		text = _embedded_text( textpage , bbox , yolo_dpi , page_height_pt )
		if (
			len( text ) >= MIN_EMBEDDED_TEXT
			and not _is_garbled( text )
			and not _looks_unspaced( text )
		):
			return _split_stuck_words( _normalize_text( text ) )
	all_lines = ocr_lines_getter()
	if not all_lines:
		return ""
	hits = _lines_in_bbox( all_lines , bbox , yolo_dpi , OCR_DPI )
	text = _lines_to_text( hits )
	if len( text ) < MIN_OCR_TEXT:
		return ""
	return _split_stuck_words( _normalize_text( text ) )


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def ocr_paper(
	pdf_path , yolo_data ,
	engine=DEFAULT_ENGINE , lang="en" ,
	force_ocr=False , force=False ,
	max_pages=None ,
):
	"""Pin OCR text onto each text-bearing YOLO detection in yolo_data.

	Mutates yolo_data[ 'pages' ] in place : every allowed detection
	gets det[ 'ocr' ][ engine ] = '<extracted text>' . Returns the
	number of detections we wrote new text for.

	Args :
	  pdf_path  : PDF on disk.
	  yolo_data : dict from src/pdf/pdf.yolo() ( wrapped { meta , pages }
	              or legacy bare-list of pages ).
	  engine    : 'rapid' ( default ) | 'paddle' | 'surya' | 'tesseract' .
	              Falls back through tesseract if the requested engine
	              isn't installed.
	  lang      : canonical 'en' / 'zh' / 'fr' / ... ( mapped per-backend )
	  force_ocr : skip the embedded-text path , OCR every bbox.
	  force     : overwrite det[ 'ocr' ][ engine ] even if already set.
	  max_pages : process at most this many pages ( default : all )."""

	if not yolo_data:
		return 0
	if isinstance( yolo_data , dict ):
		pages_yolo = yolo_data.get( "pages" , [] )
		yolo_dpi   = yolo_data.get( "meta" , {} ).get( "dpi" , PDF.DPI )
	else:
		pages_yolo = yolo_data
		yolo_dpi   = PDF.DPI
	if not pages_yolo:
		return 0
	if max_pages is not None:
		pages_yolo = pages_yolo[ : max_pages ]

	pdf_path = Path( pdf_path )
	if not pdf_path.exists():
		return 0

	pdf = pdfium.PdfDocument( str( pdf_path ) )
	n_written = 0
	try:
		n_pdf_pages = len( pdf )
		for page_idx , page_dets in enumerate( pages_yolo ):
			# Skip pages that have no text-bearing detections at all.
			text_dets = [
				d for d in page_dets
				if isinstance( d , dict )
				and d.get( "type" ) in TEXT_DETECTION_CLASSES
			]
			if not text_dets:
				continue
			# YOLO data may reference more pages than the live PDF
			# ( the PDF was replaced after YOLO ran ). Mark orphan-page
			# dets as attempted so the paper isn't re-queued forever.
			if page_idx >= n_pdf_pages:
				for det in text_dets:
					if engine not in ( det.get( "ocr" ) or {} ):
						det.setdefault( "ocr" , {} )[ engine ] = ""
				continue

			page      = pdf[ page_idx ]
			page_h_pt = page.get_height()

			# Lazy page-level OCR : only paid when at least one detection
			# on this page actually needs OCR ( embedded text was bad ).
			cached_lines = [ None ]
			def get_ocr_lines():
				if cached_lines[ 0 ] is None:
					scale = OCR_DPI / 72.0
					bitmap = page.render( scale=scale )
					img = bitmap.to_pil().convert( "RGB" )
					cached_lines[ 0 ] = _ocr_page( img , engine , lang )
				return cached_lines[ 0 ]

			textpage = page.get_textpage() if not force_ocr else None
			try:
				for det in text_dets:
					if not force:
						if engine in ( det.get( "ocr" ) or {} ):
							continue
					text = _extract_text_for_det(
						det , textpage , page_h_pt , yolo_dpi ,
						get_ocr_lines , force_ocr ,
					)
					det.setdefault( "ocr" , {} )[ engine ] = text
					if text:
						n_written += 1
			finally:
				if textpage is not None:
					try: textpage.close()
					except Exception: pass
				try: page.close()
				except Exception: pass
	finally:
		try: pdf.close()
		except Exception: pass

	return n_written
