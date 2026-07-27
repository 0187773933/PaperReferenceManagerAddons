"""
prma all-images : the /method-images contact sheet with the FILTER TAKEN OFF --
every figure ` prma images ` cropped out of every processed paper , in one page.

Same relationship to the pipeline as ` prma method-images ` : a pure
post-processing pass over what's already on disk , never running a stage inline
( so papers the suite hasn't reached are counted and skipped ) , reading

  - paper[ 'yolo' ][ 'pages' ]   figure dets + the OCR text of their captions
  - paper[ 'modalities' ]        the modality stamp ( pills + filter chips )
  - output/images/ALL/*.png      the crops ` prma images ` wrote
  - output/md , output/methods   linked from the report ( when present )

WHAT'S DIFFERENT from ` prma method-images ` is only what gets in : there are no
keywords , no [strong] / [weak] tiers , no --threshold and no relevance ranking ,
because nothing is being selected FOR. Every ` figure ` det with a cropped PNG is
a card , captioned or not. Consequently the cards carry no keyword tags , the
chip bar has no keyword chips , and the 'Overviews only' filter -- which is a
statement about a caption's structural vocabulary -- isn't there either.

Everything else is deliberately IDENTICAL , and identical by SHARING rather than
by copying : the page ( src/dashboard/method_images.html ) , the paper-section /
figure-card HTML ( method_images.render_paper_sections ) , the caption pairing ,
the publication-date lookup and the modality stamp all come from
src/tasks/method_images.py. Fix a card there and both reports get it.

CURATION IS SEPARATE. This report has its OWN selection and its own skipped-paper
list -- the "images" collection in src/db/figure_state.py
( output/cache/images-state.json ) , served at /api/images/state -- so a pass over
everything here never disturbs the model-design picks you made on
/method-images. The page namespaces its localStorage fallback , its saved sort
and its export filenames the same way ( see the data-mode plumbing in
method_images.html ) .

It does READ the other collection , one way : a figure already picked on
/method-images is badged here and has its own filter chip ( 'In Model Design' ) ,
so a second pass can see at a glance what's already been curated and skip it.
That works because BOTH reports name a figure by the same stable id
( '<paper-key>#figure-<N>' ) . Nothing here writes that collection.

Output : output/all-images/report.html ( --out to move it ) , served by
` prma server ` at /images. Overwritten on each run.

SIZE : this is the whole library , so the page is big -- on a ~2000-paper library
roughly 15k cards and a few tens of MB of HTML. Images are lazy-loaded , so a
browser copes , but expect a slower first paint than /method-images and give the
sort / filter chips a beat to work through the cards.
"""

import html
import threading
from datetime import datetime , timezone
from pathlib import Path

from tqdm import tqdm

from ..db import papers
from ..utils import utils
from ..utils import methods as methods_vocab
from . import method_images as mi


# data-mode / figure_state collection / /api/<mode>/… prefix for this report.
MODE = "images"


def report_path( args ):
	"""Where the report lands by default. Shared with ` prma server ` , which
	serves this exact file at /images ( and rebuilds it after the watch worker
	consumes new papers ) -- one artifact , not a second renderer.

	NOT under output/images/ : that directory is the crop output ` prma images `
	owns ( and the server serves verbatim under /images/… ) , so the report gets
	its own , same as method-images does."""
	return args.output.joinpath( "all-images" , "report.html" )


def report_is_stale( args ):
	"""True when the report needs rebuilding because a DEFINITION input changed :
	it's missing , or the shared page template , THIS module , or the module it
	borrows its renderer from ( method_images ) is newer than the built report.
	( New DATA is handled separately , by the --watch worker's refresh_reports. )

	No keyword file in the list -- unlike method-images , nothing about this
	report's CONTENT is configurable , so there's nothing else to watch.

	Best-effort : any stat error resolves to 'not stale' , so a filesystem hiccup
	never forces a rebuild loop."""
	try:
		rep = report_path( args )
		if not rep.exists():
			return True
		rep_m = rep.stat().st_mtime
		for src in ( mi.TEMPLATE_PATH , Path( __file__ ) , Path( mi.__file__ ) ):
			try:
				if src and src.exists() and src.stat().st_mtime > rep_m:
					return True
			except Exception:
				pass
		return False
	except Exception:
		return False


