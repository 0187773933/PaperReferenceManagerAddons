"""
prma process <doi-or-title> : find ONE paper in your reference manager and
run the full per-paper suite on just it, then refresh the dashboard index.

This is the single-paper analogue of running the whole pipeline. The library
tasks ( yolo / ocr / images / methods / md / summarize ) are all idempotent
and plan their work by walking papers.iter_all , so we scope them to one
paper simply by stamping ` args.only_keys = { key } ` for the duration of the
suite ( see papers.iter_all's SCOPING note ) and clearing it again before the
reindex ( which must see the whole library ).

Run order mirrors the standalone commands :

  snapshot           -- so a just-added paper lands in the unified DB ( and
                        its OpenAlex meta is auto-fetched by get_common )
  openalex           -- ensure meta + references + cited-by are cached
  yolo -> ocr -> images -> methods -> code -> md -> modalities
  summarize          -- ONLY with --summarize ( costs LLM calls )
  reindex            -- rebuild the dashboard's index off disk so a running
                        ` prma server ` ( or this process ) shows the paper

The same run_suite() is what the server's live --watch worker calls per
newly-added paper, so single-shot CLI use and real-time processing share one
code path.
"""

import importlib
from pathlib import Path

from ..db    import papers as papers_db
from ..utils import utils


# ---------------------------------------------------------------------------
# Resolve a free-text query ( DOI or title ) to one paper key
# ---------------------------------------------------------------------------

# Same threshold the /exists server uses for fuzzy title identity.
_TITLE_THRESHOLD = 90


def resolve_key( args , query ):
	"""Resolve a user-supplied DOI or title to exactly one primary key in the
	unified DB , or None. DOI is tried first ( normalize + direct existence
	check ) ; otherwise we fuzzy-match the query against library titles with
	the same scorer the server's lookup uses. Returns the matched paper key."""
	q = ( query or "" ).strip()
	if not q:
		return None

	# 1.) DOI : normalize and look up directly.
	nd = utils.normalize_doi( q )
	if nd and papers_db.exists( args , nd ):
		return nd

	# 2.) Title : fuzzy match over the library. ( Make sure we're not scoped
	#     while resolving -- we need to see every paper. )
	prev_only , args.only_keys = getattr( args , "only_keys" , None ) , None
	try:
		choices = {}
		for key , paper in papers_db.iter_all( args ):
			t = utils.normalize_title( paper.get( "title" ) or "" )
			if key and t:
				choices[ key ] = t
	finally:
		args.only_keys = prev_only
	if not choices:
		return None

	from rapidfuzz import fuzz , process as rf
	nt = utils.normalize_title( q )
	best = rf.extractOne(
		nt , choices ,
		scorer       = fuzz.token_sort_ratio ,
		score_cutoff = _TITLE_THRESHOLD ,
	)
	# extractOne over a dict returns ( value , score , key ) -> we want the key.
	return best[ 2 ] if best else None


# ---------------------------------------------------------------------------
# The per-paper suite ( shared by the CLI command and the live worker )
# ---------------------------------------------------------------------------

# Ordered ( stage-label , task-module-name ) for the PDF pipeline. Each module
# exposes run( args ) and plans off papers.iter_all , so all of them honor the
# args.only_keys scope we set below.
_PDF_STAGES = (
	( "yolo"       , "yolo"       ) ,
	( "ocr"        , "ocr"        ) ,
	( "images"     , "images"     ) ,
	( "methods"    , "methods"    ) ,
	( "code"       , "code"       ) ,   # scan abstract + OCR text for source-code links ,
	( "md"         , "md"         ) ,   # BEFORE md so md can render a Source Code section
	( "modalities" , "modalities" ) ,   # AFTER md : its inferred fallback reads the rendered md body
)


# ---------------------------------------------------------------------------
# Per-paper completion stamp ( so we don't re-sweep finished papers forever )
# ---------------------------------------------------------------------------
# The watch backlog used to decide "is this paper done?" purely by looking for
# each stage's OUTPUT file on disk. But some stages legitimately produce NO
# output for some papers -- a paper with no PDF / no detected sections never
# gets an .md ( md skips it ) or a methods .txt , and a dead PDF never gets
# yolo data. Those papers looked perpetually "undone" , so every server restart
# re-swept them and announced them again.
#
# So once the suite has run end-to-end on a paper we STAMP it :
#   paper[ 'processed' ] = { 'at' , 'version' , 'pdf_sig' }
# and the backlog treats a stamped paper as done UNLESS the pipeline changed
# ( SUITE_VERSION bump ) or the paper's PDF changed ( pdf_sig ). A paper that
# can't make further progress is thus processed ONCE , stamped , and then left
# alone -- and stays out of the backlog log -- across restarts.

