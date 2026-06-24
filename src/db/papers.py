"""
Unified per-paper store.

ONE source of truth for everything we know about a paper , keyed by
normalized DOI. Replaces the previous per-manager outputs
( output/cache/yolo/{manager}/* , output/text/{manager}/* ) so the
downstream tasks ( yolo , ocr , images , ... ) work off a single DB
instead of duplicating work between Mendeley and Zotero entries.

File layout :
  output/cache/papers/{doi_to_filename(doi)}.json

Each file holds the full record :
  {
    "doi":     "10.1016/j.cub.2025.01.024" ,
    "title":   "..." ,
    "sources": {                            -- which managers know this paper
      "zotero":   { "key": "..." , "itemID": ... , "pdfs": [...] , ... } ,
      "mendeley": { "id":  "..." , "pdfs": [...] , ... }
    } ,
    "pdf_path": "/abs/first-existing.pdf"   -- best on-disk PDF resolved across
                                               every source's pdfs ; null if
                                               nothing is on disk yet.
    -- added later by other tasks --
    "yolo":    { "meta": {...} , "pages": [...] }      -- src/pdf/yolo.py output ,
                                                          augmented with 'ocr' on
                                                          each detection
  }

The 'ocr' results live pinned per-detection inside the yolo structure :
  yolo.pages[*][*].ocr = { engine_name: "decoded text blob" , ... }
so multiple engines can coexist on the same detection.

Functions here are stateless : they all take ( args , ... ) and read /
write the file system directly. No in-process caching ; concurrent
writers would race , so don't.
"""

import re
from pathlib import Path
from datetime import datetime , timezone

from ..utils import utils

# Constants used by callers ( so the magic-string version of these
# doesn't leak into other modules ).
SOURCE_ZOTERO   = "zotero"
SOURCE_MENDELEY = "mendeley"


# ---------------------------------------------------------------------------
# Primary key
# ---------------------------------------------------------------------------
# A record's primary key is its real normalized DOI when it has one , else a
# SYNTHETIC key of the form  nodoi-<source>-<sanitized item id> . This lets a
# paper that has no DOI ( yet ) still live in the unified store and flow through
# the PDF pipeline ( yolo / images / md / methods / text / ocr / preprocess )
# exactly like a DOI'd paper -- those tasks only ever treat the key as opaque.
#
# DOI stays a real field on the record ( null for no-doi papers ) ; the synthetic
# key is stored separately under 'key'. Consumers that genuinely need a real DOI
# ( OpenAlex meta fetch , the doi.org url ) gate on paper[ 'doi' ] truthiness.

NO_DOI_PREFIX = "nodoi-"


def synthetic_key( source_name , source_item_key ):
	"""Stable , filesystem / Windows-safe primary key for a paper that has no
	DOI. Built from the manager name + that manager's own item id ( Zotero
	item key / Mendeley id ) so it's stable across runs and never empty."""
	safe = re.sub( r"[^A-Za-z0-9]+" , "-" , str( source_item_key or "" ) ).strip( "-" )
	return f"{NO_DOI_PREFIX}{source_name}-{safe}"


def is_synthetic_key( key ):
	return bool( key ) and str( key ).startswith( NO_DOI_PREFIX )


def record_key( paper ):
	"""Primary key of a record : its real normalized DOI if present , else its
	synthetic 'key'. Returns None for a record that has neither."""
	doi = utils.normalize_doi( ( paper or {} ).get( "doi" ) )
	if doi:
		return doi
	return ( paper or {} ).get( "key" )


def _normalize_lookup( key ):
	"""Normalize a lookup key. Synthetic ( nodoi-... ) keys pass through
	unchanged ; everything else goes through DOI normalization."""
	if is_synthetic_key( key ):
		return key
	return utils.normalize_doi( key )


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def papers_dir( args ):
	"""Path to the unified papers/ directory ; created if missing."""
	d = args.output.joinpath( "cache" , "papers" )
	d.mkdir( parents=True , exist_ok=True )
	return d


def paper_path( args , key ):
	"""Path to one paper's JSON file. `key` is the record's primary key --
	a normalized DOI or a synthetic nodoi-... key ; both are filename-safe
	once run through doi_to_filename ( synthetic keys carry no / \\ : )."""
	prefix = utils.doi_to_filename( key )
	return papers_dir( args ).joinpath( f"{prefix}.json" )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def load( args , key ):
	"""Load a paper record by primary key ( normalized DOI or synthetic
	nodoi-... key ) , or None if not in the DB. Normalizes the key before
	looking up so callers can pass raw input."""
	key = _normalize_lookup( key )
	if not key:
		return None
	p = paper_path( args , key )
	if not p.exists():
		return None
	try:
		return utils.read_json( p )
	except Exception:
		return None


