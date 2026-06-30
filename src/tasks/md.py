"""
prma md : iterate the unified papers/ DB and write a Markdown
document for each paper using its 'sections' classification. Output
path :
  args.output / md / { doi_to_filename( doi ) }.md

Preprocess and images are both run inline before rendering ( both
idempotent : papers that already have 'yolo_sorted_page_indexes' +
'sections' are skipped at the preprocess stage , and papers whose
figure crops are already on disk are skipped at the images stage ) so
'prma md' is a one-stop command :

  snapshot -> yolo -> ocr -> preprocess -> images -> code -> render

preprocess.run() already pulls every paper up through yolo + ocr
( see src/tasks/preprocess.py ) , so after it returns each paper has
its YOLO + OCR + sections. images.run() then crops the figures into
output/images/ALL/ so the rendered Markdown can link real PNGs instead
of falling back to the per-figure text placeholder.

Honors --manager :
  --manager zotero   ( default ) -> papers with a 'zotero'   source
  --manager mendeley             -> papers with a 'mendeley' source
  --manager all                  -> every paper in the DB

For each paper :
  - skip if it has no 'sections' field after preprocess ran
    ( almost always means it has no PDF / yolo data yet ) ;
  - skip if the target .md already exists , unless --force ;
  - render via src/pdf/md.py and write to disk.
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
	# Make sure every paper has sections before we try to render.
	# preprocess.run is idempotent : papers that already have
	# 'yolo_sorted_page_indexes' + 'sections' fall through cheaply.
	# It also pulls every paper up through yolo + ocr on the way , so
	# this single call covers snapshot -> yolo -> ocr -> preprocess.
	from . import preprocess as preprocess_task
	preprocess_task.run( args )

	# Crop each paper's figures to output/images/ALL/ so the rendered
	# Markdown can link real PNGs instead of the text placeholder.
	# Idempotent : papers whose crops already exist fall through cheaply ,
	# and YOLO is reused from the preprocess pass above ( not re-run ).
	from . import images as images_task
	images_task.run( args )

	# Harvest source-code / data links so the renderer can emit a
	# '## Source Code' section. Idempotent ( papers already scanned carry a
	# 'code' marker and fall through ) ; in the per-paper suite this already
	# ran as the stage right before md , so here it's a cheap no-op -- it just
	# makes a standalone ` prma md ` one-stop. OCR is reused from preprocess.
	from . import code as code_task
	code_task.run( args )

	from ..pdf import md as md_mod

	force              = getattr( args , "md_force" , False )
	include_references = getattr( args , "md_include_references" , False )
	managers = _resolve_managers( args )
	manager_label = " + ".join( managers ) if managers else "all"

	out_dir = args.output.joinpath( "md" )
	out_dir.mkdir( parents=True , exist_ok=True )

	# Images written by ` prma images ` live at
	# args.output/images/ALL/{prefix}-figure-N.png . From a .md file
	# at args.output/md/{prefix}.md the relative href is
	# ../images/ALL/ . We pass both the relative dir ( used in the
	# Markdown image link ) and the absolute dir ( used to check if
	# the file actually exists ) so the renderer can fall back to a
	# text placeholder per-file when an image isn't on disk.
	image_abs_dir = args.output.joinpath( "images" , "ALL" )
	image_rel_dir = "../images/ALL"

	# Plan the work.
	jobs = []
	skip_no_sections , skip_done , skip_other = 0 , 0 , 0
	for doi , paper in papers.iter_all( args ):
		if not _paper_matches_managers( paper , managers ):
			skip_other += 1
			continue
		if not paper.get( "sections" ):
			skip_no_sections += 1
			continue
		out_path = out_dir.joinpath( f"{utils.doi_to_filename( doi )}.md" )
		if not force and out_path.exists():
			skip_done += 1
			continue
		jobs.append( ( doi , out_path ) )

	print(
		f"MD :: ({manager_label})  {len(jobs)} papers to render -> {out_dir} "
		f"( skipped: no-sections={skip_no_sections} "
		f"already-done={skip_done} other-manager={skip_other} )"
	)

	outer = tqdm( jobs , desc="Papers" , unit="md" )
	total_bytes = 0
	for doi , out_path in outer:
		paper = papers.load( args , doi )
		if paper is None:
			continue
		prefix = utils.doi_to_filename( doi )
		try:
			doc = md_mod.paper_to_markdown(
				paper ,
				image_rel_dir=image_rel_dir ,
				image_abs_dir=image_abs_dir ,
				prefix=prefix ,
				include_references=include_references ,
			)
		except Exception as e:
			print( f"MD :: {doi}: failed ( {e} )" )
			continue
		try:
			out_path.write_text( doc , encoding="utf-8" )
		except Exception as e:
			print( f"MD :: {doi}: write failed ( {e} )" )
			continue
		total_bytes += len( doc )

	print(
		f"MD :: wrote {len(jobs)} .md files "
		f"( {total_bytes} bytes total ) -> {out_dir}"
	)
