"""
prma images : iterate the unified papers/ DB and extract figures
( and optionally tables ) using each paper's YOLO bboxes.

Honors --manager :
  --manager zotero   ( default ) -> papers with a 'zotero'   source
  --manager mendeley             -> papers with a 'mendeley' source
  --manager all                  -> every paper in the DB

For each paper :
  - skip if no PDF is on disk
  - if paper has no 'yolo' field , run YOLO on the fly  ( same call
    as ` prma yolo ` would make ) and save it onto the paper record
  - call src/pdf/images.extract() with the in-memory yolo data

Output ( no more per-manager subdirs ; the DB is unified , so the
images dir is unified too ) :
  output/images/ALL/{normalized_doi}-figure-N.png
  output/images/ALL/{normalized_doi}-table-N.png
  output/images/{normalized_doi}-Figures.png    ( per-paper montage )
  output/images/{normalized_doi}-Tables.png     ( if include_tables )
"""

from datetime import datetime , timezone
from pathlib import Path
from tqdm import tqdm

from ..db import papers
from ..utils import utils


def _utc_now_iso():
	return datetime.now( timezone.utc ).replace( microsecond=0 ).isoformat()


# Reuse the same manager-filter logic the yolo task uses , so flag
# behavior is identical across commands.
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


def _scan_done_prefixes( all_dir ):
	"""Walk output/images/ALL/ once and collect every prefix that
	already has at least one figure/table image on disk , so we can
	skip those papers on resume."""
	done = set()
	if not all_dir.exists():
		return done
	for f in all_dir.iterdir():
		if f.suffix != ".png":
			continue
		for marker in ( "-figure-" , "-table-" ):
			idx = f.stem.rfind( marker )
			if idx > 0:
				done.add( f.stem[ :idx ] )
				break
	return done


def _count_dets_by_type( yolo_data ):
	"""Tally how many detections of each type live in `yolo_data` ;
	used to explain why a paper produced zero crops."""
	out = {}
	if not yolo_data:
		return out
	for page in yolo_data.get( "pages" , [] ) or []:
		for det in page:
			if not isinstance( det , dict ):
				continue
			t = det.get( "type" , "?" )
			out[ t ] = out.get( t , 0 ) + 1
	return out


