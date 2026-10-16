#!/usr/bin/env python3
"""
tools/ocr-eval.py

Old-vs-new comparison for the OCR stage. READ-ONLY : it never writes a
paper record , an .md , or anything else outside output/cache/ocr-eval/.

For each selected paper it re-runs the CURRENT src/pdf/ocr.ocr_paper()
( force , in memory , on a copy ) plus the current reading-order pass
( preprocess_page , which drops duplicate YOLO boxes ) , and scores both
the STORED text and the NEW text against the PDF's own full-page text
layer ( pdfium get_text_range ) :

  precision : share of extracted words that exist on that page's text
              layer -- catches truncated ( 'featu' ) , garbled
              ( 'lfddhhbh' ) and wrongly split ( 'perce ptr on' ) words ;
  recall    : share of the page's text-layer words the extraction covers
              ( multiset ) -- guards against the duplicate filter , or
              anything else , LOSING text. Never 1.0 : the reference also
              holds headers , table cells and figure labels ;
  dup       : share of repeated 6-word shingles across the paper ;
  ocr_pages : pages the new run had to send to the OCR engine.

Only pages whose text layer looks real ( >= 30 words , not garbled ) are
scored ; scanned pages are skipped. A paper is FLAGGED when new precision
or recall is more than 2 points below old -- read those by hand before
rolling anything out.

Usage :
  venv/bin/python tools/ocr-eval.py --affected
  venv/bin/python tools/ocr-eval.py --sample 300 --seed 1
  venv/bin/python tools/ocr-eval.py --keys 10.1142/S0129156425403857
  venv/bin/python tools/ocr-eval.py --affected --rescan   # re-read page geometry

Writes output/cache/ocr-eval/<label>-<timestamp>.csv ( one row per paper ).
"""

import argparse
import copy
import csv
import re
import sys
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert( 0 , str( Path( __file__ ).resolve().parent ) )
import ocr_common as C

import pypdfium2 as pdfium

from src.db    import papers
from src.pdf   import ocr        as OCR
from src.pdf   import preprocess as PP
from src.utils import utils

FLAG_DROP     = 0.02
MIN_REF_WORDS = 30
SHINGLE       = 6

_WORD_RE   = re.compile( r"[a-z]{3,}" )
_HYPHEN_RE = re.compile( r"(\w)-\s*\n\s*(\w)" )


def _words( text ):
	text = _HYPHEN_RE.sub( r"\1\2" , OCR._normalize_text( text or "" ) )
	return _WORD_RE.findall( text.lower() )


def _dup_rate( words ):
	sh = [ tuple( words[ i:i + SHINGLE ] ) for i in range( len( words ) - SHINGLE + 1 ) ]
	if not sh:
		return 0.0
	return 1.0 - len( set( sh ) ) / len( sh )


def _reference_pages( pdf_path , n ):
	"""Per-page text-layer words , or None for a page that has no usable
	text layer ( scanned / CID garbage )."""
	out = []
	pdf = pdfium.PdfDocument( pdf_path )
	try:
		for i in range( n ):
			if i >= len( pdf ):
				out.append( None )
				continue
			pg = pdf[ i ]
			tp = pg.get_textpage()
			try:
				raw = tp.get_text_range( force_this=True ) or ""
			finally:
				tp.close()
				pg.close()
			w = _words( raw )
			ok = len( w ) >= MIN_REF_WORDS and not OCR._is_garbled( OCR._normalize_text( raw ) )
			out.append( w if ok else None )
	finally:
		pdf.close()
	return out


def _score( pages_text , ref ):
	"""( precision , recall , n_pages_scored ) over pages with a reference."""
	hit = tot = cov = ref_tot = n = 0
	for text , rw in zip( pages_text , ref ):
		if rw is None:
			continue
		n += 1
		ew = _words( text )
		rset , rc , ec = set( rw ) , Counter( rw ) , Counter( ew )
		hit += sum( 1 for w in ew if w in rset )
		tot += len( ew )
		cov += sum( min( c , ec[ w ] ) for w , c in rc.items() )
		ref_tot += len( rw )
	if not n:
		return None , None , 0
	return ( hit / tot if tot else 0.0 ) , ( cov / ref_tot if ref_tot else 0.0 ) , n


_OCR_CALLS = [ 0 ]

def _counting_ocr_page( *a , **k ):
	_OCR_CALLS[ 0 ] += 1
	return _ORIG_OCR_PAGE( *a , **k )

_ORIG_OCR_PAGE = OCR._ocr_page
OCR._ocr_page  = _counting_ocr_page