def exists( args , key ):
	key = _normalize_lookup( key )
	if not key:
		return False
	return paper_path( args , key ).exists()


def iter_all( args , only=None ):
	"""Yield every ( key , paper_dict ) currently in the DB , where key is
	the record's primary key ( normalized DOI , or synthetic nodoi-... key
	for papers that have no DOI ).

	SCOPING. `only` -- or ` getattr( args , 'only_keys' , None ) ` when `only`
	is left at its default -- is an optional iterable of primary keys ( raw
	DOIs / synthetic keys , normalized here ). When present , iteration is
	restricted to JUST those papers and they're loaded DIRECTLY by key ( no
	full-directory walk ) , so a one-paper scope reads one file instead of
	the whole library. This is the single chokepoint that lets ` prma process
	<doi> ` and the server's live --watch worker run the per-paper PDF tasks
	( yolo / ocr / images / preprocess / md / methods / summarize -- they ALL
	plan off iter_all ) against one freshly-added paper : the caller sets
	` args.only_keys = { key } ` , every task auto-scopes , and the caller
	clears it again.

	CAUTION : the snapshot-diff helper ( tasks/snapshot._snapshot_dois ) and
	the dashboard indexer must see the WHOLE library , so callers MUST set
	only_keys ONLY around the scoped task phase -- never across a snapshot or
	reindex."""
	scope = only if only is not None else getattr( args , "only_keys" , None )
	if scope is not None:
		seen = set()
		for raw in scope:
			nk = _normalize_lookup( raw )
			if not nk or nk in seen:
				continue
			seen.add( nk )
			paper = load( args , nk )
			if paper is None:
				continue
			key = record_key( paper )
			if key:
				yield key , paper
		return
	for p in sorted( papers_dir( args ).glob( "*.json" ) ):
		try:
			data = utils.read_json( p )
		except Exception:
			continue
		key = record_key( data )
		if key:
			yield key , data


def count( args ):
	"""How many paper records exist."""
	return sum( 1 for _ in papers_dir( args ).glob( "*.json" ) )


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def _all_pdfs( paper ):
	"""Iterate every pdf path from every source , in source-name order
	then list order."""
	for src_name in sorted( ( paper.get( "sources" ) or {} ).keys() ):
		for p in ( paper.get( "sources" )[ src_name ].get( "pdfs" ) or [] ):
			if p:
				yield str( p )


def _resolve_pdf_path( paper ):
	"""Return the first pdf path that exists on disk , or None.
	Recomputed every save so renames / re-downloads pick up next run."""
	for raw in _all_pdfs( paper ):
		if Path( raw ).exists():
			return raw
	# Nothing exists yet ; still record a best-guess pointer ( first
	# path , even if missing ) so downstream code has something to log.
	for raw in _all_pdfs( paper ):
		return raw
	return None


def save( args , paper ):
	"""Write a paper record to disk. Requires a primary key -- either a
	normalizable paper[ 'doi' ] or a synthetic paper[ 'key' ]. Returns the
	path written. Side effects :
	  - normalizes the DOI in-place ( left as None for no-doi papers ) ,
	  - recomputes paper[ 'pdf_path' ] ( first existing across sources ) ,
	  - stamps paper[ 'updated_at' ] ."""
	paper[ "doi" ] = utils.normalize_doi( paper.get( "doi" ) )
	key = record_key( paper )
	if not key:
		raise ValueError( "papers.save : paper needs a 'doi' or synthetic 'key'" )
	paper[ "pdf_path" ] = _resolve_pdf_path( paper )
	paper[ "updated_at" ] = _utc_now_iso()
	p = paper_path( args , key )
	utils.write_json( p , paper )
	return p


def _utc_now_iso():
	return datetime.now( timezone.utc ).replace( microsecond=0 ).isoformat()


# ---------------------------------------------------------------------------
# YOLO failure marker
# ---------------------------------------------------------------------------
# A PDF that YOLO can't even load ( corrupt / truncated file , PDFium
# "Data format error" , ... ) used to be retried on every single run :
# the planning loops in yolo / ocr / images all see a paper with no
# 'yolo' field , queue it , run YOLO inline , and fail again. The marker
# below records that failure so those loops can skip the paper next time
# instead of re-attempting the same doomed load. It's cleared the moment
# YOLO succeeds for the paper ( e.g. the file was replaced ) , and each
# task's --force overrides the skip so the user can always force a retry.

