#!/usr/bin/env python3
"""
tools/reocr.py

Rebuild existing papers' text with the CURRENT OCR stage ( OCR_VERSION in
src/pdf/ocr.py ) -- without re-running YOLO , re-cropping figures , or
touching the LLM summaries.

  1. BACKUP : every touched output/cache/papers/<k>.json plus its
     output/{md,methods,text}/<k>.* goes into
     output/cache/backups/reocr-<timestamp>.tar.gz first.
  2. RE-EXTRACT : ocr_paper( force=True ) on the existing YOLO boxes ( in
     parallel ) ; the record is re-read right before it's written so a
     concurrent writer ( the server's --watch worker ) isn't clobbered.
     Drops 'yolo_sorted_page_indexes' + 'sections' and unlinks the
     paper's md / methods / text so they rebuild. --affected ( or
     --reharvest ) also drops 'code' / 'datasets' / 'modalities' : those
     were harvested from the garbled text and should be read again.
  3. REBUILD : the ordinary per-paper suite ( process.run_suite ) in
     batches -- every stage skips what's still on disk ( yolo , ocr ,
     images ) and redoes what step 2 removed. Then ` prma text ` ( ocr
     source ) for papers that HAD a .txt , then one dashboard reindex +
     figure-report refresh.

Never touches output/summaries/. For --affected it writes the keys whose
summaries were generated from the bad text to
output/cache/reocr-stale-summaries.txt so you can decide about them.

Usage :
  venv/bin/python tools/reocr.py --affected --dry-run
  venv/bin/python tools/reocr.py --affected
  venv/bin/python tools/reocr.py --all              # every paper below OCR_VERSION
  venv/bin/python tools/reocr.py --keys 10.1142/S0129156425403857
  venv/bin/python -u tools/reocr.py --all --remote modal   # OCR pages on Modal
"""

import argparse
import copy
import os
import sys
import tarfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert( 0 , str( Path( __file__ ).resolve().parent ) )
import ocr_common as C

from src.db    import papers
from src.pdf   import ocr as OCR
from src.utils import utils

# Derived from the OCR text : always rebuilt.
DROP_FIELDS = ( "yolo_sorted_page_indexes" , "sections" )
# Harvested from the text : rebuilt for papers whose text was garbage.
HARVEST_FIELDS = ( "code" , "datasets" , "modalities" )
# Per-paper files rendered from the OCR text.
TEXT_FILES = ( ( "md" , ".md" ) , ( "methods" , ".txt" ) , ( "text" , ".txt" ) )


def _files_for( output , key ):
	prefix = utils.doi_to_filename( key )
	return [ Path( output ) / sub / f"{prefix}{ext}" for sub , ext in TEXT_FILES ]


def _summaries_for( output , key ):
	prefix = utils.doi_to_filename( key )
	root = Path( output ) / "summaries"
	if not root.is_dir():
		return []
	return [ d / f"{prefix}.md" for d in root.iterdir() if ( d / f"{prefix}.md" ).exists() ]


def _backup( output , keys ):
	out = Path( output ) / "cache" / "backups" / f"reocr-{time.strftime( '%Y%m%d-%H%M%S' )}.tar.gz"
	out.parent.mkdir( parents=True , exist_ok=True )
	args = argparse.Namespace( output=Path( output ) )
	n = 0
	with tarfile.open( out , "w:gz" ) as tar:
		for key in keys:
			for p in [ papers.paper_path( args , key ) ] + _files_for( output , key ):
				if p.exists():
					tar.add( p , arcname=str( p.relative_to( output ) ) )
					n += 1
	print( f"reocr :: backed up {n} files -> {out}" )
	return out


def _write_back( output , key , new_yolo , harvest ):
	"""Pin re-extracted yolo onto the record and clear what derives from it.
	Re-reads the record first so a concurrent writer ( the server's --watch
	worker ) isn't clobbered. Returns had_text_file."""
	args  = argparse.Namespace( output=Path( output ) )
	fresh = papers.load( args , key )
	if fresh is None:
		return False
	fresh[ "yolo" ] = new_yolo
	for f in DROP_FIELDS + ( HARVEST_FIELDS if harvest else () ):
		fresh.pop( f , None )
	papers.save( args , fresh )
	md_p , methods_p , text_p = _files_for( output , key )
	had_text = text_p.exists()
	for p in ( md_p , methods_p , text_p ):
		try:
			p.unlink()
		except FileNotFoundError:
			pass
	return had_text