def run( args ):
	from ..pdf import pdf       as pdf_mod
	from ..pdf import images    as images_mod

	include_tables = getattr( args , "images_include_tables" , False )
	montage        = not getattr( args , "images_no_montage" , False )
	size_name      = getattr( args , "images_montage_size" , "medium" )
	montage_scale  = images_mod.MONTAGE_SCALES.get( size_name , 1.0 )
	do_deskew      = getattr( args , "pdf_deskew" , False )
	force          = getattr( args , "images_force" , False )
	managers       = _resolve_managers( args )
	manager_label  = " + ".join( managers ) if managers else "all"

	images_dir = args.output.joinpath( "images" )
	images_dir.mkdir( parents=True , exist_ok=True )
	all_dir = images_dir.joinpath( "ALL" )
	all_dir.mkdir( parents=True , exist_ok=True )

	done_prefixes = _scan_done_prefixes( all_dir )

	# Plan the work. A paper is considered DONE when ANY of :
	#   - the unified DB record has paper[ 'images' ][ 'yolo_at' ] equal
	#     to paper[ 'yolo' ][ 'meta' ][ 'ran_at' ]  ( we processed it
	#     against this exact YOLO output already ) , OR
	#   - figure / table crops with the right prefix already exist on
	#     disk in output/images/ALL/  ( backwards compat with runs
	#     that predate the marker ) .
	# Re-running ` prma yolo ` updates ran_at and naturally invalidates
	# the marker so a re-crop happens on the next ` prma images ` .
	jobs = []
	skip_no_pdf , skip_missing , skip_done , skip_other , skip_failed = 0 , 0 , 0 , 0 , 0
	needs_yolo = 0
	for doi , paper in papers.iter_all( args ):
		if not _paper_matches_managers( paper , managers ):
			skip_other += 1
			continue
		prefix = utils.doi_to_filename( doi )
		if not prefix:
			continue
		# YOLO already failed to load this PDF on a previous run -- the
		# inline-YOLO step below would just fail again , so skip it.
		# --force overrides ; a successful YOLO clears the marker.
		if not force and paper.get( papers.YOLO_FAILED_KEY ):
			skip_failed += 1
			continue
		if not force and prefix in done_prefixes:
			skip_done += 1
			continue
		if not force:
			yolo_ran_at = ( ( paper.get( "yolo" ) or {} ).get( "meta" ) or {} ).get( "ran_at" )
			images_marker = paper.get( "images" ) or {}
			# Marker matches if we previously processed this paper AND
			# both its yolo_at value AND its tables-mode line up with
			# the current run. Legacy yolo data without meta.ran_at
			# stores yolo_at=None and matches future runs with None too.
			if (
				images_marker.get( "processed_at" )
				and images_marker.get( "yolo_at" ) == yolo_ran_at
				and bool( images_marker.get( "include_tables" ) ) == bool( include_tables )
			):
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
		jobs.append( ( doi , prefix , p ) )

	print(
		f"IMAGES :: ({manager_label})  {len(jobs)} pdfs to process -> {images_dir} "
		f"( will run YOLO inline for {needs_yolo} ; "
		f"skipped: no-pdf={skip_no_pdf} not-on-disk={skip_missing} "
		f"already-done={skip_done} yolo-failed={skip_failed} other-manager={skip_other} )"
	)

	outer = tqdm( jobs , desc="PDFs" , position=1 , leave=True , unit="pdf" )
	total_imgs = 0
	n_no_figs , total_fig_dets , total_tab_dets = 0 , 0 , 0
	for doi , prefix , pdf_path in outer:
		outer.set_postfix_str( pdf_path.name[ :60 ] )

		# Reload the paper so we don't clobber concurrent updates.
		paper = papers.load( args , doi )
		if paper is None:
			continue
		yolo_data = paper.get( "yolo" )
		if not yolo_data or not yolo_data.get( "pages" ):
			# Inline YOLO , persist it back into the paper record so
			# the next ` prma yolo ` / ` prma ocr ` invocation reuses it.
			try:
				yolo_data = pdf_mod.yolo( pdf_path , do_deskew=do_deskew )
			except Exception as e:
				print( f"IMAGES :: {pdf_path.name} : YOLO failed ( {e} )" )
				papers.mark_yolo_failed( args , doi , e , pdf_path.name )
				continue
			paper[ "yolo" ] = yolo_data
			papers.clear_yolo_failed( args , paper )   # recovered : drop stale marker
			papers.save( args , paper )

		# Count detections by type so we can both ( a ) explain "wrote 0
		# images" ( usually a philosophy / review paper with no figures ,
		# not a bug ) and ( b ) skip the expensive PDF render entirely
		# for papers that have nothing to crop.
		by_type = _count_dets_by_type( yolo_data )
		n_figs  = by_type.get( "figure" , 0 )
		n_tabs  = by_type.get( "table"  , 0 )
		total_fig_dets += n_figs
		total_tab_dets += n_tabs
		want_figures = n_figs > 0
		want_tables  = include_tables and n_tabs > 0
		yolo_ran_at  = ( ( yolo_data or {} ).get( "meta" ) or {} ).get( "ran_at" )

		if not want_figures and not want_tables:
			# No anchors at all -> skip rendering AND stamp the marker so
			# we don't re-evaluate this paper on every future ` prma images `
			# until YOLO is re-run ( which changes meta.ran_at ).
			n_no_figs += 1
			paper[ "images" ] = {
				"yolo_at":          yolo_ran_at ,
				"processed_at":     _utc_now_iso() ,
				"n_crops":          0 ,
				"skipped_reason":   "no-anchors" ,
				"include_tables":   include_tables ,
			}
			papers.save( args , paper )
			continue

		try:
			n = images_mod.extract(
				pdf_path , yolo_data , images_dir , prefix ,
				include_tables=include_tables ,
				montage=montage ,
				montage_scale=montage_scale ,
			)
		except Exception as e:
			print( f"IMAGES :: {pdf_path.name} : extract failed ( {e} )" )
			continue
		total_imgs += n

		paper[ "images" ] = {
			"yolo_at":        yolo_ran_at ,
			"processed_at":   _utc_now_iso() ,
			"n_crops":        n ,
			"include_tables": include_tables ,
		}
		papers.save( args , paper )

	tail_count = (
		f"figures-in-yolo={total_fig_dets} "
		+ ( f"tables-in-yolo={total_tab_dets} " if include_tables else "" )
		+ f"papers-with-no-anchors={n_no_figs}"
	)
	print(
		f"IMAGES :: wrote {total_imgs} cropped images "
		f"-> {images_dir}/ALL/  ( + per-paper montages ; {tail_count} )"
	)