YOLO_FAILED_KEY = "yolo_failed"


def mark_yolo_failed( args , doi , error , pdf_name=None ):
	"""Load the paper , stamp a yolo_failed marker , and save. Called from
	the inline-YOLO except branches in yolo / ocr / images. Also surfaces the
	failure in the unified problems log under kind 'yolo'."""
	paper = load( args , doi )
	if paper is None:
		return
	paper[ YOLO_FAILED_KEY ] = {
		"at":    _utc_now_iso() ,
		"error": str( error ) ,
		"pdf":   pdf_name ,
	}
	save( args , paper )
	report_problem( args , "yolo" , doi , detail=str( error ) , extra={ "pdf": pdf_name } )


def clear_yolo_failed( args , paper ):
	"""Drop any stale yolo_failed marker from an in-memory paper record and
	the matching 'yolo' problems-log entry. Call right before saving a
	successful YOLO result. Returns True if a marker was actually present
	( so callers can log a recovery )."""
	had = paper.pop( YOLO_FAILED_KEY , None ) is not None
	clear_problem( args , "yolo" , record_key( paper ) )
	return had


def _strip_cosmetic( source ):
	"""Drop the timestamp field that we re-stamp on every upsert , so
	source-vs-source equality compares the substantive data only."""
	return { k: v for k , v in ( source or {} ).items() if k != "updated_at" }


def upsert_source(
	args , doi , source_name , source_fields ,
	title=None , key=None ,
):
	"""Insert or update one paper's record :
	  - creates the paper if missing ;
	  - replaces paper[ 'sources' ][ source_name ] with `source_fields`
	    ( so re-snapshotting Zotero refreshes its slice without touching
	    Mendeley's ) ;
	  - sets the title if not already set ;
	  - lets save() recompute paper[ 'pdf_path' ] from the union of
	    every source's 'pdfs' field.

	source_fields is expected to carry a 'pdfs' list of absolute
	paths under that manager.

	IDEMPOTENT : if the incoming source data matches what's already
	saved ( ignoring the cosmetic 'updated_at' timestamp ) AND no
	title fill-in is happening , we SKIP save() and return changed=
	False. This is what makes re-snapshotting on an unchanged library
	cheap -- otherwise we'd JSON-rewrite every 100KB+ paper file even
	when nothing actually changed.

	A paper with no DOI is keyed by the synthetic `key` ( see synthetic_key )
	the caller passes ; its 'doi' field stays None. When a real DOI is given
	it wins and `key` is ignored.

	Returns ( paper , was_created , was_changed ) :
	  - was_created : the paper didn't exist before this call ;
	  - was_changed : save() actually ran ( file was rewritten ).
	    Always True when was_created is True."""
	nd = utils.normalize_doi( doi )
	if nd:
		pkey , real_doi = nd , nd
	elif key:
		pkey , real_doi = key , None
	else:
		return None , False , False
	existing = load( args , pkey )
	created = existing is None
	if created:
		existing = {
			"doi":        real_doi ,
			"key":        pkey ,
			"title":      None ,
			"sources":    {} ,
			"pdf_path":   None ,
			"created_at": _utc_now_iso() ,
		}

	incoming      = dict( source_fields or {} )
	existing_src  = ( existing.get( "sources" ) or {} ).get( source_name ) or {}
	title_change  = bool( title and not existing.get( "title" ) )
	data_changed  = _strip_cosmetic( existing_src ) != _strip_cosmetic( incoming )

	if not created and not data_changed and not title_change:
		# No substantive change -- skip the JSON rewrite entirely.
		return existing , False , False

	incoming[ "updated_at" ] = _utc_now_iso()
	existing.setdefault( "sources" , {} )[ source_name ] = incoming
	if title_change:
		existing[ "title" ] = title
	save( args , existing )
	return existing , created , True


def remove_source( args , key , source_name ):
	"""Detach a source from a paper ( looked up by primary key -- DOI or
	synthetic nodoi-... key ). If no sources remain the file is deleted
	entirely. Returns True if anything changed."""
	key = _normalize_lookup( key )
	if not key:
		return False
	paper = load( args , key )
	if not paper:
		return False
	srcs = paper.get( "sources" , {} )
	if source_name not in srcs:
		return False
	del srcs[ source_name ]
	if not srcs:
		paper_path( args , key ).unlink( missing_ok=True )
		return True
	save( args , paper )
	return True


