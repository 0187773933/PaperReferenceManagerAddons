"""
prma methods : iterate the unified papers/ DB , extract the METHODS
section of each paper , and write one plain-text file per paper at
  args.output / methods / { doi_to_filename( doi ) }.txt

Pipeline : depends on yolo + ocr + preprocess. preprocess.run is
called inline ( it itself chains through ocr , which runs yolo inline )
so 'prma methods' is a one-stop command :
  snapshot -> yolo -> ocr -> preprocess -> extract
Every inner stage is idempotent ; papers already done fall through
cheaply.

Honors --manager the same way every other task does. Skip-if-exists is
the default ; pass --force to re-render even papers whose .txt is
already on disk.

Empty-result sentinel : when the extractor returns no text for a paper
( e.g. the paper has yolo + sections but no 'methods' bucket and no
methods header in raw_text -- common for editorials , letters , and
some review chapters ) , we still write a ZERO-BYTE .txt at the target
path. The planning loop on the next run sees out_path.exists() and
fast-paths the paper into the 'already-done' bucket instead of
re-walking the extractor every time. Downstream tasks ( summarize ,
rollup ) .strip() the file contents -- empty stays empty -- so they
correctly skip these papers too. ` --force ` re-tries from scratch.
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
	# preprocess.run itself chains through ocr ( which runs yolo inline
	# for any paper that doesn't have it ) , so this one call pulls
	# every paper up through sections + raw_text. All stages are
	# idempotent.
	from . import preprocess as preprocess_task
	preprocess_task.run( args )

	from ..pdf import methods as methods_mod

	force         = getattr( args , "methods_force" , False )
	managers      = _resolve_managers( args )
	manager_label = " + ".join( managers ) if managers else "all"

	out_dir = args.output.joinpath( "methods" )
	out_dir.mkdir( parents=True , exist_ok=True )

	# Plan the work. Mirrors the structure of prma text / prma md so
	# the skipped-buckets summary is the same shape.
	jobs = []
	skip_no_content , skip_done , skip_other = 0 , 0 , 0
	for doi , paper in papers.iter_all( args ):
		if not _paper_matches_managers( paper , managers ):
			skip_other += 1
			continue
		# Quick gate : if neither sections nor raw_text is present ,
		# there's nothing for methods_mod to work with. We still re-
		# check after extraction below ( the sections path may have
		# the bucket present but empty ).
		if not paper.get( "sections" ) and not paper.get( "raw_text" ):
			skip_no_content += 1
			continue
		out_path = out_dir.joinpath( f"{utils.doi_to_filename( doi )}.txt" )
		if not force and out_path.exists():
			skip_done += 1
			continue
		jobs.append( ( doi , out_path ) )

	print(
		f"METHODS :: ({manager_label})  {len(jobs)} papers to extract -> {out_dir} "
		f"( skipped: no-content={skip_no_content} "
		f"already-done={skip_done} other-manager={skip_other} )"
	)

	outer = tqdm( jobs , desc="Papers" , unit="txt" )
	n_written , n_empty , total_bytes = 0 , 0 , 0
	for doi , out_path in outer:
		paper = papers.load( args , doi )
		if paper is None:
			continue
		try:
			text = methods_mod.paper_methods( paper )
		except Exception as e:
			print( f"METHODS :: {doi}: failed ( {e} )" )
			continue
		if not text:
			# Persist an EMPTY sentinel so the planning loop on the next
			# ` prma methods ` run sees out_path.exists() and fast-paths
			# this paper into the 'already-done' bucket. Without this we
			# re-walk every paper that came back empty on EVERY run --
			# they have sections data ( so they pass the no-content gate )
			# but no methods bucket ( so the extractor returns "" ) , a
			# silent steady-state cost that scales with the library.
			# --force still re-tries because the planning loop ignores
			# exists() when force=True. Downstream tasks ( summarize ,
			# rollup ) strip file contents -- empty stays empty -- so
			# they correctly skip these papers too.
			try:
				out_path.write_text( "" , encoding="utf-8" )
			except Exception as e:
				print( f"METHODS :: {doi}: sentinel write failed ( {e} )" )
			n_empty += 1
			continue
		try:
			out_path.write_text( text , encoding="utf-8" )
		except Exception as e:
			print( f"METHODS :: {doi}: write failed ( {e} )" )
			continue
		n_written   += 1
		total_bytes += len( text )

	print(
		f"METHODS :: wrote {n_written} .txt files + {n_empty} empty sentinels "
		f"( {total_bytes} bytes total ; sentinels mark papers with no methods so "
		f"future runs skip them via the already-done path ) -> {out_dir}"
	)