def _eval_one( job ):
	output , key = job
	args  = argparse.Namespace( output=Path( output ) )
	paper = papers.load( args , key )
	row   = { "key": key }
	if not paper:
		row[ "error" ] = "no record"
		return row
	pdf_path = paper.get( "pdf_path" )
	pages    = ( paper.get( "yolo" ) or {} ).get( "pages" ) or []
	if not pdf_path or not pages:
		row[ "error" ] = "no pdf / yolo"
		return row
	try:
		old_text = C.reading_pages( paper )
		new = copy.deepcopy( paper )
		_OCR_CALLS[ 0 ] = 0
		OCR.ocr_paper( pdf_path , new[ "yolo" ] , force=True )
		ordered  = [ PP.preprocess_page( p ) for p in new[ "yolo" ][ "pages" ] ]
		new_text = C.reading_pages( new , ordered )
		ref = _reference_pages( pdf_path , len( pages ) )
	except Exception as e:
		row[ "error" ] = f"{type( e ).__name__}: {e}"
		return row
	op , orc , n = _score( old_text , ref )
	np_ , nrc , _ = _score( new_text , ref )
	row.update( {
		"pages": len( pages ) , "pages_scored": n , "ocr_pages": _OCR_CALLS[ 0 ] ,
		"old_prec": op , "new_prec": np_ , "old_rec": orc , "new_rec": nrc ,
		"old_dup": _dup_rate( _words( "\n".join( old_text ) ) ) ,
		"new_dup": _dup_rate( _words( "\n".join( new_text ) ) ) ,
	} )
	row[ "flag" ] = bool( n and (
		np_ < op - FLAG_DROP or nrc < orc - FLAG_DROP
	) )
	return row


def _mean( rows , col ):
	v = [ r[ col ] for r in rows if r.get( col ) is not None ]
	return sum( v ) / len( v ) if v else float( "nan" )


def main():
	ap = argparse.ArgumentParser(
		description=__doc__ , formatter_class=argparse.RawDescriptionHelpFormatter ,
	)
	ap.add_argument( "--output" , type=Path , default=C.DEFAULT_OUTPUT )
	sel = ap.add_mutually_exclusive_group( required=True )
	sel.add_argument( "--affected" , action="store_true" ,
		help="PDFs with a CropBox offset or rotated pages" )
	sel.add_argument( "--sample" , type=int , metavar="N" ,
		help="N random papers ( not in the affected set )" )
	sel.add_argument( "--keys" , nargs="+" , metavar="KEY" )
	ap.add_argument( "--seed" , type=int , default=1 )
	ap.add_argument( "--rescan" , action="store_true" ,
		help="Re-read page geometry instead of using the cache" )
	ap.add_argument( "--workers" , type=int , default=C.WORKERS )
	args = ap.parse_args()

	if args.keys:
		keys , label = args.keys , "keys"
	else:
		geo = C.scan_library( args.output , rescan=args.rescan )
		if args.affected:
			keys , label = C.affected_keys( geo ) , "affected"
		else:
			keys  = C.sample_keys( geo , args.sample , args.seed , exclude=C.affected_keys( geo ) )
			label = f"sample{args.sample}-seed{args.seed}"

	print( f"eval :: {len( keys )} papers ( {label} , {args.workers} workers )" )
	t0 , rows = time.time() , []
	with ProcessPoolExecutor( max_workers=args.workers ) as ex:
		for i , row in enumerate( ex.map( _eval_one , [ ( str( args.output ) , k ) for k in keys ] ) , 1 ):
			rows.append( row )
			if i % 25 == 0:
				print( f"eval :: {i}/{len( keys )} ( {time.time() - t0:.0f}s )" )

	out_dir = args.output / "cache" / "ocr-eval"
	out_dir.mkdir( parents=True , exist_ok=True )
	out_csv = out_dir / f"{label}-{time.strftime( '%Y%m%d-%H%M%S' )}.csv"
	cols = [ "key" , "pages" , "pages_scored" , "ocr_pages" ,
	         "old_prec" , "new_prec" , "old_rec" , "new_rec" ,
	         "old_dup" , "new_dup" , "flag" , "error" ]
	with out_csv.open( "w" , newline="" ) as f:
		w = csv.DictWriter( f , fieldnames=cols , extrasaction="ignore" )
		w.writeheader()
		for r in rows:
			w.writerow( { k: ( f"{v:.4f}" if isinstance( v , float ) else v ) for k , v in r.items() } )

	scored  = [ r for r in rows if r.get( "pages_scored" ) ]
	errors  = [ r for r in rows if r.get( "error" ) ]
	flagged = [ r for r in scored if r.get( "flag" ) ]
	print()
	print( f"papers {len( rows )}  scored {len( scored )}  errors {len( errors )}  "
	       f"( {time.time() - t0:.0f}s )" )
	for col , name in ( ( "prec" , "precision" ) , ( "rec" , "recall" ) , ( "dup" , "duplication" ) ):
		print( f"  {name:<12} old {_mean( scored , 'old_' + col ):.4f}   new {_mean( scored , 'new_' + col ):.4f}" )
	print( f"  ocr pages    new {sum( r.get( 'ocr_pages' ) or 0 for r in rows )} "
	       f"of {sum( r.get( 'pages' ) or 0 for r in rows )}" )
	print( f"  flagged ( new > {FLAG_DROP:.0%} worse on precision or recall ) : {len( flagged )}" )
	for r in flagged:
		print( f"    {r[ 'key' ]}  prec {r[ 'old_prec' ]:.3f}->{r[ 'new_prec' ]:.3f}  "
		       f"rec {r[ 'old_rec' ]:.3f}->{r[ 'new_rec' ]:.3f}" )
	for r in errors[ :20 ]:
		print( f"    ERROR {r[ 'key' ]} : {r[ 'error' ]}" )
	print( f"-> {out_csv}" )


if __name__ == "__main__":
	main()
