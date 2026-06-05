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
    pairs ;
  - paper[ 'raw_text' ]    -- '\\n'-joined raw page text from
    pymupdf , for plain full-text indexing.

raw_text is stored as None if the PDF is missing on disk or
extraction fails ; the key is still set so idempotency holds.

Honors --manager :
  --manager zotero   ( default ) -> papers with a 'zotero'   source
  --manager mendeley             -> papers with a 'mendeley' source
  --manager all                  -> every paper in the DB

For each paper :
  - skip if it has no 'yolo' field yet ( run ` prma yolo ` and
    ` prma ocr ` first ) ;
  - skip if all three fields above are already present , unless
    --force is passed ;
  - call preprocess.preprocess_paper() and save all three results
    back onto the paper record.

Idempotent. Does NOT re-render YOLO or re-OCR anything ; sorts /
labels what's already pinned on the paper , plus a single pymupdf
raw-text extraction per PDF.
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

	# Plan the work. "Done" means ALL THREE output fields are already
	# present on the paper record ; we use `in` rather than `is not
	# None` so a paper whose pymupdf extraction failed last time (
	# stored as None ) still counts as done and we don't retry every
	# run. --force overrides.
	required_keys = (
		"yolo_sorted_page_indexes" ,
		"sections" ,
		"raw_text" ,
	)
	jobs = []
	skip_no_yolo , skip_done , skip_other = 0 , 0 , 0
	for doi , paper in papers.iter_all( args ):
		if not _paper_matches_managers( paper , managers ):
			skip_other += 1
			continue
		if not force and all( k in paper for k in required_keys ):
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
	total_indexes , total_classified , total_raw = 0 , 0 , 0
	for doi in outer:
		paper = papers.load( args , doi )
		if paper is None:
			continue
		pdf_path = paper.get( "pdf_path" )
		try:
			ordered , sections , raw_text = pre.preprocess_paper(
				paper , pdf_path=pdf_path ,
			)
		except Exception as e:
			print( f"PREPROCESS :: {doi}: failed ( {e} )" )
			continue
		paper[ "yolo_sorted_page_indexes" ] = ordered
		paper[ "sections" ]                 = sections
		paper[ "raw_text" ]                 = raw_text   # may be None
		total_indexes    += sum( len( p ) for p in ordered )
		total_classified += sum( len( v ) for v in sections.values() )
		if raw_text:
			total_raw += 1
		papers.save( args , paper )

	print(
		f"PREPROCESS :: wrote {total_indexes} sorted indexes + "
		f"{total_classified} classified into sections + "
		f"raw_text on {total_raw} across {len(jobs)} papers"
	)