def _reocr_one( job ):
	"""Re-extract one paper's text. Returns ( key , status , had_text_file ).

	remote=True : run the embedded-text pass with the OCR engine stubbed out.
	A paper whose every box read cleanly off the text layer is finished right
	here ; one that asked for OCR comes back as status 'remote' , untouched ,
	for Modal to redo."""
	output , key , harvest , remote , skip_current = job
	args  = argparse.Namespace( output=Path( output ) )
	paper = papers.load( args , key )
	if not paper:
		return key , "no record" , False
	pdf_path = paper.get( "pdf_path" )
	yolo     = paper.get( "yolo" ) or {}
	# --all reads ocr_version off the geometry cache , which can predate an
	# earlier reocr pass ; the record itself is the truth.
	if skip_current and ( yolo.get( "meta" ) or {} ).get( "ocr_version" , 1 ) >= OCR.OCR_VERSION:
		return key , "current" , False
	if not pdf_path or not Path( pdf_path ).exists() or not yolo.get( "pages" ):
		return key , "no pdf / yolo" , False
	new_yolo = copy.deepcopy( yolo )
	asked = [ 0 ]
	if remote:
		def stub( *a , **k ):
			asked[ 0 ] += 1
			return []
		OCR._ocr_page = stub
	try:
		OCR.ocr_paper( pdf_path , new_yolo , force=True )
	except Exception as e:
		return key , f"ocr failed ( {e} )" , False
	if asked[ 0 ]:
		return key , "remote" , False
	return key , "ok" , _write_back( output , key , new_yolo , harvest )


def _progress( label , i , n , t0 ):
	el = time.time() - t0
	eta = el / i * ( n - i ) if i else 0
	print( f"reocr :: {label} {i}/{n}  elapsed {el / 60:.1f}m  eta {eta / 60:.1f}m" , flush=True )


def _run_remote( output , keys , harvest , flavour , t0 ):
	"""Ship the papers that need OCR to Modal ( tools/modal_ocr.py ) and
	write each result back as it lands. Returns ( ok , had_text , failed )."""
	import modal_ocr
	fn = modal_ocr.ocr_gpu if flavour == "gpu" else modal_ocr.ocr_cpu
	args = argparse.Namespace( output=Path( output ) )

	def inputs():
		for key in keys:
			p = papers.load( args , key )
			yield ( Path( p[ "pdf_path" ] ).read_bytes() , p[ "yolo" ] , key )

	ok , had_text , failed = [] , [] , []
	with modal_ocr.app.run():
		results = fn.starmap( inputs() , order_outputs=False , return_exceptions=True )
		for i , res in enumerate( results , 1 ):
			new_yolo , info = res if not isinstance( res , BaseException ) else ( None , { "key": "?" , "error": str( res ) } )
			key = info.get( "key" , "?" )
			if new_yolo is None:
				failed.append( ( key , f"remote failed ( {info.get( 'error' )} )" ) )
			else:
				ok.append( key )
				if _write_back( output , key , new_yolo , harvest ):
					had_text.append( key )
			if i % 10 == 0 or i == len( keys ):
				_progress( f"modal ( {flavour} )" , i , len( keys ) , t0 )
	return ok , had_text , failed


def _pipeline_args( output ):
	"""The namespace ` prma process ` would get , so the stage modules see
	every default they read. --manager all : run_suite scopes by key , and
	a Mendeley-only paper must not be skipped by the default zotero filter."""
	# The package re-exports cli() under the module's own name , so go
	# through importlib to get the module.
	import importlib
	CLI = importlib.import_module( "src.cli.cli" )
	g = CLI.global_parser()
	parser = argparse.ArgumentParser( parents=[ g ] , add_help=False )
	sub = parser.add_subparsers( dest="command" )
	top = {}
	for reg in CLI.REGISTRARS:
		top.update( reg( sub , g ) or {} )
	parser.set_defaults( **top )
	repo = C._REPO
	return parser.parse_args( [
		"process" , "-" , "--skip-snapshot" , "--manager" , "all" ,
		"--output" , str( output ) , "--config" , str( repo / "config" ) ,
		"--searches" , str( repo / "searches" ) ,
	] )