# Serializes in-process rebuilds , same as method_images._REBUILD_LOCK.
_REBUILD_LOCK = threading.Lock()


def rebuild( args ):
	"""Regenerate the report because an input changed -- new papers , or an edited
	page. Whole-library pass , so callers batch it. Never raises : a failed report
	must not take a processing run or the server down with it."""
	prev , args.only_keys = getattr( args , "only_keys" , None ) , None
	try:
		with _REBUILD_LOCK:
			run( args )
	except Exception as e:
		print( f"ALL-IMAGES :: report rebuild failed ( {e} ) ; leaving the last one in place" )
	finally:
		args.only_keys = prev


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

def _render_report( out_path , matched , stats ):
	"""Build the full HTML document : the data-driven fragments here , everything
	else from the shared template ( method_images.TEMPLATE_PATH / _fill )."""
	out_dir = out_path.parent
	now     = datetime.now( timezone.utc ).replace( microsecond=0 ).isoformat()
	n_figs  = sum( len( p[ "figures" ] ) for p in matched )

	tail = ""
	if stats[ "skip_not_processed" ]:
		tail = (
			f' &nbsp;·&nbsp; {stats[ "skip_not_processed" ]} papers not through the '
			f'PDF suite yet'
		)
	meta = (
		f'<p class="meta">Generated {html.escape( now )} &nbsp;·&nbsp; '
		f'{n_figs} figures across {len( matched )} papers '
		f'( every figure the pipeline cropped -- no keyword filter ){tail}</p>'
	)

	# Chips. No keyword chips and no 'Overviews only' : both are statements about
	# a caption matching something , and nothing was matched to get here.
	C = []
	C.append( f'<button class="chip active" id="chip-all">All<span class="n">{n_figs}</span></button>' )
	C.append(
		'<button class="chip sel-toggle" id="chip-selected" '
		'title="Show only the figures you picked here ( this page keeps its own selection )">'
		'Selected<span class="n" id="sel-count">0</span></button>'
	)
	# Read-only cross-reference to the OTHER report's picks , so a pass over
	# everything can see what's already curated. The count is filled by the page
	# once it has fetched that collection ( see method_images.html ) .
	C.append(
		'<button class="chip xref-toggle" id="chip-xref" '
		'title="Figures you already picked on /method-images ( read-only here -- '
		'clicking a card still edits THIS page\'s own selection )">'
		'In Model Design<span class="n" id="xref-count">0</span></button>'
	)
	C.append(
		'<button class="chip skip-toggle" id="chip-skipped" '
		'title="Show only the papers you skipped ( otherwise hidden from the list )">'
		'Skipped<span class="n" id="skip-count">0</span></button>'
	)
	# Modality filters , as on /method-images : a fixed , small set from
	# < --config >/methods.py. Counts are FIGURES , not papers.
	for m in stats[ "mod_labels" ]:
		n = stats[ "mod_counts" ].get( m , 0 )
		if not n:
			continue
		em = html.escape( m , quote=True )
		C.append(
			f'<button class="chip mod-toggle" data-modf="{em}" '
			f'title="Only figures from studies using {em}">'
			f'{html.escape( m )}<span class="n">{n}</span></button>'
		)

	L = mi.render_paper_sections( matched , out_dir )
	if not matched:
		L.append(
			"<p>No figures on disk yet. This report renders the crops "
			"<code>prma images</code> writes , which need the PDF suite to have run "
			"( <code>prma process</code> , or <code>prma server --watch</code> ) -- "
			"or run the stages directly : <code>prma yolo</code> ; "
			"<code>prma ocr</code> ; <code>prma images</code>.</p>"
		)

	return mi._fill( {
		"mode":     MODE ,
		"meta":     meta ,
		"chips":    "\n".join( C ) ,
		"papers":   "\n".join( L ) ,
		# method-images-only legend text , hidden in this mode ( see the footer's
		# per-mode spans in the template ) -- the slot still has to be filled.
		"min_weak": "" ,
	} )


