"""
prma yolo : iterate the unified papers/ DB , run doclayout-yolo on
every paper that doesn't yet have a 'yolo' field , merge the result
into that paper's JSON record.

Honors --manager :
  --manager zotero    -> only papers that have a 'zotero'   source ( default )
  --manager mendeley  -> only papers that have a 'mendeley' source
  --manager all       -> every paper in the DB

The on-disk shape after this task runs , per paper :
  {
    ... existing snapshot fields ...
    "pdf_path": "/abs/path.pdf" ,
    "yolo": {
      "meta": { "dpi": 200 , "confidence": 0.2 , ... , "ran_at": "..." } ,
      "pages": [ [ { type , class_id , bbox , bbox_area , confidence } , ... ] , ... ]
    }
  }

Idempotent : papers with a 'yolo' field already are skipped unless
--force is passed.
"""

from pathlib import Path
from tqdm import tqdm

from ..db import papers


def _resolve_managers( args ):
	"""Translate --manager / --zotero / --mendeley into the set of
	manager-name strings the task should process."""
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


def run( args ):
	from ..pdf import pdf as pdf_mod

	do_deskew = getattr( args , "pdf_deskew" , False )
	force     = getattr( args , "yolo_force" , False )
	managers  = _resolve_managers( args )
	manager_label = " + ".join( managers ) if managers else "all"

	jobs = []
	skip_no_pdf , skip_missing , skip_done , skip_other = 0 , 0 , 0 , 0
	for doi , paper in papers.iter_all( args ):
		if not _paper_matches_managers( paper , managers ):
			skip_other += 1
			continue
		if not force and paper.get( "yolo" ) and paper[ "yolo" ].get( "pages" ):
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
		jobs.append( ( doi , p ) )

	print(
		f"YOLO :: ({manager_label})  {len(jobs)} pdfs to process -> papers/ "
		f"( skipped: no-pdf={skip_no_pdf} not-on-disk={skip_missing} "
		f"already-done={skip_done} other-manager={skip_other} )"
	)

	outer = tqdm( jobs , desc="PDFs" , position=1 , leave=True , unit="pdf" )
	n_ok = 0
	for doi , pdf_path in outer:
		outer.set_postfix_str( pdf_path.name[ :60 ] )
		try:
			result = pdf_mod.yolo( pdf_path , do_deskew=do_deskew )
		except Exception as e:
			print( f"YOLO :: {pdf_path.name} : failed ( {e} )" )
			continue
		# Reload paper to avoid clobbering any concurrent updates.
		paper = papers.load( args , doi )
		if paper is None:
			continue
		paper[ "yolo" ] = result
		papers.save( args , paper )
		n_ok += 1

	print( f"YOLO :: wrote yolo data for {n_ok} papers." )
