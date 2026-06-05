"""
prma preprocess : iterate the unified papers/ DB and add :
  - paper[ 'yolo_sorted_page_indexes' ] -- per-page reading-order
    permutation of the existing yolo detection indexes ( so we don't
    duplicate any bbox / text content ; consumers index back into
    paper[ 'yolo' ][ 'pages' ][ page_idx ] ) ;
  - paper[ 'sections' ] -- a dict keyed by semantic bucket
    ( title / abstract / introduction / background / methods /
      results / figures / tables / conclusions / future / misc /
      references ) , each value a list of [ page_idx , det_idx ]
    pairs.

Honors --manager :
  --manager zotero   ( default ) -> papers with a 'zotero'   source
  --manager mendeley             -> papers with a 'mendeley' source
  --manager all                  -> every paper in the DB

For each paper :
  - skip if it has no 'yolo' field yet ( run ` prma yolo ` and
    ` prma ocr ` first ) ;
  - skip if it already has both 'yolo_sorted_page_indexes' and
    'sections' , unless --force is passed ;
  - call preprocess.preprocess_paper() and save both results back
    onto the paper record.

Idempotent. Does NOT re-render the PDF , re-run YOLO , or re-OCR
anything ; it only sorts and labels what's already pinned on the paper.
"""

from tqdm import tqdm

from ..db import papers


# Same manager-filter helpers used by the other task modules ; kept
# duplicated rather than imported so each task module stays
# self-contained.

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


def run( args ):
	from ..pdf import preprocess as pre

	force = getattr( args , "preprocess_force" , False )
	managers = _resolve_managers( args )
	manager_label = " + ".join( managers ) if managers else "all"

	# Plan the work.
	jobs = []
	skip_no_yolo , skip_done , skip_other = 0 , 0 , 0
	for doi , paper in papers.iter_all( args ):
		if not _paper_matches_managers( paper , managers ):
			skip_other += 1
			continue
		if not force and (
			paper.get( "yolo_sorted_page_indexes" ) is not None
			and paper.get( "sections" ) is not None
		):
			skip_done += 1
			continue
		yolo = paper.get( "yolo" ) or {}
		if not yolo.get( "pages" ):
			skip_no_yolo += 1
			continue
		jobs.append( doi )

	print(
		f"PREPROCESS :: ({manager_label})  {len(jobs)} papers to sort -> papers/ "
		f"( skipped: no-yolo={skip_no_yolo} "
		f"already-done={skip_done} other-manager={skip_other} )"
	)

	outer = tqdm( jobs , desc="Papers" , unit="paper" )
	total_indexes , total_classified = 0 , 0
	for doi in outer:
		paper = papers.load( args , doi )
		if paper is None:
			continue
		try:
			ordered , sections = pre.preprocess_paper( paper )
		except Exception as e:
			print( f"PREPROCESS :: {doi}: failed ( {e} )" )
			continue
		paper[ "yolo_sorted_page_indexes" ] = ordered
		paper[ "sections" ] = sections
		total_indexes    += sum( len( p ) for p in ordered )
		total_classified += sum( len( v ) for v in sections.values() )
		papers.save( args , paper )

	print(
		f"PREPROCESS :: wrote {total_indexes} sorted indexes + "
		f"{total_classified} classified into sections across {len(jobs)} papers"
	)