def prune_source( args , source_name , kept_keys ):
	"""Sync semantics for `source_name` : any paper in the DB that has
	`sources[ source_name ]` but whose primary key is NOT in `kept_keys`
	gets that source detached. If a paper ends up with no sources at all ,
	its file is deleted entirely.

	Returns ( n_source_removed , n_paper_deleted ).

	Use this after a full snapshot of a single manager : collect every
	key you saw in the snapshot ( real DOIs and synthetic nodoi-... keys )
	into `kept_keys` , then call this to bring the DB in sync with what's
	actually still in that manager. Only safe to call when the snapshot is
	authoritative ( e.g. the Zotero SQLite is a full local copy ; the
	Mendeley API uses incremental fetch from a cache , so a missed page
	would cause false-positive deletions there )."""
	# Normalize the kept set once so caller can pass raw DOIs / synthetic keys.
	kept_norm = set()
	for d in kept_keys or []:
		nk = _normalize_lookup( d )
		if nk:
			kept_norm.add( nk )

	n_source_removed , n_paper_deleted = 0 , 0
	# Materialize the list so we can mutate the directory while iterating.
	for key , paper in list( iter_all( args ) ):
		srcs = paper.get( "sources" ) or {}
		if source_name not in srcs:
			continue
		if key in kept_norm:
			continue
		del srcs[ source_name ]
		if not srcs:
			paper_path( args , key ).unlink( missing_ok=True )
			n_paper_deleted += 1
		else:
			save( args , paper )
			n_source_removed += 1
	return n_source_removed , n_paper_deleted


# ---------------------------------------------------------------------------
# Problems log
# ---------------------------------------------------------------------------
# ONE human-readable record of everything the pipeline couldn't fully
# process , so trouble spots aren't buried in task scrollback. Every stage
# reports as it goes and clears entries when a later run recovers , so the
# file reflects CURRENT problems , not a growing history.
#
#   output/cache/problems.json
#   {
#     "<kind>": {
#       "<id>": { "kind" , "id" , "detail" , "first_seen" , "last_seen" ,
#                 ...kind-specific extras } ,
#       ...
#     } ,
#     ...
#   }
#
# kinds currently in use :
#   non_imported:zotero   - snapshot skipped a Zotero item ( no doi/title/pdf )
#   non_imported:mendeley - same , for Mendeley
#   openalex_meta         - OpenAlex returned nothing for the paper's DOI
#   openalex_ref          - a referenced work came back empty from OpenAlex
#   yolo                  - PDF couldn't be loaded / parsed by YOLO
#   ocr                   - OCR engine failed on a PDF
#
# Each entry is keyed by id inside its kind , so re-reporting the same
# failure just refreshes last_seen instead of duplicating ( the "unique
# list" guarantee ). Snapshot kinds are replaced wholesale each run
# ( replace_problem_kind ) so resolved items drop out ; per-paper failures
# are upserted ( report_problem* ) and removed on success ( clear_problem* ).

PROBLEMS_FILE = "problems.json"


def problems_path( args ):
	"""Path to the unified problems log."""
	return args.output.joinpath( "cache" , PROBLEMS_FILE )


def load_problems( args ):
	"""Whole problems log as a { kind: { id: entry } } dict ( {} if none )."""
	p = problems_path( args )
	if not p.exists():
		return {}
	try:
		return utils.read_json( p ) or {}
	except Exception:
		return {}


def _save_problems( args , data ):
	# Drop empty kinds so a fully-recovered library leaves a tidy {} .
	data = { k: v for k , v in data.items() if v }
	p = problems_path( args )
	p.parent.mkdir( parents=True , exist_ok=True )
	utils.write_json( p , data )


def _coerce_problem( it ):
	"""Normalize one reported item into ( ident , detail , extra ). Accepts a
	bare id , a ( id , detail ) / ( id , detail , extra ) tuple , or a dict
	carrying an 'id' ( + optional 'detail' ) plus arbitrary extra fields."""
	if isinstance( it , dict ):
		d = dict( it )
		return d.pop( "id" , None ) , d.pop( "detail" , None ) , ( d or None )
	if isinstance( it , ( tuple , list ) ):
		ident  = it[ 0 ] if len( it ) > 0 else None
		detail = it[ 1 ] if len( it ) > 1 else None
		extra  = it[ 2 ] if len( it ) > 2 else None
		return ident , detail , extra
	return it , None , None