# ---------------------------------------------------------------------------
# Task entry
# ---------------------------------------------------------------------------

def run( args ):
	from ..pdf import images     as images_mod
	from ..pdf import md         as md_mod
	from ..pdf import pdf        as pdf_mod
	from ..pdf import preprocess as PP
	from . import modalities     as modalities_task

	managers      = mi._resolve_managers( args )
	manager_label = " + ".join( managers ) if managers else "all"

	all_dir     = args.output.joinpath( "images"  , "ALL" )
	md_dir      = args.output.joinpath( "md"      )
	methods_dir = args.output.joinpath( "methods" )
	images_dir  = args.output.joinpath( "images"  )

	out_path = getattr( args , "all_images_out" , None )
	out_path = Path( out_path ) if out_path else report_path( args )

	# Plan : every paper the pipeline got far enough on to have yolo pages ( the
	# figure dets and their OCR'd captions ride on those ) . Nothing runs inline.
	jobs = []
	skip_not_processed , skip_other = 0 , 0
	for key , paper in papers.iter_all( args ):
		if not mi._paper_matches_managers( paper , managers ):
			skip_other += 1
			continue
		prefix = utils.doi_to_filename( key )
		if not prefix:
			continue
		if not ( ( paper.get( "yolo" ) or {} ).get( "pages" ) ):
			skip_not_processed += 1
			continue
		jobs.append( ( key , prefix ) )

	print(
		f"ALL-IMAGES :: ({manager_label})  {len( jobs )} processed papers to render "
		f"( every cropped figure , no keyword filter ; "
		f"skipped: not-processed={skip_not_processed} other-manager={skip_other} )"
	)
	if not jobs:
		print(
			"ALL-IMAGES :: nothing has been processed yet -- this command depends "
			"on the PDF suite. Run ` prma process <paper> ` / ` prma server --watch ` , "
			"or the stages directly ( prma yolo ; prma ocr ; prma images ) , "
			"then re-run."
		)
		return

	matched = []
	figures_seen , captions_empty , missing_png = 0 , 0 , 0
	n_with_mods , n_inferred , _mod_warned = 0 , 0 , False
	for key , prefix in tqdm( jobs , desc="Papers" , unit="paper" ):
		paper = papers.load( args , key )
		if paper is None:
			continue
		yolo_data = paper.get( "yolo" ) or {}
		pages     = yolo_data.get( "pages" ) or []
		yolo_dpi  = ( yolo_data.get( "meta" ) or {} ).get( "dpi" ) or pdf_mod.DPI
		scale     = images_mod.CROP_DPI / float( yolo_dpi )
		figure_idx , _table_idx = md_mod._build_image_index( pages )

		figs = []
		for page_idx , page_dets in enumerate( pages ):
			# Same figure <-> caption pairing ` prma images ` cropped with , so the
			# text under a card is the text inside its PNG. Unlike method-images an
			# EMPTY caption is fine here -- the crop is the point , and the page
			# renders "( no caption text )" for it.
			for det_idx , caption in mi._page_figures_with_captions( page_dets , scale , images_mod , PP , md_mod ):
				figures_seen += 1
				if not caption:
					captions_empty += 1
				seq = figure_idx.get( ( page_idx , det_idx ) )
				if seq is None:
					continue
				png = all_dir.joinpath( f"{prefix}-figure-{seq}.png" )
				if not png.exists():
					# yolo found a figure but ` prma images ` never cropped this paper.
					missing_png += 1
					continue
				figs.append( {
					"seq":     seq ,
					"page":    page_idx ,
					"caption": caption ,
					"hits":    [] ,        # no keywords here -> no tags , no highlight
					"png":     png ,
					"lead":    False ,
				} )
		if not figs:
			continue
		# Document order : figure 1 first , as they appear in the paper.
		figs.sort( key=lambda f: f[ "seq" ] )

		pdf_path = paper.get( "pdf_path" )
		pdf_path = Path( pdf_path ) if pdf_path else None
		md_path      = md_dir.joinpath( f"{prefix}.md" )
		methods_path = methods_dir.joinpath( f"{prefix}.txt" )
		montage_path = images_dir.joinpath( f"{prefix}-Figures.png" )
		# Which modalities the STUDY used : READ the stamp the ` modalities ` stage
		# pinned on the record , same as method-images ( and stamp the stragglers
		# once , so a paper that report never showed still gets its pills ).
		inferred = False
		try:
			mods = modalities_task.read( args , paper )
			if mods is None:
				mods = modalities_task.stamp( args , key , paper )
			modalities = mods.get( "used" ) or []
			inferred   = bool( mods.get( "inferred" ) )
		except Exception as e:
			if not _mod_warned:
				print( f"ALL-IMAGES :: method tagging unavailable ( {e} ) ; continuing without it" )
				_mod_warned = True
			modalities = []
		if modalities:
			n_with_mods += 1
			n_inferred  += 1 if inferred else 0
		matched.append( {
			"key":     key ,
			"title":   ( paper.get( "title" ) or "" ).strip() ,
			"doi":     paper.get( "doi" ) ,
			"added":     paper.get( "created_at" ) or "" ,
			"published": mi._publication_date( args , paper ) ,
			"mods":    modalities ,
			"mods_inferred": inferred ,
			"pdf":     pdf_path if ( pdf_path and pdf_path.exists() ) else None ,
			"md":      md_path      if md_path.exists()      else None ,
			"methods": methods_path if ( methods_path.exists() and methods_path.stat().st_size > 0 ) else None ,
			"montage": montage_path if montage_path.exists() else None ,
			"figures": figs ,
		} )

	# There's no relevance here -- nothing was searched for -- so data-rank is just
	# a STABLE tiebreak for the page's sorts ( and the fallback order when a paper
	# has no date ) : most figures first , then title. The page's third sort option
	# in this mode is 'Most figures' , which reads data-nfigs directly.
	matched.sort( key=lambda p: ( -len( p[ "figures" ] ) , ( p[ "title" ] or p[ "key" ] ).lower() ) )
	for i , p in enumerate( matched ):
		p[ "rank" ] = i
	# Default DOM order = most-recently-ADDED first , what the page opens on ( stable ,
	# so papers added in one import keep the order above among themselves ).
	matched.sort( key=lambda p: p.get( "added" ) or "" , reverse=True )

	# Figures per modality , for the filter chips ( a paper's figures all inherit
	# its modalities ) , in the config's canonical order.
	mod_counts = {}
	for p in matched:
		for m in p[ "mods" ]:
			mod_counts[ m ] = mod_counts.get( m , 0 ) + len( p[ "figures" ] )

	stats = {
		"skip_not_processed": skip_not_processed ,
		"mod_labels":         methods_vocab.labels( args ) ,
		"mod_counts":         mod_counts ,
	}
	out_path.parent.mkdir( parents=True , exist_ok=True )
	out_path.write_text( _render_report( out_path , matched , stats ) , encoding="utf-8" )

	n_figs = sum( len( p[ "figures" ] ) for p in matched )
	tail = ""
	if missing_png:
		tail = f" ; {missing_png} figure dets had no cropped PNG -- run ` prma images ` to include them"
	if skip_not_processed:
		tail += f" ; {skip_not_processed} papers not processed yet ( run the PDF suite to include them )"
	print(
		f"ALL-IMAGES :: {n_figs} figures across {len( matched )} papers "
		f"( {figures_seen} figure dets seen ; empty-caption={captions_empty} ; "
		f"modality-tagged {n_with_mods}/{len( matched )} papers , "
		f"{n_inferred} inferred from full text )"
		f" -> {out_path}{tail}"
	)
