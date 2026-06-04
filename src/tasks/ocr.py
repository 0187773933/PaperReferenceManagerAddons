"""
prma ocr : iterate the unified papers/ DB and pin OCR text onto every
YOLO detection of a text-bearing type.

Honors --manager :
  --manager zotero   ( default ) -> papers with a 'zotero'   source
  --manager mendeley             -> papers with a 'mendeley' source
  --manager all                  -> every paper in the DB

For each paper :
  - skip if no PDF is on disk
  - if paper has no 'yolo' field , run YOLO inline ( same call
    as ` prma yolo ` would make ) and save it onto the paper record
  - call ocr.ocr_paper() which mutates paper[ 'yolo' ][ 'pages' ]
    so each allowed detection gains det[ 'ocr' ][ engine ] = '...text...'
  - save the paper back

Idempotent : a detection that already has det[ 'ocr' ][ engine ] is
skipped unless --force is passed. Use --force-ocr to skip the embedded-
text path and OCR every bbox even when a text layer exists ( useful for
PDFs with garbled text layers ).
"""

from pathlib import Path
from tqdm import tqdm

from ..db import papers
from ..utils import utils


# Same manager-filter helpers used by yolo + images , kept duplicated
# rather than imported so each task module stays self-contained.

def _resolve_managers( args ):
	name = ( getattr( args , "manager" , None ) or "zotero" ).lower()
	if getattr( args , "mendeley" , False ):
		name = "mendeley"
	elif getattr( args , "zotero" , False ):
		name = "zotero"
	if name == "all":
		return ( papers.SOURCE_ZOTERO , papers.SOURCE_MENDELEY )
	return ( name , )


def _paper_matches_managers( paper , managers ):
	if not managers:
		return True
	srcs = paper.get( "sources" ) or {}
	return any( m in srcs for m in managers )


def _paper_fully_ocrd( paper , engine ):
	"""True if every text-bearing detection already has det[ 'ocr' ][ engine ]
	set. Used as a cheap skip check ; --force overrides."""
	from ..pdf import ocr as ocr_mod
	yolo = paper.get( "yolo" ) or {}
	pages = yolo.get( "pages" ) or []
	any_text = False
	for page in pages:
		for det in page:
			if not isinstance( det , dict ):
				continue
			if det.get( "type" ) not in ocr_mod.TEXT_DETECTION_CLASSES:
				continue
			any_text = True
			if not ( det.get( "ocr" ) or {} ).get( engine ):
				return False
	return any_text   # if there were no text dets at all , treat as not-done


def run( args ):
	from ..pdf import pdf as pdf_mod
	from ..pdf import ocr as ocr_mod

	engine    = getattr( args , "ocr_engine" , ocr_mod.DEFAULT_ENGINE )
	lang      = getattr( args , "ocr_lang"   , "en" )
	force_ocr = getattr( args , "ocr_force"  , False )
	force     = getattr( args , "ocr_force_recompute" , False )
	max_pages = getattr( args , "ocr_max_pages" , None )
	do_deskew = getattr( args , "pdf_deskew" , False )
	managers  = _resolve_managers( args )
	manager_label = " + ".join( managers ) if managers else "all"

	# Plan the work.
	jobs = []
	skip_no_pdf , skip_missing , skip_done , skip_other = 0 , 0 , 0 , 0
	needs_yolo = 0
	for doi , paper in papers.iter_all( args ):
		if not _paper_matches_managers( paper , managers ):
			skip_other += 1
			continue
		if not force and _paper_fully_ocrd( paper , engine ):
			skip_done += 1
			continue
		pdf_path = paper.get( "pdf_path" )
		if not pdf_path:
			skip_no_pdf += 1
			continue
		p = Path( pdf_path )
		if not p.exists():
			skip_missing += 1
			continue
		yolo_data = paper.get( "yolo" )
		if not yolo_data or not yolo_data.get( "pages" ):
			needs_yolo += 1
		jobs.append( ( doi , p ) )

	print(
		f"OCR :: ({manager_label} , engine={engine} , lang={lang})  "
		f"{len(jobs)} pdfs to process -> papers/ "
		f"( will run YOLO inline for {needs_yolo} ; "
		f"skipped: no-pdf={skip_no_pdf} not-on-disk={skip_missing} "
		f"already-done={skip_done} other-manager={skip_other} )"
	)

	outer = tqdm( jobs , desc="PDFs" , position=1 , leave=True , unit="pdf" )
	total_pinned = 0
	for doi , pdf_path in outer:
		outer.set_postfix_str( pdf_path.name[ :60 ] )

		# Reload to avoid clobbering concurrent updates.
		paper = papers.load( args , doi )
		if paper is None:
			continue

		# Run YOLO inline if we don't have it yet , and persist so future
		# tasks reuse it.
		yolo_data = paper.get( "yolo" )
		if not yolo_data or not yolo_data.get( "pages" ):
			try:
				yolo_data = pdf_mod.yolo( pdf_path , do_deskew=do_deskew )
			except Exception as e:
				print( f"OCR :: {pdf_path.name} : YOLO failed ( {e} )" )
				continue
			paper[ "yolo" ] = yolo_data

		try:
			n = ocr_mod.ocr_paper(
				pdf_path , paper[ "yolo" ] ,
				engine=engine , lang=lang ,
				force_ocr=force_ocr , force=force ,
				max_pages=max_pages ,
			)
		except Exception as e:
			print( f"OCR :: {pdf_path.name} : failed ( {e} )" )
			continue

		total_pinned += n
		# yolo_data was mutated in place -- save it back.
		papers.save( args , paper )

	print(
		f"OCR :: pinned text on {total_pinned} detections "
		f"across {len(jobs)} pdfs ( engine={engine} )"
	)