# Bump when the STAGES change ( one added / removed / materially reworked ) so
# papers processed under the old pipeline re-run once to catch up , then re-stamp.
# v2 : added the ` modalities ` stage ( per-paper modality stamp ).
SUITE_VERSION = 2


def _pdf_sig( paper ):
	"""Cheap signature of the paper's PDF input ( path | mtime | size ) , or ''
	when no PDF is on disk. Changes when a PDF arrives / is replaced , which
	invalidates a 'processed' stamp so the PDF stages re-run."""
	p = paper.get( "pdf_path" )
	if not p:
		return ""
	try:
		st = Path( p ).stat()
		return f"{p}|{int( st.st_mtime )}|{st.st_size}"
	except Exception:
		return f"{p}|missing"


def _has_pending_work( args , key , paper ):
	"""Does any CURRENT stage still lack its output for this paper? -- code never
	scanned , modalities never stamped , or ( when a live , non-failed PDF is on
	disk ) yolo / md / methods missing. NOTE this stays True forever for papers
	that simply CAN'T produce an output ( no sections -> no md / methods ) ;
	needs_processing() layers the 'already attempted' stamp on top so those
	aren't retried endlessly."""
	if paper.get( "code" ) is None:
		return True
	from . import modalities as modalities_task
	if modalities_task.read( args , paper ) is None:
		return True
	pdf = paper.get( "pdf_path" )
	if pdf and not paper.get( papers_db.YOLO_FAILED_KEY ):
		prefix      = utils.doi_to_filename( key )
		has_yolo    = bool( ( paper.get( "yolo" ) or {} ).get( "pages" ) )
		has_md      = args.output.joinpath( "md" , f"{prefix}.md" ).exists()
		has_methods = args.output.joinpath( "methods" , f"{prefix}.txt" ).exists()
		if not ( has_yolo and has_md and has_methods ):
			return True
	return False


def needs_processing( args , key , paper ):
	"""True when the per-paper suite should (re)run on this paper. False once it
	is EITHER fully produced OR already attempted end-to-end at the current
	SUITE_VERSION with the current PDF -- so a paper that can't make further
	progress ( no PDF / no sections / a dead PDF ) is processed ONCE , stamped ,
	and then never re-queued ( no more backlog noise ) until its inputs or the
	pipeline change."""
	if not _has_pending_work( args , key , paper ):
		return False
	proc = paper.get( "processed" ) or {}
	if proc.get( "version" ) == SUITE_VERSION and proc.get( "pdf_sig" ) == _pdf_sig( paper ):
		return False
	return True


def _stamp_processed( args , keys ):
	"""Record that the suite ran end-to-end on each key at this SUITE_VERSION
	with the current PDF inputs. Called by run_suite ONLY on success ( a stage
	that raises propagates past this , so a genuinely-broken paper is retried )."""
	for key in keys:
		paper = papers_db.load( args , key )
		if paper is None:
			continue
		paper[ "processed" ] = {
			"at":      papers_db._utc_now_iso() ,
			"version": SUITE_VERSION ,
			"pdf_sig": _pdf_sig( paper ) ,
		}
		try:
			papers_db.save( args , paper )
		except Exception as e:
			print( f"process :: could not stamp {key} processed ( {e} )" )


def _run_openalex( args ):
	"""Ensure OpenAlex meta + references + cited-by are cached for the scoped
	paper(s). snapshot_view is itself scoped by args.only_keys , so this is a
	one-paper update_cache ( cache-hit fast-path when get_common already
	fetched it ). Failures are logged , never raised."""
	view = papers_db.snapshot_view( args )   # scoped to args.only_keys
	if not view:
		return
	try:
		from ..openalex.openalex import OpenAlex
		oa = OpenAlex( args )
	except Exception as e:
		print( f"process :: openalex skipped ( {e} )" )
		return
	try:
		oa.update_cache( view )
	except Exception as e:
		print( f"process :: openalex update failed ( {e} ) ; continuing" )


