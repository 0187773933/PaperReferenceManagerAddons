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

from pathlib import Path
from datetime import datetime , timezone

from ..utils import utils

# Constants used by callers ( so the magic-string version of these
# doesn't leak into other modules ).
SOURCE_ZOTERO   = "zotero"
SOURCE_MENDELEY = "mendeley"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def papers_dir( args ):
	"""Path to the unified papers/ directory ; created if missing."""
	d = args.output.joinpath( "cache" , "papers" )
	d.mkdir( parents=True , exist_ok=True )
	return d


def paper_path( args , doi ):
	"""Path to one paper's JSON file. Caller is responsible for passing
	an already-normalized DOI."""
	prefix = utils.doi_to_filename( doi )
	return papers_dir( args ).joinpath( f"{prefix}.json" )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def load( args , doi ):
	"""Load a paper record by DOI , or None if not in the DB.
	Normalizes the DOI before looking up so callers can pass raw input."""
	doi = utils.normalize_doi( doi )
	if not doi:
		return None
	p = paper_path( args , doi )
	if not p.exists():
		return None
	try:
		return utils.read_json( p )
	except Exception:
		return None


def exists( args , doi ):
	doi = utils.normalize_doi( doi )
	if not doi:
		return False
	return paper_path( args , doi ).exists()


def iter_all( args ):
	"""Yield every ( doi , paper_dict ) currently in the DB."""
	for p in sorted( papers_dir( args ).glob( "*.json" ) ):
		try:
			data = utils.read_json( p )
		except Exception:
			continue
		doi = data.get( "doi" )
		if doi:
			yield doi , data


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
	"""Write a paper record to disk. Requires paper[ 'doi' ] to be set
	and normalizable. Returns the path written. Side effects :
	  - normalizes the DOI in-place ,
	  - recomputes paper[ 'pdf_path' ] ( first existing across sources ) ,
	  - stamps paper[ 'updated_at' ] ."""
	doi = utils.normalize_doi( paper.get( "doi" ) )
	if not doi:
		raise ValueError( "papers.save : paper[ 'doi' ] is required" )
	paper[ "doi" ] = doi
	paper[ "pdf_path" ] = _resolve_pdf_path( paper )
	paper[ "updated_at" ] = _utc_now_iso()
	p = paper_path( args , doi )
	utils.write_json( p , paper )
	return p


def _utc_now_iso():
	return datetime.now( timezone.utc ).replace( microsecond=0 ).isoformat()


def upsert_source(
	args , doi , source_name , source_fields ,
	title=None ,
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

	Returns ( paper , was_created )."""
	doi = utils.normalize_doi( doi )
	if not doi:
		return None , False
	existing = load( args , doi )
	created = existing is None
	if created:
		existing = {
			"doi":        doi ,
			"title":      None ,
			"sources":    {} ,
			"pdf_path":   None ,
			"created_at": _utc_now_iso() ,
		}
	source_fields = dict( source_fields or {} )
	source_fields[ "updated_at" ] = _utc_now_iso()
	existing.setdefault( "sources" , {} )[ source_name ] = source_fields
	if title and not existing.get( "title" ):
		existing[ "title" ] = title
	save( args , existing )
	return existing , created


def remove_source( args , doi , source_name ):
	"""Detach a source from a paper. If no sources remain the file is
	deleted entirely. Returns True if anything changed."""
	doi = utils.normalize_doi( doi )
	if not doi:
		return False
	paper = load( args , doi )
	if not paper:
		return False
	srcs = paper.get( "sources" , {} )
	if source_name not in srcs:
		return False
	del srcs[ source_name ]
	if not srcs:
		paper_path( args , doi ).unlink( missing_ok=True )
		return True
	save( args , paper )
	return True


def prune_source( args , source_name , kept_dois ):
	"""Sync semantics for `source_name` : any paper in the DB that has
	`sources[ source_name ]` but whose DOI is NOT in `kept_dois` gets
	that source detached. If a paper ends up with no sources at all ,
	its file is deleted entirely.

	Returns ( n_source_removed , n_paper_deleted ).

	Use this after a full snapshot of a single manager : collect every
	DOI you saw in the snapshot into `kept_dois` , then call this to
	bring the DB in sync with what's actually still in that manager.
	Only safe to call when the snapshot is authoritative ( e.g. the
	Zotero SQLite is a full local copy ; the Mendeley API uses
	incremental fetch from a cache , so a missed page would cause
	false-positive deletions there )."""
	# Normalize the kept set once so caller can pass raw DOIs.
	kept_norm = set()
	for d in kept_dois or []:
		nd = utils.normalize_doi( d )
		if nd:
			kept_norm.add( nd )

	n_source_removed , n_paper_deleted = 0 , 0
	# Materialize the list so we can mutate the directory while iterating.
	for doi , paper in list( iter_all( args ) ):
		srcs = paper.get( "sources" ) or {}
		if source_name not in srcs:
			continue
		if doi in kept_norm:
			continue
		del srcs[ source_name ]
		if not srcs:
			paper_path( args , doi ).unlink( missing_ok=True )
			n_paper_deleted += 1
		else:
			save( args , paper )
			n_source_removed += 1
	return n_source_removed , n_paper_deleted


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
	for doi , paper in iter_all( args ):
		out[ doi ] = {
			"doi":      doi ,
			"id":       doi ,                # legacy code keys on 'id'
			"title":    paper.get( "title" ) ,
			"url":      f"https://doi.org/{doi}" ,
			"pdfs":     list( _all_pdfs( paper ) ) ,
			"pdf_path": paper.get( "pdf_path" ) ,
			"sources":  paper.get( "sources" , {} ) ,
		}
	return out
