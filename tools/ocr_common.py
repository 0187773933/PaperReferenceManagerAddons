"""
tools/ocr_common.py

Shared by tools/ocr-eval.py and tools/reocr.py :

  - paper selection : --affected ( PDFs whose CropBox doesn't start at
    0,0 or that have rotated pages -- the ones the pre-v2 bbox mapping
    garbled ) , --sample N , --all ( below the current OCR_VERSION ) ,
    --keys ;
  - reading_pages() : a paper's text dets as per-page reading-order text ,
    the thing both the eval and a human reading /md/<doi> actually see.

Imported as a sibling module ( Python puts the running script's dir on
sys.path ) , so it is not a CLI of its own.
"""

import json
import os
import random
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

_REPO = Path( __file__ ).resolve().parent.parent
if str( _REPO ) not in sys.path:
	sys.path.insert( 0 , str( _REPO ) )

import pypdfium2 as pdfium

from src.db    import papers
from src.pdf   import ocr        as OCR
from src.pdf   import preprocess as PP
from src.utils import utils

DEFAULT_OUTPUT = _REPO / "output"
WORKERS = max( 1 , min( 8 , ( os.cpu_count() or 2 ) - 1 ) )


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def _scan_one( path_str ):
	"""( key , info ) for one record file , or ( key , None ) when it has no
	YOLO pages / no PDF on disk. info = { crop , rot , ocr_version }."""
	try:
		paper = utils.read_json( Path( path_str ) )
	except Exception:
		return None , None
	key  = papers.record_key( paper )
	yolo = paper.get( "yolo" ) or {}
	pages = yolo.get( "pages" ) or []
	pdf_path = paper.get( "pdf_path" )
	if not key or not pages or not pdf_path or not os.path.exists( pdf_path ):
		return key , None
	info = {
		"crop": False , "rot": False ,
		"ocr_version": ( yolo.get( "meta" ) or {} ).get( "ocr_version" , 1 ) ,
	}
	try:
		pdf = pdfium.PdfDocument( pdf_path )
	except Exception:
		return key , None
	try:
		for i in range( min( len( pdf ) , len( pages ) ) ):
			pg = pdf[ i ]
			try:
				left , bottom , _r , _t = pg.get_cropbox()
				if abs( left ) > 0.5 or abs( bottom ) > 0.5:
					info[ "crop" ] = True
				if pg.get_rotation():
					info[ "rot" ] = True
			finally:
				pg.close()
	finally:
		pdf.close()
	return key , info


def scan_library( output , rescan=False ):
	"""{ key : { crop , rot , ocr_version } } for every paper with YOLO pages
	and a PDF on disk. Cached at output/cache/ocr-eval/geometry.json ( the
	page geometry of a PDF doesn't change ) ; ocr_version is re-read from the
	records each time since reocr moves it."""
	cache = Path( output ) / "cache" / "ocr-eval" / "geometry.json"
	files = sorted( str( p ) for p in ( Path( output ) / "cache" / "papers" ).glob( "*.json" ) )
	if cache.exists() and not rescan:
		try:
			geo = json.loads( cache.read_text() )
			if geo.get( "n_files" ) == len( files ):
				return geo[ "papers" ]
		except Exception:
			pass
	t0 = time.time()
	print( f"scan :: reading page geometry of {len( files )} records ( {WORKERS} workers ) ..." )
	out = {}
	with ProcessPoolExecutor( max_workers=WORKERS ) as ex:
		for key , info in ex.map( _scan_one , files , chunksize=16 ):
			if key and info:
				out[ key ] = info
	cache.parent.mkdir( parents=True , exist_ok=True )
	cache.write_text( json.dumps( { "n_files": len( files ) , "papers": out } ) )
	print( f"scan :: {len( out )} papers with yolo + pdf ( {time.time() - t0:.0f}s )" )
	return out


def affected_keys( geo ):
	return sorted( k for k , v in geo.items() if v[ "crop" ] or v[ "rot" ] )


def stale_keys( geo ):
	return sorted( k for k , v in geo.items() if ( v.get( "ocr_version" ) or 1 ) < OCR.OCR_VERSION )


def sample_keys( geo , n , seed , exclude=() ):
	pool = sorted( set( geo ) - set( exclude ) )
	random.Random( seed ).shuffle( pool )
	return pool[ :n ]


# ---------------------------------------------------------------------------
# Reading-order text
# ---------------------------------------------------------------------------

def reading_pages( paper , ordered=None ):
	"""Per-page list of reading-order text for a paper's text dets. Uses
	`ordered` ( a yolo_sorted_page_indexes ) or the stored one ; a page with
	no stored order falls back to detection order minus page furniture."""
	pages = ( paper.get( "yolo" ) or {} ).get( "pages" ) or []
	if ordered is None:
		ordered = paper.get( "yolo_sorted_page_indexes" ) or []
	out = []
	for pi , dets in enumerate( pages ):
		idxs = ordered[ pi ] if pi < len( ordered ) else range( len( dets ) )
		parts = []
		for di in idxs:
			d = dets[ di ] if 0 <= di < len( dets ) else None
			if isinstance( d , dict ) and d.get( "type" ) in PP.TEXT_CLASSES:
				t = PP._text_for_det( d )
				if t:
					parts.append( t )
		out.append( "\n".join( parts ) )
	return out