def run_suite( args , keys , summarize=False , progress=None , reindex=True ):
	"""Run the full per-paper suite scoped to `keys` ( a list of primary
	keys ). Stamps args.only_keys for the duration so every task touches only
	those papers , restores it before the reindex.

	`progress( stage )` , if given , is called with the short stage label
	( 'openalex' , 'yolo' , ... , 'reindex' ) right before each stage starts ,
	so a caller ( the server worker ) can surface live progress. With no
	callback we just print.

	`reindex=True` ( the CLI default ) rebuilds the dashboard's on-disk index
	at the end so a running server picks the paper up. The live worker passes
	reindex=False and batches a single rebuild after processing a group."""
	keys = [ k for k in ( keys or [] ) if k ]
	if not keys:
		return

	def prog( stage ):
		if progress:
			try: progress( stage )
			except Exception: pass
		else:
			print( f"process :: ---- {stage} ----" )

	prev_only        = getattr( args , "only_keys" , None )
	args.only_keys   = set( keys )
	try:
		prog( "openalex" )
		_run_openalex( args )
		for label , modname in _PDF_STAGES:
			prog( label )
			mod = importlib.import_module( f".{modname}" , package=__package__ )
			mod.run( args )
		if summarize:
			prog( "summarize" )
			from . import summarize as summarize_task
			summarize_task.run( args )
	finally:
		# ALWAYS clear the scope before anything that must see the whole
		# library ( the reindex below , and whatever the caller does next ).
		args.only_keys = prev_only

	# Stamp completion so a restart's backlog doesn't re-sweep papers that have
	# nothing left to produce. Reached only when the stages above didn't raise.
	_stamp_processed( args , keys )

	if reindex:
		prog( "reindex" )
		# indexer.build globs the full papers/ dir ( it does NOT use iter_all ) ,
		# so the index stays whole -- it just incrementally picks up the paper
		# we changed. Pure disk read ; get_common / _run_openalex already
		# refreshed the cache.
		from ..dashboard import indexer
		indexer.build( args )
		refresh_reports( args , progress=prog )


# ---------------------------------------------------------------------------
# Library-wide artifacts the per-paper stages invalidate
# ---------------------------------------------------------------------------

def refresh_reports( args , progress=None ):
	"""Rebuild the whole-library artifacts that new papers make stale. Kept OUT
	of _PDF_STAGES on purpose : those are per-paper and scoped by only_keys ,
	while this re-reads every paper ( ` prma method-images ` sweeps 15k figure
	captions ) , so running it inside the loop would redo the whole library once
	per paper. It belongs where the reindex is -- at the END of a run , once ,
	after the papers have landed. The server's --watch worker calls it once per
	BATCH for the same reason.

	Never raises : a broken report must not fail the processing run.

	BOTH figure reports , sequentially : /method-images ( caption-matched ) and
	/images ( every crop ). They read the same records and stamp the same
	modality field , so there's nothing to gain from overlapping them."""
	from . import all_images , method_images
	for label , mod in ( ( "method-images" , method_images ) , ( "all-images" , all_images ) ):
		if progress:
			try: progress( label )
			except Exception: pass
		mod.rebuild( args )


# ---------------------------------------------------------------------------
# CLI entry : prma process <doi-or-title>
# ---------------------------------------------------------------------------

def run( args ):
	# 1.) Snapshot ( unless --skip-snapshot ) so a paper you JUST added to
	#     Zotero / Mendeley is in the unified DB before we look it up.
	#     get_common also auto-fetches OpenAlex meta for any brand-new DOI.
	if not getattr( args , "skip_snapshot" , False ):
		from . import snapshot as snapshot_task
		snapshot_task.get_common( args )

	# 2.) Resolve the query to exactly one paper.
	query = getattr( args , "process_query" , "" )
	key   = resolve_key( args , query )
	if not key:
		print(
			f"process :: no paper in your library matches {query!r} . "
			f"Try the exact DOI , or run ` prma snapshot ` first if you just "
			f"added it."
		)
		return

	paper = papers_db.load( args , key ) or {}
	title = paper.get( "title" ) or key
	print( f"process :: {key} — {title}" )

	# 3.) Full per-paper suite , then reindex so the dashboard reflects it.
	run_suite(
		args , [ key ] ,
		summarize = getattr( args , "process_summarize" , False ) ,
		reindex   = True ,
	)
	print( f"process :: done — {title} ; dashboard index refreshed." )