def _problem_entry( kind , ident , detail , extra , now , prev=None ):
	e = dict( prev or {} )
	e[ "kind" ] = kind
	e[ "id" ]   = str( ident )
	if detail is not None:
		e[ "detail" ] = detail
	if extra:
		e.update( extra )
	e.setdefault( "first_seen" , now )
	e[ "last_seen" ] = now
	return e


def report_problems( args , kind , items ):
	"""Bulk upsert problems of `kind` ( one read+write for the batch ).
	`items` is an iterable of anything _coerce_problem accepts. Returns the
	number of entries written. Prefer this over a report_problem() loop when
	you have many at once ( e.g. a paper's failed references )."""
	items = list( items or [] )
	if not items:
		return 0
	now = _utc_now_iso()
	data = load_problems( args )
	bucket = data.setdefault( kind , {} )
	n = 0
	for it in items:
		ident , detail , extra = _coerce_problem( it )
		if ident is None:
			continue
		key = str( ident )
		bucket[ key ] = _problem_entry( kind , ident , detail , extra , now , bucket.get( key ) )
		n += 1
	if n:
		_save_problems( args , data )
	return n


def report_problem( args , kind , ident , detail=None , extra=None ):
	"""Upsert a single problem. See report_problems for bulk."""
	return report_problems( args , kind , [ ( ident , detail , extra ) ] )


def clear_problems( args , kind , idents ):
	"""Drop the given ids from `kind` ( call when a later run succeeds ).
	Returns how many were actually present and removed."""
	idents = [ str( i ) for i in ( idents or [] ) if i is not None ]
	if not idents:
		return 0
	data = load_problems( args )
	bucket = data.get( kind )
	if not bucket:
		return 0
	n = sum( 1 for i in idents if bucket.pop( i , None ) is not None )
	if n:
		_save_problems( args , data )
	return n


def clear_problem( args , kind , ident ):
	"""Drop a single id from `kind`. Returns True if it was present."""
	return clear_problems( args , kind , [ ident ] ) > 0


def replace_problem_kind( args , kind , items ):
	"""Replace ALL entries of `kind` with `items` ( wholesale ). Use when the
	caller knows the COMPLETE current set for that kind -- e.g. a manager
	snapshot listing every skipped item -- so entries that no longer qualify
	( the item gained a title / DOI ) drop out. first_seen is preserved for
	ids that survive. Returns the resulting count."""
	now = _utc_now_iso()
	data = load_problems( args )
	prev = data.get( kind ) or {}
	bucket = {}
	for it in ( items or [] ):
		ident , detail , extra = _coerce_problem( it )
		if ident is None:
			continue
		key = str( ident )
		bucket[ key ] = _problem_entry( kind , ident , detail , extra , now , prev.get( key ) )
	data[ kind ] = bucket
	_save_problems( args , data )
	return len( bucket )


def save_non_imported( args , source_name , items ):
	"""Record the manager items a snapshot skipped ( no DOI , no title , and
	no PDF -- nothing for the pipeline to act on ) into the unified problems
	log under kind 'non_imported:<source>'. Replaced wholesale each snapshot
	so an item that later gains a title / DOI drops off automatically.
	Returns the count recorded."""
	return replace_problem_kind( args , f"non_imported:{source_name}" , items )


# ---------------------------------------------------------------------------
# Backward-compat view : reconstruct a flat "snapshot dict" ( keyed by
# DOI ) from the unified store so the OpenAlex update / stats / search
# pipeline in tasks/main.py can consume it without caring whether the
# data came from Zotero , Mendeley , or both.
# ---------------------------------------------------------------------------

def snapshot_view( args ):
	"""Return a dict keyed by DOI in roughly the shape that the
	per-manager snapshot() functions used to return :
	  { doi : { 'doi' , 'title' , 'pdfs' ( union ) , 'pdf_path' ,
	            'sources' } }
	OpenAlex / stats / search code only reads doi + title + url , so
	the minimal fields are filled in ; everything else is passed
	through under 'sources' for callers that want more detail."""
	out = {}
	for key , paper in iter_all( args ):
		real_doi = utils.normalize_doi( paper.get( "doi" ) )
		out[ key ] = {
			"doi":      real_doi ,            # None for no-doi papers
			"id":       key ,                 # legacy code keys on 'id' ; drives
			                                  # OpenAlex's per-paper cache file
			"title":    paper.get( "title" ) ,
			"url":      f"https://doi.org/{real_doi}" if real_doi else None ,
			"pdfs":     list( _all_pdfs( paper ) ) ,
			"pdf_path": paper.get( "pdf_path" ) ,
			"sources":  paper.get( "sources" , {} ) ,
		}
	return out
