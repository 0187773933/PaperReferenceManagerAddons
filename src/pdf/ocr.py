"""
PDF -> structured-content OCR.

Turn a PDF into a flat , reading-order list of section blocks so
downstream code can do RAG / summarization / chunking without having to
re-implement scientific-paper layout parsing every time.

Output shape:
  [
    { "type": "title"        , "lines": [ "..." ]       , "page": 0 } ,
    { "type": "abstract"     , "lines": [ "..." , ... ] , "page": 0 } ,
    { "type": "introduction" , "lines": [ "..." , ... ] , "page": 0 } ,
    { "type": "Figure 1"     , "lines": [ "Figure 1. ..." ] , "page": 1 } ,
    { "type": "methods"      , "lines": [ "..." , ... ] , "page": 2 } ,
    ...
  ]

Pipeline:
  1) Read <pdf>.yolo.json ( doclayout-yolo detections at pdf.DPI ).
  2) Per page , clean up the detections :
       a) drop low-confidence + 'abandon' ( running heads / page nums ) ,
       b) drop text-class boxes that sit inside figure / table boxes
          ( axis labels , in-figure annotations ) ,
       c) dedupe overlapping boxes ( YOLO often emits 2-3 nearly
          identical detections for the same paragraph ; we keep the
          highest-confidence one per IoU cluster ).
  3) Sort surviving detections in human reading order :
       - cluster x-centers to find 1- , 2- , or 3-column layout ,
       - interleave full-width blocks ( wide figures / page titles ) by
         y between column passes so they land where readers see them.
  4) For each block , pull text from the PDF :
       - first try pypdfium2's get_text_bounded() on the bbox ( fast ,
         exact , preserves ligatures ) ,
       - if that returns < MIN_EMBEDDED_TEXT chars , or if force_ocr is
         on , render the bbox region at OCR_DPI and OCR it via the
         system 'tesseract' binary ( pytesseract is NOT required ).
  5) Classify blocks into sections :
       - the FIRST 'title' detection in reading order = paper title ,
       - subsequent 'title' detections = section headings ; their text
         is matched against SECTION_KEYWORDS to assign a canonical type
         ( 'abstract' , 'introduction' , 'methods' , ... ) , falling
         back to the trimmed heading text for unknown sections ,
       - 'plain text' blocks accumulate into the current section ,
       - 'figure_caption' blocks become standalone { type: 'Figure N' } ,
       - 'table_caption' / 'table_footnote' become { type: 'Table N' } ,
       - formulas stay inline within the current section ,
       - a section interrupted by a figure flushes immediately so the
         output stays strictly linear ( no later page jumping ahead of
         an earlier one ).
  6) Dedupe consecutive identical paragraphs ( catches stragglers that
     escaped the IoU pass when YOLO drew two boxes at slightly
     different offsets ).

YOLO bboxes use top-left pixel coords at meta.dpi ; pdfium text coords
use bottom-left point coords. Conversion = pixels * 72 / dpi , with y
flipped against the page height.
"""

import io
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
from tqdm import tqdm

from . import pdf as PDF
from ..utils import utils

# OCR engine identifiers. The dispatcher tries the requested engine
# first and falls back through this preference order if it errors or
# isn't installed : <requested> -> tesseract.
ENGINE_RAPID     = "rapid"        # RapidOCR ( PP-OCRv5 on ONNX Runtime , default )
ENGINE_PADDLE    = "paddle"       # PaddleOCR ( PP-OCRv5 on paddlepaddle )
ENGINE_SURYA     = "surya"        # Surya OCR ( transformer )
ENGINE_TESSERACT = "tesseract"    # tesseract ( fallback )
ENGINE_MINERU    = "mineru"       # MinerU end-to-end ( formulas + tables ; bypasses YOLO )
DEFAULT_ENGINE   = ENGINE_RAPID

# DPI for re-rendering pages when we fall back to OCR. 300 is the
# tesseract sweet spot. Above this we waste CPU for negligible accuracy.
OCR_DPI = 300

# Classes from doclayout-yolo's docstructbench labels.
CLS_TITLE        = "title"
CLS_TEXT         = "plain text"
CLS_FIGURE       = "figure"
CLS_FIG_CAPTION  = "figure_caption"
CLS_TABLE        = "table"
CLS_TAB_CAPTION  = "table_caption"
CLS_TAB_FOOTNOTE = "table_footnote"
CLS_ABANDON      = "abandon"
CLS_FORMULA      = "isolate_formula"
CLS_FORM_CAPTION = "formula_caption"

# Classes we read out as text in reading order. 'figure' / 'table'
# anchors are kept around only for the suppress-text-inside-figure pass.
READING_ORDER_CLASSES = {
	CLS_TITLE , CLS_TEXT ,
	CLS_FIG_CAPTION ,
	CLS_TAB_CAPTION , CLS_TAB_FOOTNOTE ,
	CLS_FORMULA , CLS_FORM_CAPTION ,
}

CONTAINER_CLASSES = ( CLS_FIGURE , CLS_TABLE )

# Heading-keyword -> canonical section type. First match wins ; keep
# keywords lowercase and unaccented.
SECTION_KEYWORDS = (
	( "abstract"        , ( "abstract" , "summary" ) ) ,
	( "introduction"    , ( "introduction" , "background" ) ) ,
	( "related work"    , ( "related work" , "related works" , "prior work" , "literature review" ) ) ,
	( "methods"         , ( "methods" , "method" , "materials and methods" ,
	                         "materials & methods" , "experimental" ,
	                         "experimental setup" , "experimental procedure" ,
	                         "methodology" , "study design" ) ) ,
	( "results"         , ( "results" , "findings" , "experimental results" ) ) ,
	( "discussion"      , ( "discussion" , ) ) ,
	( "conclusions"     , ( "conclusion" , "conclusions" , "concluding remarks" ) ) ,
	( "future"          , ( "future work" , "future directions" , "future research" ) ) ,
	( "limitations"     , ( "limitations" , "limitation" ) ) ,
	( "acknowledgments" , ( "acknowledgments" , "acknowledgements" ) ) ,
	( "references"      , ( "references" , "bibliography" , "literature cited" ) ) ,
	( "appendix"        , ( "appendix" , "supplementary" , "supporting information" ) ) ,
)

# Trust embedded text only if we got at least this many chars from
# get_text_bounded() ; otherwise fall back to OCR.
MIN_EMBEDDED_TEXT = 8

# Garbled-text detection. Some PDFs ( notably ones built with custom
# CID-keyed fonts and no ToUnicode CMap ) return strings from
# get_text_bounded() that LOOK like text but are actually private-use
# glyph IDs or replacement chars -- length checks pass , readers see
# noise. We post-check the embedded text and , if it's garbled , treat
# it as if it were empty and fall through to OCR ( which doesn't care
# about the PDF's text layer ).
#   MAX_GARBLED_BAD_RATIO    : fraction of private-use + control +
#                              replacement chars above which we reject.
#   MIN_GARBLED_LETTER_RATIO : if real ASCII / Latin letters drop below
#                              this fraction , we also reject. Tuned for
#                              English papers ; relax for other scripts.
MAX_GARBLED_BAD_RATIO    = 0.05
MIN_GARBLED_LETTER_RATIO = 0.20

