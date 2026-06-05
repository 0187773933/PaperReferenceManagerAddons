"""
prma text : iterate the unified papers/ DB and write a plain-text
document for each paper to args.output / text / { doi_to_filename }.txt .

Source ( --source ) :
  raw  ( default ) -- emit paper[ 'raw_text' ] ( pymupdf full-text
                      dump from preprocess ) , prefixed with a tiny
                      title + DOI header. Preserves the PDF text
                      layer verbatim.
  ocr               -- build from yolo + ocr + sections , same content
                      that ` prma md ` uses , but without Markdown
                      formatting ( uppercase + dash-underline headers
                      , numbered references , [ Figure on page N ]
                      placeholders ).

Preprocess is run inline first ( idempotent ) so 'prma text' is a
one-stop command : snapshot -> preprocess -> render. Honors --manager
the same way every other task does.
"""

from tqdm import tqdm

from ..db import papers
from ..utils import utils


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
	# Make sure raw_text / sections are populated. preprocess.run is
	# idempotent : papers that already have the three required keys
	# fall through cheaply.
	from . import preprocess as preprocess_task
	preprocess_task.run( args )

	from ..pdf import text as text_mod

	source             = getattr( args , "text_source" , "raw" )
	force              = getattr( args , "text_force"  , False )
	include_references = getattr( args , "text_include_references" , False )
	managers = _resolve_managers( args )
	manager_label = " + ".join( managers ) if managers else "all"

	out_dir = args.output.joinpath( "text" )
	out_dir.mkdir( parents=True , exist_ok=True )

	# Plan the work.
	jobs = []
	skip_no_content , skip_done , skip_other = 0 , 0 , 0
	for doi , paper in papers.iter_all( args ):
		if not _paper_matches_managers( paper , managers ):
			skip_other += 1
			continue
		# Source-specific availability check : 'raw' needs raw_text ;
		# 'ocr' needs sections.
		if source == "ocr":
			if not paper.get( "sections" ):
				skip_no_content += 1
				continue
		else:
			if not paper.get( "raw_text" ):
				skip_no_content += 1
				continue
		out_path = out_dir.joinpath( f"{utils.doi_to_filename( doi )}.txt" )
		if not force and out_path.exists():
			skip_done += 1
			continue
		jobs.append( ( doi , out_path ) )

	print(
		f"TEXT :: ({manager_label} , source={source})  {len(jobs)} papers "
		f"to render -> {out_dir} "
		f"( skipped: no-{source}-content={skip_no_content} "
		f"already-done={skip_done} other-manager={skip_other} )"
	)

	outer = tqdm( jobs , desc="Papers" , unit="txt" )
	total_bytes = 0
	for doi , out_path in outer:
		paper = papers.load( args , doi )
		if paper is None:
			continue
		try:
			doc = text_mod.paper_to_text(
				paper ,
				source=source ,
				include_references=include_references ,
			)
		except Exception as e:
			print( f"TEXT :: {doi}: failed ( {e} )" )
			continue
		if not doc:
			continue
		try:
			out_path.write_text( doc , encoding="utf-8" )
		except Exception as e:
			print( f"TEXT :: {doi}: write failed ( {e} )" )
			continue
		total_bytes += len( doc )

	print(
		f"TEXT :: wrote {len(jobs)} .txt files "
		f"( {total_bytes} bytes total , source={source} ) -> {out_dir}"
	)