def main():
	ap = argparse.ArgumentParser(
		description=__doc__ , formatter_class=argparse.RawDescriptionHelpFormatter ,
	)
	ap.add_argument( "--output" , type=Path , default=C.DEFAULT_OUTPUT )
	sel = ap.add_mutually_exclusive_group( required=True )
	sel.add_argument( "--affected" , action="store_true" ,
		help="PDFs with a CropBox offset or rotated pages ( implies --reharvest )" )
	sel.add_argument( "--all" , action="store_true" ,
		help=f"Every paper whose text predates OCR_VERSION {OCR.OCR_VERSION}" )
	sel.add_argument( "--keys" , nargs="+" , metavar="KEY" )
	ap.add_argument( "--reharvest" , action="store_true" ,
		help="Also rebuild code / datasets / modalities from the new text" )
	ap.add_argument( "--dry-run" , action="store_true" )
	ap.add_argument( "--batch" , type=int , default=200 ,
		help="Papers per run_suite batch ( default 200 )" )
	ap.add_argument( "--workers" , type=int , default=C.WORKERS )
	ap.add_argument( "--rescan" , action="store_true" ,
		help="Re-read page geometry instead of using the cache" )
	ap.add_argument( "--remote" , choices=[ "modal" ] , default=None ,
		help="Send papers that need real OCR to Modal ( tools/modal_ocr.py ) ; "
		     "papers whose text layer is clean still finish locally" )
	ap.add_argument( "--flavour" , choices=[ "cpu" , "gpu" ] , default="cpu" ,
		help="Modal container type for --remote ( default cpu )" )
	opts = ap.parse_args()
	os.chdir( C._REPO )   # the stage modules resolve models / config off cwd
	output = opts.output.resolve()

	if opts.keys:
		args0 = argparse.Namespace( output=output )
		keys  = [ papers.record_key( p ) for p in ( papers.load( args0 , k ) for k in opts.keys ) if p ]
		label = "keys"
	else:
		geo = C.scan_library( output , rescan=opts.rescan )
		if opts.affected:
			keys , label = C.affected_keys( geo ) , "affected"
		else:
			keys , label = C.stale_keys( geo ) , "all"
	harvest = opts.affected or opts.reharvest

	stale_summ = [ k for k in keys if _summaries_for( output , k ) ] if harvest else []
	print(
		f"reocr :: {len( keys )} papers ( {label} ) ; "
		f"reharvest code/datasets/modalities : {'yes' if harvest else 'no'} ; "
		f"papers with summaries built on the old text : {len( stale_summ )}"
	)
	if opts.dry_run:
		for k in keys[ :40 ]:
			print( f"  {k}" )
		if len( keys ) > 40:
			print( f"  ... +{len( keys ) - 40} more" )
		return
	if not keys:
		return

	t0 = time.time()
	_backup( output , keys )

	remote = bool( opts.remote )
	print( f"reocr :: re-extracting text ( {opts.workers} workers"
	       f"{' , OCR pages -> modal ' + opts.flavour if remote else ''} ) ..." , flush=True )
	ok , had_text , failed , needs_ocr = [] , [] , [] , []
	with ProcessPoolExecutor( max_workers=opts.workers ) as ex:
		jobs = [ ( str( output ) , k , harvest , remote , label == "all" ) for k in keys ]
		for i , ( key , status , txt ) in enumerate( ex.map( _reocr_one , jobs ) , 1 ):
			if status == "ok":
				ok.append( key )
				if txt:
					had_text.append( key )
			elif status == "remote":
				needs_ocr.append( key )
			elif status == "current":
				pass
			else:
				failed.append( ( key , status ) )
			if i % 25 == 0 or i == len( keys ):
				_progress( "local" , i , len( keys ) , t0 )
	if needs_ocr:
		print( f"reocr :: {len( needs_ocr )} papers need real OCR -> modal" , flush=True )
		r_ok , r_txt , r_failed = _run_remote( output , needs_ocr , harvest , opts.flavour , time.time() )
		ok += r_ok ; had_text += r_txt ; failed += r_failed
	print( f"reocr :: re-extracted {len( ok )} , failed {len( failed )}" , flush=True )
	for key , status in failed[ :30 ]:
		print( f"    {key} : {status}" )

	from src.tasks import process as process_task
	from src.tasks import text as text_task
	from src.dashboard import indexer
	args = _pipeline_args( output )
	for i in range( 0 , len( ok ) , opts.batch ):
		batch = ok[ i:i + opts.batch ]
		print( f"reocr :: rebuilding {i + 1}-{i + len( batch )} of {len( ok )} ( {time.time() - t0:.0f}s )" )
		process_task.run_suite( args , batch , reindex=False )

	if had_text:
		print( f"reocr :: re-rendering {len( had_text )} text files" )
		args.text_source , args.only_keys = "ocr" , set( had_text )
		try:
			text_task.run( args )
		finally:
			args.only_keys = None

	print( "reocr :: reindex + figure reports" )
	indexer.build( args )
	process_task.refresh_reports( args )

	if stale_summ:
		p = output / "cache" / "reocr-stale-summaries.txt"
		p.write_text( "\n".join( stale_summ ) + "\n" )
		print( f"reocr :: {len( stale_summ )} papers have summaries built on the old text -> {p}" )
	print( f"reocr :: done ( {time.time() - t0:.0f}s )" )


if __name__ == "__main__":
	main()