# "Stuck words" detection. Some PDFs ( CID-without-spacing , RapidOCR's
# PP-OCRv5 multilingual recognizer on tight English kerning ) emit text
# with missing inter-word spaces : 'furthertechnicaladvantageof'. We
# detect this two ways :
#   1) Pre-OCR : at the embedded-text stage , if avg word length is too
#      high we treat the embedded text as bad and fall through to OCR.
#   2) Post-OCR : tokens longer than STUCK_WORD_MIN_LEN are run through
#      a dictionary-based splitter ( wordninja ) ; we only accept the
#      split if it produces >= STUCK_WORD_MIN_SPLITS valid pieces so
#      real long words ( 'electroencephalography' ) stay intact.
STUCK_WORD_AVG_LEN_THRESHOLD = 12.0
STUCK_WORD_LONG_FRACTION     = 0.15      # >15% tokens longer than 18 chars
STUCK_WORD_LONG_LEN          = 18
STUCK_WORD_MIN_LEN           = 9         # 'absenceof' = 9 chars , still want to split

# Detection-confidence floor ( yolo.json already filters by its own
# threshold ; we lift it slightly for the text path to drop noise ).
MIN_DETECTION_CONF = 0.20

# IoU threshold for considering two same-class detections duplicates.
DEDUPE_IOU = 0.55

# Fraction of a text-class box that must be inside a figure/table box
# for us to drop it ( kills axis labels , in-figure annotations ).
SUPPRESS_INSIDE_FIG_FRAC = 0.55

# Min characters for OCR output to be treated as real text. Below this
# tesseract is almost certainly hallucinating from noise.
MIN_OCR_TEXT = 4


# ---------------------------------------------------------------------------
# OCR backends.
#
# Four are supported , chosen with the `engine` arg to parse() :
#   rapid     - RapidOCR ( PP-OCRv5 detection + recognition exported to
#               ONNX Runtime ) ; ~0.2s/page on CPU , 80 MB install ,
#               same accuracy as PaddleOCR. Default.
#   paddle    - PaddleOCR ( PP-OCRv5 on paddlepaddle ) ; same models as
#               rapid but ~5-30x slower because paddlepaddle's runtime
#               carries far more overhead than ONNX Runtime. Kept as an
#               option for users who already have it installed.
#   surya     - Surya OCR ; transformer-based , built specifically for
#               document layouts , higher quality on complex pages
#               ( handwriting , exotic scripts ) but 10-50x slower.
#   tesseract - last-resort fallback using the system 'tesseract' binary.
#
# Every engine takes a PIL page image and returns a list of
# { 'bbox' , 'text' } lines in the image's pixel coords ; the page-level
# pipeline filters those lines into YOLO bboxes by spatial containment.
# The dispatcher tries the requested engine first ; if its import or
# call fails we fall back to tesseract so the pipeline keeps running on
# machines without a deep-learning OCR stack installed.
#
# Backend lang codes ( the dispatcher takes a single canonical string
# and maps it per-backend ) :
#   "en" / "eng" -> rapid="en"  , paddle="en"     , surya="en" , tess="eng"
#   "zh"         -> rapid="ch"  , paddle="ch"     , surya="zh" , tess="chi_sim"
#   "fr"         -> rapid="fr"  , paddle="fr"     , surya="fr" , tess="fra"
# Default everywhere is English.
# ---------------------------------------------------------------------------

LANG_ALIASES = {
	"en":  { "rapid": "en"  , "paddle": "en"     , "surya": "en" , "tesseract": "eng"     , "mineru": "en" } ,
	"eng": { "rapid": "en"  , "paddle": "en"     , "surya": "en" , "tesseract": "eng"     , "mineru": "en" } ,
	"zh":  { "rapid": "ch"  , "paddle": "ch"     , "surya": "zh" , "tesseract": "chi_sim" , "mineru": "ch" } ,
	"fr":  { "rapid": "fr"  , "paddle": "fr"     , "surya": "fr" , "tesseract": "fra"     , "mineru": "fr" } ,
	"de":  { "rapid": "de"  , "paddle": "german" , "surya": "de" , "tesseract": "deu"     , "mineru": "de" } ,
	"es":  { "rapid": "es"  , "paddle": "es"     , "surya": "es" , "tesseract": "spa"     , "mineru": "es" } ,
}

def _lang_for( canonical , backend ):
	row = LANG_ALIASES.get( canonical , LANG_ALIASES[ "en" ] )
	return row[ backend ]


# --- tesseract ( subprocess to the binary ) --------------------------------

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


def _ocr_tesseract( pil_image , lang , psm=6 ):
	global _TESSERACT_WARNED
	tess = _find_tesseract()
	if not tess:
		if not _TESSERACT_WARNED:
			print(
				"OCR :: 'tesseract' binary not found on PATH ; "
				"install it ( brew install tesseract / apt install tesseract-ocr ) "
				"to enable the tesseract fallback."
			)
			_TESSERACT_WARNED = True
		return ""
	with tempfile.TemporaryDirectory() as tmpdir:
		tmp_in = Path( tmpdir ) / "in.png"
		try:
			pil_image.save( tmp_in )
		except Exception:
			return ""
		try:
			proc = subprocess.run(
				[ tess , str( tmp_in ) , "stdout" ,
				  "-l" , _lang_for( lang , "tesseract" ) ,
				  "--psm" , str( psm ) ] ,
				capture_output=True , text=True , timeout=120 ,
			)
		except Exception:
			return ""
		if proc.returncode != 0:
			return ""
		return ( proc.stdout or "" ).strip()


# --- RapidOCR ( PP-OCRv5 on ONNX Runtime , default ) -----------------------

_RAPID_OCR = None
_RAPID_FAILED = False

def _get_rapid():
	"""Lazy-init a RapidOCR instance. Single global because RapidOCR
	handles multi-lang in the same model , no per-lang separate
	instance needed."""
	global _RAPID_OCR , _RAPID_FAILED
	if _RAPID_FAILED:
		return None
	if _RAPID_OCR is not None:
		return _RAPID_OCR
	try:
		from rapidocr_onnxruntime import RapidOCR
	except Exception as e1:
		try:
			from rapidocr import RapidOCR
		except Exception as e2:
			print(
				f"OCR :: RapidOCR not installed ( {e1} ) ; "
				f"falling back to tesseract. "
				f"Install with : pip install rapidocr-onnxruntime"
			)
			_RAPID_FAILED = True
			return None
	try:
		_RAPID_OCR = RapidOCR()
	except Exception as e:
		print( f"OCR :: RapidOCR init failed ( {e} ) ; falling back to tesseract." )
		_RAPID_FAILED = True
		return None
	return _RAPID_OCR


def _ocr_page_rapid( pil_image , lang ):
	"""Run RapidOCR on a whole page image. Returns list of
	{ 'bbox' , 'text' } in pixel coords. lang is ignored : RapidOCR's
	bundled PP-OCRv5 rec model handles en/zh/etc. in one shot."""
	ocr = _get_rapid()
	if ocr is None:
		return None
	arr = np.array( pil_image.convert( "RGB" ) )
	try:
		result , _elapse = ocr( arr )
	except Exception as e:
		print( f"OCR :: rapidocr page call failed ( {e} )" )
		return None
	if not result:
		return []
	out = []
	for item in result:
		# Each item is [ polygon , text , confidence ].
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


# --- PaddleOCR ( PP-OCRv5 ) -------------------------------------------------

_PADDLE_OCR = None
_PADDLE_LANG = None
_PADDLE_FAILED = False
_PADDLE_WARMED = [ False ]

def _get_paddle( lang ):
	"""Lazy-init a PaddleOCR instance ( one per lang ). Returns None and
	prints a one-time warning if PaddleOCR isn't installed."""
	global _PADDLE_OCR , _PADDLE_LANG , _PADDLE_FAILED
	if _PADDLE_FAILED:
		return None
	paddle_lang = _lang_for( lang , "paddle" )
	if _PADDLE_OCR is not None and _PADDLE_LANG == paddle_lang:
		return _PADDLE_OCR
	try:
		from paddleocr import PaddleOCR
	except Exception as e:
		print(
			f"OCR :: PaddleOCR not installed ( {e} ) ; "
			f"falling back to tesseract. "
			f"Install with : pip install paddlepaddle paddleocr"
		)
		_PADDLE_FAILED = True
		return None
	# Layout / orientation features off because YOLO has already done
	# layout and our crops are upright single text blocks. PaddleOCR's
	# init signature shifted between 2.x and 3.x , so we try the modern
	# 3.x kwargs first and fall back to the legacy ones on any failure.
	# We catch broad Exception because Paddle raises plain Exception
	# subclasses ( e.g. 'Unknown argument: ...' ) , not TypeError.
	# Pick the MOBILE detector + recognizer by default ; the 'server'
	# variants are ~5x slower on CPU and the quality delta on clean
	# paper scans is negligible.
	last_err = None
	for kwargs in (
		{ "text_detection_model_name":    "PP-OCRv5_mobile_det" ,
		  "text_recognition_model_name":  f"{paddle_lang}_PP-OCRv5_mobile_rec" if paddle_lang == "en" else "PP-OCRv5_mobile_rec" ,
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
	print( f"OCR :: PaddleOCR init failed ( {last_err} ) ; falling back to tesseract." )
	_PADDLE_FAILED = True
	return None


def _polygon_to_aabb( polygon ):
	"""Convert a 4-point polygon ( [[x,y],[x,y],[x,y],[x,y]] ) to an
	axis-aligned bbox ( x1 , y1 , x2 , y2 ). Tolerates numpy arrays."""
	try:
		xs = [ float( pt[ 0 ] ) for pt in polygon ]
		ys = [ float( pt[ 1 ] ) for pt in polygon ]
		return ( min( xs ) , min( ys ) , max( xs ) , max( ys ) )
	except Exception:
		return None


def _normalize_paddle_bbox( raw ):
	"""Paddle bboxes come as either [ x1 , y1 , x2 , y2 ] or a 4-point
	polygon ( either as nested lists or numpy ). Normalize to AABB
	tuple. Returns None if unrecognizable."""
	if raw is None:
		return None
	try:
		seq = list( raw )
	except Exception:
		return None
	if len( seq ) == 4:
		# Could be either [ x1 , y1 , x2 , y2 ] or 4 points.
		first = seq[ 0 ]
		try:
			# 4 scalars -> AABB.
			if all( isinstance( v , ( int , float ) ) or hasattr( v , "__float__" ) and not hasattr( v , "__len__" ) for v in seq ):
				return tuple( float( v ) for v in seq )
		except Exception:
			pass
		# Otherwise treat as polygon.
		return _polygon_to_aabb( seq )
	if len( seq ) >= 3:
		return _polygon_to_aabb( seq )
	return None


def _paddle_result_to_lines( result ):
	"""Extract a list of { 'bbox' , 'text' } from a PaddleOCR result.
	Handles both v3.x ( OCRResult with rec_texts / rec_boxes ) and v2.x
	( list-of-lists ) shapes. Bboxes are in the page image's pixel coords."""
	if not result:
		return []
	lines = []
	# v3.x : list of OCRResult ( or dict ) per input image.
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
	# v2.x : list-of-lists -> [ polygon , ( text , conf ) ].
	try:
		for page in result:
			if not page:
				continue
			for item in page:
				if not item or len( item ) < 2:
					continue
				poly , txt = item[ 0 ] , item[ 1 ]
				text = txt[ 0 ] if isinstance( txt , ( list , tuple ) ) else txt
				if not text:
					continue
				bbox = _polygon_to_aabb( poly )
				if bbox is None:
					continue
				lines.append( { "bbox": bbox , "text": str( text ) } )
	except Exception:
		pass
	return lines


def _ocr_page_paddle( pil_image , lang ):
	"""Run PaddleOCR on a whole page image. Returns list of
	{ 'bbox' , 'text' } in pixel coords matching the input image."""
	ocr = _get_paddle( lang )
	if ocr is None:
		return None      # signal "engine unavailable"
	arr = np.array( pil_image.convert( "RGB" ) )
	try:
		if hasattr( ocr , "predict" ):
			result = ocr.predict( input=arr )
		else:
			result = ocr.ocr( arr , cls=False )
	except Exception as e:
		print( f"OCR :: paddle page call failed ( {e} )" )
		return None
	return _paddle_result_to_lines( result )


# --- Surya OCR --------------------------------------------------------------

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
		print(
			f"OCR :: Surya not installed ( {e} ) ; "
			f"falling back to tesseract. Install with : pip install surya-ocr"
		)
		_SURYA_FAILED = True
		return None , None
	try:
		_SURYA_REC = RecognitionPredictor()
		_SURYA_DET = DetectionPredictor()
	except Exception as e:
		print( f"OCR :: Surya init failed ( {e} ) ; falling back to tesseract." )
		_SURYA_FAILED = True
		return None , None
	return _SURYA_REC , _SURYA_DET


def _ocr_page_surya( pil_image , lang ):
	"""Run Surya OCR on a whole page image. Returns list of
	{ 'bbox' , 'text' } in pixel coords."""
	rec , det = _get_surya()
	if rec is None:
		return None
	surya_lang = _lang_for( lang , "surya" )
	try:
		preds = rec(
			images=[ pil_image ] ,
			langs=[ [ surya_lang ] ] ,
			det_predictor=det ,
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


# --- tesseract page-level ( image_to_data TSV -> bboxes + text ) ----------

_TESSERACT_TSV_WARNED = False

def _ocr_page_tesseract( pil_image , lang ):
	"""Run tesseract once on the full page in TSV mode and reconstruct
	per-line bboxes. Each line carries the concatenated words on that
	line plus the line's AABB ."""
	global _TESSERACT_TSV_WARNED
	tess = _find_tesseract()
	if not tess:
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
	# TSV columns : level page block para line word left top width height conf text.
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
		if bucket is None:
			continue
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


# --- MinerU ( end-to-end PDF -> structured content ; bypasses YOLO ) --------
#
# MinerU is structurally different from the other engines : it consumes a
# whole PDF and produces a content_list.json that is already a flat list
# of typed blocks with page_idx. We invoke its CLI via subprocess , read
# the resulting content_list.json , and convert it to our block schema.
# YOLO is skipped entirely in this branch ; MinerU has its own layout.

_MINERU_BIN = None
_MINERU_CHECKED = False

def _find_mineru():
	global _MINERU_BIN , _MINERU_CHECKED
	if _MINERU_CHECKED:
		return _MINERU_BIN
	_MINERU_CHECKED = True
	_MINERU_BIN = shutil.which( "mineru" )
	return _MINERU_BIN


def _find_mineru_content_list( output_root ):
	"""MinerU writes content_list.json under
	   <out>/<pdf_stem>/auto/<pdf_stem>_content_list.json
	   ( or .../ocr/... when parse_method=ocr ). We just glob for any
	   *_content_list.json under the output root."""
	matches = list( Path( output_root ).rglob( "*_content_list.json" ) )
	if not matches:
		return None
	# Newest-mtime wins in the unlikely case of multiple.
	matches.sort( key=lambda p: p.stat().st_mtime , reverse=True )
	return matches[ 0 ]


def _parse_with_mineru( pdf_path , lang , max_pages=None ,
                        parse_method="auto" , backend="pipeline" ):
	"""Run MinerU on a single PDF , return list of { type , lines , page }
	blocks parsed from its content_list.json. Returns [] if MinerU
	isn't installed or the run fails."""
	mineru_bin = _find_mineru()
	if not mineru_bin:
		print(
			"OCR :: 'mineru' command not found. "
			"Install with : pip install 'mineru[pipeline]'"
		)
		return []

	mineru_lang = _lang_for( lang , "mineru" )
	with tempfile.TemporaryDirectory( prefix="prma_mineru_" ) as tmpdir:
		cmd = [
			mineru_bin ,
			"-p" , str( pdf_path ) ,
			"-o" , tmpdir ,
			"-b" , backend ,
			"-m" , parse_method ,
			"-l" , mineru_lang ,
		]
		if max_pages is not None:
			# MinerU expects 0-based end page id ( inclusive ).
			cmd.extend( [ "-e" , str( max( 0 , int( max_pages ) - 1 ) ) ] )
		try:
			proc = subprocess.run(
				cmd , capture_output=True , text=True ,
				timeout=1800 ,    # 30 min per PDF cap
			)
		except subprocess.TimeoutExpired:
			print( f"OCR :: mineru timed out on {pdf_path.name}" )
			return []
		except Exception as e:
			print( f"OCR :: mineru call failed ( {e} )" )
			return []
		if proc.returncode != 0:
			tail = ( proc.stderr or proc.stdout or "" ).strip().splitlines()[ -3 : ]
			print( f"OCR :: mineru exit {proc.returncode} on {pdf_path.name} : {' | '.join(tail)}" )
			return []
		cl_path = _find_mineru_content_list( tmpdir )
		if cl_path is None:
			print( f"OCR :: mineru produced no content_list.json for {pdf_path.name}" )
			return []
		try:
			content_list = utils.read_json( cl_path )
		except Exception as e:
			print( f"OCR :: failed to read mineru output ( {e} )" )
			return []
	return _mineru_content_list_to_blocks( content_list )


def _mineru_content_list_to_blocks( content_list ):
	"""Convert MinerU's content_list.json into our flat block schema.

	MinerU emits items of the form :
	  { type: 'text'     , text: '...'                , page_idx: N }
	  { type: 'title'    , text: '...' , text_level: K , page_idx: N }
	  { type: 'image'    , img_caption: [...] , img_path: '...'    , page_idx: N }
	  { type: 'table'    , table_caption: [...] , table_body: '<table>...</table>' , page_idx: N }
	  { type: 'equation' , text: 'a = b^2'        , text_format: 'latex' , page_idx: N }

	We map :
	  - first 'title' -> { type: 'title' , ... }
	  - subsequent 'title' -> opens a new section ( classified via
	    SECTION_KEYWORDS ; unknown -> trimmed heading text )
	  - 'text' -> appended to current section
	  - 'image' -> { type: 'Figure N' , lines: [caption...] }
	  - 'table' -> { type: 'Table N' , lines: [caption...] }
	  - 'equation' -> appended inline to current section ( LaTeX preserved )
	"""
	if not content_list:
		return []
	blocks            = []
	open_block        = None
	last_heading_type = None
	have_paper_title  = False
	n_figure          = 0
	n_table           = 0

	def flush():
		nonlocal open_block
		if open_block and open_block[ "lines" ]:
			blocks.append( open_block )
		open_block = None

	def open_section( section_type , page_idx ):
		nonlocal open_block
		open_block = { "type": section_type , "lines": [] , "page": page_idx }

	for item in content_list:
		if not isinstance( item , dict ):
			continue
		item_type = item.get( "type" , "text" )
		page_idx  = int( item.get( "page_idx" , 0 ) or 0 )
		text      = item.get( "text" , "" ) or ""

		if item_type == "title":
			flush()
			if not have_paper_title:
				paras = _to_paragraphs( _normalize_text( text ) )
				if paras:
					blocks.append( {
						"type": "title" , "lines": paras , "page": page_idx ,
					} )
					have_paper_title = True
				continue
			last_heading_type = _classify_heading( text )
			open_section( last_heading_type , page_idx )
			continue

		if item_type == "text":
			paras = _to_paragraphs( _normalize_text( text ) )
			if not paras:
				continue
			if open_block is None:
				if last_heading_type:
					section_type = last_heading_type
				elif not have_paper_title or page_idx <= 1:
					section_type = "abstract"
					last_heading_type = "abstract"
				else:
					section_type = "body"
				open_section( section_type , page_idx )
			open_block[ "lines" ].extend( paras )
			continue

		if item_type == "image":
			flush()
			n_figure += 1
			caps_raw = item.get( "img_caption" ) or []
			# Normalize : MinerU emits a list of strings.
			caps = []
			for c in caps_raw:
				if not c:
					continue
				caps.extend( _to_paragraphs( _normalize_text( str( c ) ) ) )
			label = _figure_label( caps[ 0 ] if caps else "" , n_figure )
			blocks.append( {
				"type":  label ,
				"lines": caps or [ label ] ,
				"page":  page_idx ,
			} )
			continue

		if item_type == "table":
			flush()
			n_table += 1
			caps_raw = item.get( "table_caption" ) or []
			caps = []
			for c in caps_raw:
				if not c:
					continue
				caps.extend( _to_paragraphs( _normalize_text( str( c ) ) ) )
			label = _table_label( caps[ 0 ] if caps else "" , n_table )
			lines = caps or [ label ]
			# Keep the raw HTML table body as a trailing line so downstream
			# consumers can re-parse it ; truncate insanely large tables.
			body = item.get( "table_body" ) or ""
			if body and len( body ) < 16000:
				lines.append( body )
			blocks.append( {
				"type":  label ,
				"lines": lines ,
				"page":  page_idx ,
			} )
			continue

		if item_type == "equation":
			if open_block is not None and text:
				open_block[ "lines" ].append( text )
			continue

	flush()
	return _dedupe_blocks( blocks )


# --- page-level dispatcher --------------------------------------------------

def _ocr_page( pil_image , engine , lang ):
	"""Run the requested engine on a whole page. Returns list of
	{ 'bbox' , 'text' } or [] on failure. Falls back to tesseract if
	the chosen engine is unavailable ( returns None )."""
	if engine == ENGINE_RAPID:
		lines = _ocr_page_rapid( pil_image , lang )
		if lines is None:
			lines = _ocr_page_tesseract( pil_image , lang )
		return lines or []
	if engine == ENGINE_PADDLE:
		lines = _ocr_page_paddle( pil_image , lang )
		if lines is None:
			lines = _ocr_page_tesseract( pil_image , lang )
		return lines or []
	if engine == ENGINE_SURYA:
		lines = _ocr_page_surya( pil_image , lang )
		if lines is None:
			lines = _ocr_page_tesseract( pil_image , lang )
		return lines or []
	return _ocr_page_tesseract( pil_image , lang ) or []


# --- text-line -> bbox filtering -------------------------------------------

def _line_center( bbox ):
	x1 , y1 , x2 , y2 = bbox
	return ( ( x1 + x2 ) / 2.0 , ( y1 + y2 ) / 2.0 )


def _scale_yolo_bbox( bbox , yolo_dpi , ocr_dpi ):
	scale = ocr_dpi / yolo_dpi
	x1 , y1 , x2 , y2 = bbox
	return ( x1 * scale , y1 * scale , x2 * scale , y2 * scale )


def _lines_in_bbox( all_lines , yolo_bbox , yolo_dpi , ocr_dpi , pad=4.0 ):
	"""Return text-line dicts whose center lies inside the given YOLO
	bbox ( with a small padding ) , sorted top-to-bottom then left-to-
	right. all_lines and yolo_bbox are converted to a shared scale
	( OCR_DPI pixel coords )."""
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
# Geometry helpers.
# ---------------------------------------------------------------------------

def _bbox_area( bbox ):
	x1 , y1 , x2 , y2 = bbox
	return max( 0 , x2 - x1 ) * max( 0 , y2 - y1 )

def _bbox_center_x( bbox ):
	x1 , _ , x2 , _ = bbox
	return ( x1 + x2 ) / 2.0

def _bbox_top( bbox ):
	return bbox[ 1 ]

def _bbox_width( bbox ):
	return bbox[ 2 ] - bbox[ 0 ]

def _bbox_intersection_area( a , b ):
	ax1 , ay1 , ax2 , ay2 = a
	bx1 , by1 , bx2 , by2 = b
	x1 = max( ax1 , bx1 )
	y1 = max( ay1 , by1 )
	x2 = min( ax2 , bx2 )
	y2 = min( ay2 , by2 )
	if x2 <= x1 or y2 <= y1:
		return 0.0
	return ( x2 - x1 ) * ( y2 - y1 )

def _bbox_iou( a , b ):
	inter = _bbox_intersection_area( a , b )
	if inter <= 0:
		return 0.0
	union = _bbox_area( a ) + _bbox_area( b ) - inter
	if union <= 0:
		return 0.0
	return inter / union

def _bbox_inside_frac( inner , outer ):
	"""Fraction of `inner`'s area that lies inside `outer`."""
	a = _bbox_area( inner )
	if a <= 0:
		return 0.0
	return _bbox_intersection_area( inner , outer ) / a


# ---------------------------------------------------------------------------
# Preprocess detections : drop noise , suppress text-in-figure , dedupe.
# ---------------------------------------------------------------------------

def _preprocess_detections( dets ):
	"""Apply confidence + class filters , suppress text boxes inside
	figure / table anchors , then IoU-dedupe within each class.
	Returns only detections in READING_ORDER_CLASSES ( figures /
	tables are dropped after they've done their suppression job )."""
	# Confidence + class filter.
	kept = []
	containers = []
	for d in dets:
		conf = float( d.get( "confidence" , 1.0 ) )
		cls  = d.get( "type" )
		bbox = d.get( "bbox" )
		if not bbox or _bbox_area( bbox ) <= 0:
			continue
		if cls in CONTAINER_CLASSES:
			containers.append( d )
			continue
		if cls not in READING_ORDER_CLASSES:
			continue
		if conf < MIN_DETECTION_CONF:
			continue
		kept.append( d )

	# Suppress text-class boxes that live mostly inside a figure / table.
	# Figure captions live OUTSIDE figures normally , so we don't
	# suppress captions even if they nick the figure edge ( hence the
	# 0.55 threshold rather than e.g. 0.30 ).
	if containers:
		suppressed = []
		for d in kept:
			# Captions can graze container edges ; only suppress
			# plain_text / title that's clearly buried inside.
			if d[ "type" ] not in ( CLS_TEXT , CLS_TITLE ):
				suppressed.append( d )
				continue
			inside = False
			for c in containers:
				if _bbox_inside_frac( d[ "bbox" ] , c[ "bbox" ] ) >= SUPPRESS_INSIDE_FIG_FRAC:
					inside = True
					break
			if not inside:
				suppressed.append( d )
		kept = suppressed

	# IoU dedupe within each class. Sort by confidence desc and greedily
	# keep boxes that don't overlap a previously-kept box of the same
	# class by more than DEDUPE_IOU.
	by_class = {}
	for d in kept:
		by_class.setdefault( d[ "type" ] , [] ).append( d )

	deduped = []
	for cls , group in by_class.items():
		group.sort( key=lambda d: -float( d.get( "confidence" , 0.0 ) ) )
		survivors = []
		for d in group:
			dup = False
			for k in survivors:
				if _bbox_iou( d[ "bbox" ] , k[ "bbox" ] ) >= DEDUPE_IOU:
					dup = True
					break
			if not dup:
				survivors.append( d )
		deduped.extend( survivors )

	return deduped


# ---------------------------------------------------------------------------
# Column detection + reading order.
# ---------------------------------------------------------------------------

def _cluster_column_centers( centers , page_width ):
	"""Cluster a list of x-centers into 1 , 2 , or 3 columns by finding
	the biggest gap(s) between sorted centers. Returns a list of column
	boundary x-values ( length 0 for single-column , 1 for 2-col , 2 for
	3-col ). Conservative : only declares a multi-column layout when
	there's a clear gap of >= page_width * 0.10 between center groups."""
	if len( centers ) < 4:
		return []
	sorted_c = sorted( centers )
	# Compute gaps between consecutive centers.
	gaps = [ ( sorted_c[ i + 1 ] - sorted_c[ i ] , i ) for i in range( len( sorted_c ) - 1 ) ]
	min_gap = page_width * 0.10
	big_gaps = [ ( g , i ) for g , i in gaps if g >= min_gap ]
	if not big_gaps:
		return []
	# Take up to the 2 largest gaps for 2- or 3-col detection.
	big_gaps.sort( key=lambda gi: -gi[ 0 ] )
	top_gaps = sorted( big_gaps[ : 2 ] , key=lambda gi: gi[ 1 ] )
	boundaries = []
	for g , i in top_gaps:
		# Boundary is midway between the two centers that bracket the gap.
		boundaries.append( ( sorted_c[ i ] + sorted_c[ i + 1 ] ) / 2.0 )
	# Require at least 2 centers on each side of each boundary , else
	# treat as single-column ( prevents one outlier from creating a col ).
	def has_quorum( bnds ):
		bnds = sorted( bnds )
		all_bnds = [ 0.0 ] + bnds + [ float( "inf" ) ]
		for j in range( len( all_bnds ) - 1 ):
			lo , hi = all_bnds[ j ] , all_bnds[ j + 1 ]
			n = sum( 1 for c in sorted_c if lo <= c < hi )
			if n < 2:
				return False
		return True
	while boundaries and not has_quorum( boundaries ):
		boundaries.pop()
	return boundaries


def _assign_column( bbox , boundaries , page_width ):
	"""Return ( column_index , is_wide ). column_index is 0..N-1 ; is_wide
	is True for blocks that span more than one column ( e.g. a wide
	figure caption )."""
	if not boundaries:
		return 0 , False
	# A block is wide if its width is > 60% of page width.
	if _bbox_width( bbox ) > page_width * 0.60:
		return 0 , True
	cx = _bbox_center_x( bbox )
	for i , b in enumerate( boundaries ):
		if cx < b:
			return i , False
	return len( boundaries ) , False


def _reading_order( detections , page_width ):
	"""Sort detections in human reading order using column-aware passes.
	Wide blocks ( spanning columns ) interrupt the column flow at their
	y position and are emitted before any column block that starts
	below them , while column blocks above the wide block are emitted
	first ( column by column )."""
	if not detections:
		return []

	centers = [ _bbox_center_x( d[ "bbox" ] ) for d in detections
	            if d[ "type" ] in ( CLS_TEXT , CLS_TITLE , CLS_FIG_CAPTION , CLS_TAB_CAPTION ) ]
	boundaries = _cluster_column_centers( centers , page_width )

	cols = {}     # col_idx -> list of detections
	wide = []
	for d in detections:
		col_idx , is_wide = _assign_column( d[ "bbox" ] , boundaries , page_width )
		if is_wide:
			wide.append( d )
		else:
			cols.setdefault( col_idx , [] ).append( d )

	# Sort each column by y_top.
	for c in cols.values():
		c.sort( key=lambda d: _bbox_top( d[ "bbox" ] ) )
	wide.sort( key=lambda d: _bbox_top( d[ "bbox" ] ) )

	if not boundaries:
		# Single column : just merge column 0 with wide by y.
		merged = sorted(
			cols.get( 0 , [] ) + wide ,
			key=lambda d: _bbox_top( d[ "bbox" ] ) ,
		)
		return merged

	# Multi-column with wide blocks : split each wide block into bands.
	# Within a band ( above a given wide block ) , emit columns
	# left-to-right ; then emit the wide block ; repeat.
	num_cols = max( cols.keys() , default=-1 ) + 1
	col_idx_ptrs = [ 0 ] * num_cols
	col_lists    = [ cols.get( i , [] ) for i in range( num_cols ) ]
	out = []

	def flush_above( y_cut ):
		for ci in range( num_cols ):
			lst , p = col_lists[ ci ] , col_idx_ptrs[ ci ]
			while p < len( lst ) and _bbox_top( lst[ p ][ "bbox" ] ) < y_cut:
				out.append( lst[ p ] )
				p += 1
			col_idx_ptrs[ ci ] = p

	for w in wide:
		flush_above( _bbox_top( w[ "bbox" ] ) )
		out.append( w )

	# Flush any remaining column tail.
	flush_above( float( "inf" ) )
	return out


# ---------------------------------------------------------------------------
# Text extraction per bbox.
# ---------------------------------------------------------------------------

def _yolo_bbox_to_pdf_rect( bbox , yolo_dpi , page_height_pt ):
	"""Convert YOLO pixel bbox ( origin top-left ) to a pdfium rectangle
	( origin bottom-left ) in PDF points."""
	x1 , y1 , x2 , y2 = bbox
	pt_per_px = 72.0 / yolo_dpi
	left   = x1 * pt_per_px
	right  = x2 * pt_per_px
	top    = page_height_pt - ( y1 * pt_per_px )
	bottom = page_height_pt - ( y2 * pt_per_px )
	return left , bottom , right , top


def _crop_page_image_for_bbox( page_image , bbox , yolo_dpi ):
	"""Crop the bbox region out of a pre-rendered page image. page_image
	was rendered at OCR_DPI ; bbox is in YOLO pixels at yolo_dpi."""
	scale = OCR_DPI / yolo_dpi
	x1 , y1 , x2 , y2 = bbox
	cx1 = max( 0 , int( x1 * scale ) )
	cy1 = max( 0 , int( y1 * scale ) )
	cx2 = min( page_image.width  , int( x2 * scale ) )
	cy2 = min( page_image.height , int( y2 * scale ) )
	if cx2 <= cx1 or cy2 <= cy1:
		return None
	return page_image.crop( ( cx1 , cy1 , cx2 , cy2 ) )


def _embedded_text( textpage , bbox , yolo_dpi , page_height_pt ):
	"""Pull text from a YOLO bbox using pdfium's text layer. '' if
	there's nothing there ( scanned page / image-only PDF )."""
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


def _is_garbled( text ):
	"""Heuristic : True if `text` is dense in private-use Unicode ,
	replacement chars , or control chars ( typical CID-without-ToUnicode
	garbage ) , OR has too few real letters to be readable. Length-
	sensitive but works on snippets as short as ~10 chars."""
	if not text:
		return False
	n = len( text )
	bad = 0
	letters = 0
	for ch in text:
		cp = ord( ch )
		# Private-use area : CID-direct rendering with no ToUnicode map.
		if 0xE000 <= cp <= 0xF8FF:
			bad += 1
			continue
		# Pdfium gave up and emitted U+FFFD.
		if cp == 0xFFFD:
			bad += 1
			continue
		# Control char that isn't normal whitespace.
		if cp < 0x20 and ch not in "\t\n\r":
			bad += 1
			continue
		# Surrogate halves leaking through.
		if 0xD800 <= cp <= 0xDFFF:
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
	"""Detect text whose word boundaries collapsed ( CID-without-
	spacing extracts , some scanned-OCR outputs ). Returns True if the
	average token is suspiciously long or many tokens are absurdly long ;
	the caller treats this like _is_garbled and falls through to OCR."""
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


def _extract_text( det , textpage , page_height_pt , yolo_dpi ,
                   ocr_lines_getter , force_ocr ):
	"""Get text for one detection.

	Default ( force_ocr=False ) is a 3-stage cascade :
	  1) embedded text from pdfium's text layer ( free , exact when it
	     works ) ;
	  2) if embedded is missing , too short , or _is_garbled ( CID-junk
	     from PDFs with broken / missing ToUnicode CMaps ) , trigger
	     the configured OCR engine on the FULL page once ( cached
	     across all bboxes on this page ) and filter its text lines
	     into this bbox by spatial containment ;
	  3) if the engine fails to load or returns nothing , the engine
	     dispatcher itself falls back to tesseract page-level.

	By going page-at-a-time instead of bbox-at-a-time we cut the OCR
	model invocations from ~10x per page to 1x per page , and the
	embedded-first default means PDFs with intact text layers never
	pay the OCR cost at all."""
	bbox = det[ "bbox" ]
	if not force_ocr:
		text = _embedded_text( textpage , bbox , yolo_dpi , page_height_pt )
		if (
			len( text ) >= MIN_EMBEDDED_TEXT
			and not _is_garbled( text )
			and not _looks_unspaced( text )
		):
			return text
	all_lines = ocr_lines_getter()
	if not all_lines:
		return ""
	hits = _lines_in_bbox( all_lines , bbox , yolo_dpi , OCR_DPI )
	text = _lines_to_text( hits )
	if len( text ) < MIN_OCR_TEXT:
		return ""
	return text


# ---------------------------------------------------------------------------
# Text cleanup : normalize PDF junk , break into paragraph lines.
# ---------------------------------------------------------------------------

_LIGATURES = {
	"ﬀ": "ff" , "ﬁ": "fi" , "ﬂ": "fl" ,
	"ﬃ": "ffi" , "ﬄ": "ffl" , "ﬅ": "ft" ,
}

_SOFT_HYPHEN     = "­"
_NB_HYPHEN       = "‑"
_FANCY_QUOTES    = { "‘": "'" , "’": "'" , "“": '"' , "”": '"' }
_HYPHEN_WRAP_RE  = re.compile( r"([A-Za-z])-$" )
_SOFT_WRAP_TAIL  = re.compile( r"[a-z,;:\-—–]$" )


def _normalize_text( text ):
	"""Unicode-normalize , de-ligature , de-fancy-quote , strip junk
	control chars and excess whitespace."""
	if not text:
		return ""
	text = unicodedata.normalize( "NFKC" , text )
	for k , v in _LIGATURES.items():
		text = text.replace( k , v )
	for k , v in _FANCY_QUOTES.items():
		text = text.replace( k , v )
	text = text.replace( _SOFT_HYPHEN , "" )
	text = text.replace( _NB_HYPHEN , "-" )
	# Strip control chars except newline / tab.
	text = "".join(
		ch for ch in text
		if ch == "\n" or ch == "\t" or unicodedata.category( ch )[ 0 ] != "C"
	)
	out_lines = []
	for raw in text.splitlines():
		line = re.sub( r"[ \t]+" , " " , raw ).strip()
		out_lines.append( line )
	# Collapse runs of blank lines to a single blank.
	collapsed = []
	prev_blank = False
	for ln in out_lines:
		if not ln:
			if prev_blank:
				continue
			prev_blank = True
		else:
			prev_blank = False
		collapsed.append( ln )
	return "\n".join( collapsed ).strip()


_WORDNINJA = None
_WORDNINJA_FAILED = False

def _get_wordninja():
	"""Lazy-load wordninja. Returns None if unavailable."""
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
	"""If `token` looks like multiple words concatenated , split via
	wordninja and return the pieces. Otherwise return [token].

	Guards , in order :
	  - token shorter than STUCK_WORD_MIN_LEN -> skip ;
	  - any uppercase past index 0 -> skip ( preserves 'TensorFlow' ,
	    'AlphaFold' , 'iPhone' , etc. ) ;
	  - any digit in the inner alpha part -> skip ( IDs , codes ) ;
	  - wordninja returns 1 piece -> token is in its frequency dict ,
	    keep as-is ( this is how 'electroencephalography' survives ) ;
	  - wordninja returns 2+ pieces -> accept the split.

	Preserves leading / trailing punctuation."""
	if len( token ) < STUCK_WORD_MIN_LEN:
		return [ token ]
	wn = _get_wordninja()
	if wn is None:
		return [ token ]
	prefix , suffix , inner = "" , "" , token
	while inner and not inner[ 0 ].isalpha():
		prefix += inner[ 0 ]
		inner = inner[ 1: ]
	while inner and not inner[ -1 ].isalpha():
		suffix = inner[ -1 ] + suffix
		inner = inner[ :-1 ]
	if len( inner ) < STUCK_WORD_MIN_LEN or not inner.isalpha():
		return [ token ]
	# CamelCase / mid-word uppercase = likely a code identifier or
	# branded term ; don't touch.
	if any( c.isupper() for c in inner[ 1: ] ):
		return [ token ]
	try:
		parts = wn.split( inner.lower() )
	except Exception:
		return [ token ]
	if len( parts ) < 2:
		# wordninja recognized this as a single dictionary word.
		return [ token ]
	# Reject splits where any piece is a single letter -- almost always
	# wordninja over-splitting on rare-word territory ( 'aspecific' ->
	# 'a specific' is the only safe single-letter case , and that's
	# vanishingly rare in scientific prose ).
	if any( len( p ) <= 1 for p in parts ):
		return [ token ]
	if inner[ 0 ].isupper():
		parts[ 0 ] = parts[ 0 ].capitalize()
	if prefix:
		parts[ 0 ] = prefix + parts[ 0 ]
	if suffix:
		parts[ -1 ] = parts[ -1 ] + suffix
	return parts


def _split_stuck_words_in_line( line ):
	"""Apply _split_stuck_token to every token in a single line and
	rejoin with single spaces."""
	if not line:
		return line
	out = []
	for tok in line.split():
		out.extend( _split_stuck_token( tok ) )
	return " ".join( out )


def _to_paragraphs( text ):
	"""Break normalized text into paragraph strings , joining soft-wrap
	and hyphenated line breaks within each paragraph."""
	if not text:
		return []
	paragraphs = []
	buf_lines = []
	for line in text.split( "\n" ):
		if not line.strip():
			if buf_lines:
				paragraphs.append( buf_lines )
				buf_lines = []
			continue
		buf_lines.append( line )
	if buf_lines:
		paragraphs.append( buf_lines )

	out = []
	for lines in paragraphs:
		buf = ""
		for line in lines:
			line = line.rstrip()
			if not buf:
				buf = line
				continue
			m = _HYPHEN_WRAP_RE.search( buf )
			if m:
				buf = buf[ : -1 ] + line.lstrip()
				continue
			if _SOFT_WRAP_TAIL.search( buf ):
				buf = buf + " " + line.lstrip()
				continue
			buf = buf + " " + line.lstrip()
		buf = re.sub( r"\s+" , " " , buf ).strip()
		if buf:
			# Final pass : split stuck-words ( 'furthertechnicaladvantage'
			# -> 'further technical advantage' ). Cheap when wordninja
			# isn't installed or when no tokens are long enough.
			buf = _split_stuck_words_in_line( buf )
			out.append( buf )
	return out


# ---------------------------------------------------------------------------
# Section / caption classification.
# ---------------------------------------------------------------------------

_HEADING_PREFIX_RE = re.compile(
	r"^\s*"
	r"(?:"
		r"\d+(?:\.\d+)*[.):\-]?"   # '3' , '3.', '3.1' , '3.1.2' , '3)'
		r"|"
		r"[ivxlcdm]+\."            # 'iii.' -- REQUIRES trailing period
		r"|"                        # so single Roman-numeral letters
		r"[IVXLCDM]+\."             # ( c / d / i / l / m / v / x ) don't
	r")"                            # get stripped off real words like
	r"[ \t]+"                       # 'methods' / 'discussion' / etc.
)

def _classify_heading( heading_text ):
	"""Map a section-heading string to a canonical type. Unknown
	headings return their trimmed lowercase text ( so the downstream
	consumer still gets a stable key )."""
	if not heading_text:
		return "section"
	t = heading_text.strip().lower()
	t = _HEADING_PREFIX_RE.sub( "" , t , count=1 )
	t = re.sub( r"\s+" , " " , t ).strip( " .:-" )
	if not t:
		return "section"
	for canon , keywords in SECTION_KEYWORDS:
		for kw in keywords:
			if t == kw or t.startswith( kw + " " ) or t.startswith( kw + ":" ):
				return canon
	return t


_FIG_NUM_RE = re.compile( r"^\s*fig(?:ure)?\.?\s*([0-9]+[a-z]?)" , re.IGNORECASE )
_TAB_NUM_RE = re.compile( r"^\s*tab(?:le)?\.?\s*([0-9]+[a-z]?)"  , re.IGNORECASE )

def _figure_label( caption_text , fallback_n ):
	m = _FIG_NUM_RE.match( caption_text or "" )
	return f"Figure {m.group( 1 )}" if m else f"Figure {fallback_n}"

def _table_label( caption_text , fallback_n ):
	m = _TAB_NUM_RE.match( caption_text or "" )
	return f"Table {m.group( 1 )}" if m else f"Table {fallback_n}"


# ---------------------------------------------------------------------------
# Post-extract dedup : drop consecutive identical paragraphs ( duplicate
# YOLO detections that survived IoU dedupe ).
# ---------------------------------------------------------------------------

def _dedupe_block_lines( block ):
	"""Drop consecutive identical paragraphs inside a block. Cheap
	insurance against YOLO emitting two text boxes at slightly
	different offsets that both extract the same text."""
	seen = []
	last = None
	for line in block[ "lines" ]:
		key = re.sub( r"\s+" , " " , line.strip().lower() )
		if not key:
			continue
		if key == last:
			continue
		seen.append( line )
		last = key
	block[ "lines" ] = seen


def _dedupe_blocks( blocks ):
	"""Drop consecutive blocks that are byte-identical ( same type ,
	same lines ) , and clean per-block dup paragraphs."""
	cleaned = []
	for b in blocks:
		_dedupe_block_lines( b )
		if not b[ "lines" ]:
			continue
		if cleaned:
			prev = cleaned[ -1 ]
			if prev[ "type" ] == b[ "type" ] and prev[ "lines" ] == b[ "lines" ]:
				continue
		cleaned.append( b )
	return cleaned


# ---------------------------------------------------------------------------
# Top-level entry point.
# ---------------------------------------------------------------------------

def parse( pdf_path , yolo_path , force_ocr=False , max_pages=None ,
           engine=DEFAULT_ENGINE , lang="en" ):
	"""Parse pdf_path into a flat list of section blocks using the YOLO
	detections in yolo_path for layout + ordering.

	Output is strictly linear in reading order : every block's page
	index is >= the previous block's page index , and within a page
	blocks appear in column-aware top-to-bottom order. A section ( e.g.
	methods ) interrupted by a figure caption is emitted as multiple
	blocks of the same type ( methods part 1 -> Figure 1 -> methods
	part 2 ) rather than buffered and re-ordered.

	Args:
	  pdf_path  : PDF on disk.
	  yolo_path : matching <pdf>.yolo.json ( pages must be 1:1 ).
	  force_ocr : skip embedded-text extraction and OCR every bbox.
	              Useful for pdfs with garbled text layers.
	  max_pages : cap pages parsed ( default = all YOLO covered ).
	  engine    : 'paddle' ( default ) | 'surya' | 'tesseract'.
	              Falls back to tesseract automatically if the chosen
	              engine isn't installed or errors on a crop.
	  lang      : canonical language code 'en' / 'zh' / 'fr' / ... ;
	              mapped per-engine internally."""
	# MinerU is end-to-end : it does its own layout , OCR , formula and
	# table parsing , and emits a flat content_list. We bypass the YOLO
	# pipeline entirely here.
	if engine == ENGINE_MINERU:
		return _parse_with_mineru(
			Path( pdf_path ) , lang , max_pages=max_pages ,
		)

	yolo_data = utils.read_json( yolo_path )
	if not yolo_data:
		return []

	if isinstance( yolo_data , dict ):
		pages_yolo = yolo_data.get( "pages" , [] )
		yolo_dpi   = yolo_data.get( "meta" , {} ).get( "dpi" , PDF.DPI )
	else:
		pages_yolo = yolo_data
		yolo_dpi   = PDF.DPI
	if not pages_yolo:
		return []

	if max_pages is not None:
		pages_yolo = pages_yolo[ : max_pages ]

	if force_ocr:
		# Warm the lazy imports for the requested engine so a missing
		# install surfaces here ( instead of N pages in ). If neither
		# the engine nor tesseract is reachable , drop force_ocr so the
		# embedded-text path still produces output instead of nothing.
		ok = False
		if engine == ENGINE_RAPID    and _get_rapid()           is not None:
			ok = True
		if engine == ENGINE_PADDLE   and _get_paddle( lang )    is not None:
			ok = True
		if engine == ENGINE_SURYA    and _get_surya()[ 0 ]      is not None:
			ok = True
		if not ok and _find_tesseract():
			ok = True
		if not ok:
			print(
				"OCR :: --force-ocr requested but no OCR backend reachable ; "
				"falling back to embedded-text extraction."
			)
			force_ocr = False
		elif engine == ENGINE_PADDLE and not _PADDLE_WARMED[ 0 ]:
			print(
				"OCR :: PaddleOCR first-inference warmup can take 30-60s on CPU ; "
				"subsequent pages are much faster. Hang tight on page 1."
			)
			_PADDLE_WARMED[ 0 ] = True

	blocks = []
	open_block        = None      # { type , lines , page } currently growing
	last_heading_type = None      # most recent heading ( for continuation after figures )
	have_paper_title  = False
	n_figure          = 0
	n_table           = 0

	def flush():
		nonlocal open_block
		if open_block and open_block[ "lines" ]:
			blocks.append( open_block )
		open_block = None

	def open_section( section_type , page_idx ):
		nonlocal open_block
		open_block = { "type": section_type , "lines": [] , "page": page_idx }

	pdf = pdfium.PdfDocument( str( pdf_path ) )
	try:
		n_pdf_pages = len( pdf )
		pages_iter = tqdm(
			list( enumerate( pages_yolo ) ) ,
			desc=f"  {pdf_path.stem[ :30 ]}" ,
			position=2 , leave=False , unit="pg" ,
		)
		for page_idx , page_dets in pages_iter:
			if page_idx >= n_pdf_pages:
				break
			page = pdf[ page_idx ]
			page_w_pt = page.get_width()
			page_h_pt = page.get_height()
			page_w_px = page_w_pt * ( yolo_dpi / 72.0 )

			dets = _preprocess_detections( page_dets )
			ordered = _reading_order( dets , page_w_px )
			if not ordered:
				page.close()
				continue

			# Lazy page-level OCR : run the engine on the FULL page once
			# the first time we need it , then filter its text lines
			# into each YOLO bbox by spatial containment. This is the
			# perf-critical change vs the previous per-bbox approach.
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
				for det in ordered:
					cls  = det[ "type" ]
					raw  = _extract_text(
						det , textpage , page_h_pt , yolo_dpi ,
						get_ocr_lines , force_ocr ,
					)
					raw  = _normalize_text( raw )
					if not raw:
						continue
					paras = _to_paragraphs( raw )
					if not paras:
						continue

					if cls == CLS_TITLE:
						flush()
						if not have_paper_title:
							blocks.append( {
								"type":  "title" ,
								"lines": paras ,
								"page":  page_idx ,
							} )
							have_paper_title = True
							continue
						heading_text = " ".join( paras )
						last_heading_type = _classify_heading( heading_text )
						open_section( last_heading_type , page_idx )
						continue

					if cls == CLS_FIG_CAPTION:
						flush()
						n_figure += 1
						label = _figure_label( paras[ 0 ] , n_figure )
						blocks.append( {
							"type":  label ,
							"lines": paras ,
							"page":  page_idx ,
						} )
						continue

					if cls in ( CLS_TAB_CAPTION , CLS_TAB_FOOTNOTE ):
						# A footnote following a table on the same page
						# folds into that table block.
						if (
							cls == CLS_TAB_FOOTNOTE
							and blocks
							and blocks[ -1 ][ "type" ].startswith( "Table " )
							and blocks[ -1 ][ "page" ] == page_idx
							and open_block is None
						):
							blocks[ -1 ][ "lines" ].extend( paras )
							continue
						flush()
						n_table += 1
						label = _table_label( paras[ 0 ] , n_table )
						blocks.append( {
							"type":  label ,
							"lines": paras ,
							"page":  page_idx ,
						} )
						continue

					if cls in ( CLS_FORMULA , CLS_FORM_CAPTION ):
						if open_block is not None:
							open_block[ "lines" ].extend( paras )
						continue

					if cls == CLS_TEXT:
						if open_block is None:
							if last_heading_type:
								section_type = last_heading_type
							elif not have_paper_title or page_idx <= 1:
								section_type = "abstract"
								last_heading_type = "abstract"
							else:
								section_type = "body"
							open_section( section_type , page_idx )
						open_block[ "lines" ].extend( paras )
						continue
			finally:
				if textpage is not None:
					textpage.close()
				page.close()
	finally:
		try:
			pdf.close()
		except Exception:
			pass

	flush()
	return _dedupe_blocks( blocks )


def parse_to_json( pdf_path , yolo_path , out_path , force_ocr=False ,
                   max_pages=None , engine=DEFAULT_ENGINE , lang="en" ):
	"""Convenience wrapper : parse() then write JSON to out_path."""
	blocks = parse(
		pdf_path , yolo_path ,
		force_ocr=force_ocr , max_pages=max_pages ,
		engine=engine , lang=lang ,
	)
	utils.write_json( out_path , blocks )
	return blocks
