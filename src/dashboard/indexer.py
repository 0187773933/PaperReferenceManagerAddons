"""
Dashboard full-text index -- self-contained, incremental, persisted.

This OWNS the dashboard's data. It does NOT touch the missing.xlsx pipeline
( OpenAlexStats.compute / missing_index_cache ) -- it replaces it for
browsing. The index is built by streaming the OpenAlex cache straight off
disk :

  library papers   output/cache/papers/*.json            ( identity only : doi / title / wid )
  library OA meta  output/cache/openalex/<b64(doi)>.json ( referenced_works , cited_by_works )
  reference metas  output/cache/openalex/references/<wid>.json   ( read once each )

The dashboard is for the papers you DON'T have -- your own library lives in
Zotero. So we index only the EXTERNAL pools :
  references  -- works your library CITES that you don't already have
                 ( the "missing" view ) , tallied by how many of your papers
                 cite each
  cited_by    -- works that CITE your library that you don't already have

The library is read only for its identity ( doi / title / wid ) , to know
what to exclude as "already have" ; we never build a full-text haystack for
it ( that was the most expensive , and now-pointless , part ).

Incrementality. State is persisted ( gzipped ) keyed per library paper by a
file signature. On reindex we only :
  - re-read papers whose file changed ( + brand-new papers ) ,
  - stream reference JSONs we haven't already cached an entry for .
Dedup ( is a work already in your library? ) is EXACT -- DOI match or
normalized-title match , both O(1) set lookups -- so it's recomputed fresh
every build in milliseconds. ( missing.xlsx keeps the slower fuzzy dedup ;
the dashboard trades that bit of recall for speed. )

So adding a handful of papers costs ~the new papers + their new references,
not a full rebuild. Pass full=True ( ` prma reindex --full ` ) to ignore the
saved state and rebuild from scratch ( e.g. after regenerating text /
summaries , which the per-paper signature doesn't watch ).
"""

import gzip
import json
import time
from pathlib import Path
from collections import Counter

from tqdm import tqdm

from ..db    import papers as papers_db
from ..utils import utils
from .       import index as dash_index


# Bump whenever the per-paper entry SHAPE changes ( fields added / removed )
# so a stale on-disk index is discarded and fully rebuilt -- the incremental
# path only re-reads papers whose FILE changed , so new fields on unchanged
# papers would otherwise never get populated.
#   7 -> 8 : library entries gained montage / has_md + full-text OCR haystack
#   8 -> 9 : library entries gained created_at ( In-Library "Added" column )
STATE_VERSION = 9


# ---------------------------------------------------------------------------
# OpenAlex work -> compact searchable entry
# ---------------------------------------------------------------------------

def _reconstruct_abstract( inv_index ):
	if not inv_index:
		return ""
	positions = [ ( pos , word ) for word , poses in inv_index.items() for pos in poses ]
	positions.sort()
	return " ".join( w for _ , w in positions )


def _make_haystack( meta ):
	title    = meta.get( "title" ) or meta.get( "display_name" ) or ""
	abstract = _reconstruct_abstract( meta.get( "abstract_inverted_index" ) )
	return ( title + " " + abstract ).lower()


def _ocr_fulltext( paper ):
	"""Concatenate the OCR text pinned on a library paper's YOLO detections
	( ` prma ocr ` output ) into one blob -- the full body text , so the
	'In Library' tab searches the WHOLE paper , not just title + abstract.
	One engine's text per detection ( whichever is present )."""
	parts = []
	for page in ( ( paper.get( "yolo" ) or {} ).get( "pages" ) or [] ):
		for det in ( page or [] ):
			ocr = det.get( "ocr" ) or {}
			for v in ocr.values():
				if v:
					parts.append( v )
					break
	return " ".join( parts )


def _work_entry( meta ):
	"""Library-independent compact entry for one OpenAlex work ( reference or
	cited-by ) : the missing.xlsx scalars + a title+abstract haystack."""
	e = utils.openalex_entry_scalars( meta )
	e[ "hay" ] = _make_haystack( meta )
	return e


def _wid( url ):
	return url.rsplit( "/" , 1 )[ -1 ] if url else ""


# ---------------------------------------------------------------------------
# Persistence ( gzipped JSON state )
# ---------------------------------------------------------------------------

def store_path( args ):
	return Path( args.output ).joinpath( "cache" , "dashboard_index.json.gz" )


def state_mtime( args ):
	p = store_path( args )
	return p.stat().st_mtime if p.exists() else 0.0


def load_state( args ):
	p = store_path( args )
	if not p.exists():
		return None
	try:
		with gzip.open( p , "rt" , encoding="utf-8" ) as f:
			state = json.load( f )
	except Exception as e:
		print( f"dashboard :: index unreadable ( {e} ) ; rebuilding" )
		return None
	if state.get( "version" ) != STATE_VERSION:
		return None
	return state


def save_state( args , state ):
	p = store_path( args )
	p.parent.mkdir( parents=True , exist_ok=True )
	tmp = p.with_suffix( ".tmp" )
	# Low compression level : this is a local cache , and the haystacks make
	# it big ( hundreds of MB ) , so fast writes matter far more than a few
	# extra MB on disk. Level 3 writes ~3-4x faster than the gzip default.
	with gzip.open( tmp , "wt" , encoding="utf-8" , compresslevel=3 ) as f:
		json.dump( state , f , ensure_ascii=False )
	tmp.replace( p )
	return p


def _empty_state():
	return {
		"version":     STATE_VERSION ,
		"built_at":    None ,
		"paper_state": {} ,   # FILENAME -> { sig , key , wid , doi , title , ref_wids , cb_wids }
		"ref_meta":    {} ,   # wid -> reference entry ( incl 'hay' )
		"cb_meta":     {} ,   # wid -> cited-by entry ( incl 'hay' )
		"lib_meta":    {} ,   # paper key -> library entry ( incl 'hay' ) -- the
		                      # papers you HAVE , searchable on their own tab
	}


# ---------------------------------------------------------------------------
# Dedup ( EXACT : DOI or normalized-title set membership -- O(1) per work )
# ---------------------------------------------------------------------------

def _compute_dedup( tally , meta , lib_index ):
	"""dup[ wid ] = is this work already in the library ( exact DOI / title )?
	Works with no metadata are marked dup so they don't pollute the pool."""
	dup = {}
	for wid in tally:
		e = meta.get( wid )
		if not e:
			dup[ wid ] = True
		else:
			dup[ wid ] = utils.is_library_dup_fields(
				e.get( "doi" ) , e.get( "title" ) , lib_index , fuzzy=False )
	return dup


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build( args , full=False , log=print , progress=None ):
	"""Build ( or incrementally refresh ) the index from the on-disk caches
	and persist it. Returns the servable pools dict. Pure disk reads -- the
	caller is responsible for having refreshed the OpenAlex cache first
	( see reindex() ).

	`progress` , if given , is called with short human status strings as the
	build moves through its phases ( the server forwards these to the
	dashboard so a long first build never looks stalled )."""
	def prog( msg ):
		if progress:
			try: progress( msg )
			except Exception: pass

	t0 = time.time()
	state = ( None if full else load_state( args ) ) or _empty_state()
	paper_state = state[ "paper_state" ]
	ref_meta    = state[ "ref_meta" ]
	cb_meta     = state[ "cb_meta" ]
	lib_meta    = state.setdefault( "lib_meta" , {} )

	oa_dir   = Path( args.output ).joinpath( "cache" , "openalex" )
	refs_dir = oa_dir.joinpath( "references" )

	# 1.) Detect changed papers by FILE SIGNATURE ( mtime ) without reading
	#     them. We only need each LIBRARY paper's identity ( doi / title / wid )
	#     -- to know what you already have ( dedup ) -- and its outbound /
	#     inbound citation WIDs. We do NOT build a full-text haystack for the
	#     library : the dashboard is for the EXTERNAL papers you don't have
	#     ( your own papers live in Zotero ). Only new / changed papers are
	#     read ; unchanged ones are skipped with a stat().
	prog( "Scanning library…" )
	paper_files = sorted( papers_db.papers_dir( args ).glob( "*.json" ) )
	n_lib = len( paper_files )
	present , n_reproc = set() , 0
	changed = bool( full ) or not paper_state or "ref_dup" not in state
	for i , path in enumerate( tqdm( paper_files , desc="Library" ) ):
		if i % 400 == 0:
			prog( f"Scanning library… {i}/{n_lib}" )
		fn = path.name
		present.add( fn )

		ps = paper_state.get( fn )
		if ps and not full:
			doi   = ps.get( "doi" )
			oa_fp = oa_dir.joinpath( f"{utils.base64_encode( doi )}.json" ) if doi else None
			sig   = path.stat().st_mtime \
			      + ( oa_fp.stat().st_mtime if ( oa_fp and oa_fp.exists() ) else 0.0 )
			if abs( ps.get( "sig" , -1 ) - sig ) < 1e-6:
				continue   # unchanged -> not read

		# New / changed : read just this one paper's identity + citation wids.
		changed = True
		n_reproc += 1
		try:
			paper = utils.read_json( path )
		except Exception:
			continue
		key = papers_db.record_key( paper )
		if not key:
			continue
		doi   = utils.normalize_doi( paper.get( "doi" ) )
		oa_fp = oa_dir.joinpath( f"{utils.base64_encode( doi )}.json" ) if doi else None
		sig   = path.stat().st_mtime \
		      + ( oa_fp.stat().st_mtime if ( oa_fp and oa_fp.exists() ) else 0.0 )

		oa = {}
		if oa_fp and oa_fp.exists():
			try: oa = utils.read_json( oa_fp ) or {}
			except Exception: oa = {}

		wid      = _wid( oa.get( "id" ) or "" ) or ( paper.get( "openalex_id" ) or "" )
		ref_wids = [ _wid( r ) for r in ( oa.get( "referenced_works" ) or [] ) ]
		cb_wids  = []
		for cw in oa.get( "cited_by_works" ) or []:
			cwid = _wid( cw.get( "id" ) or "" )
			if not cwid:
				continue
			cb_wids.append( cwid )
			if cwid not in cb_meta:
				cb_meta[ cwid ] = _work_entry( cw )

		paper_state[ fn ] = {
			"sig":      sig ,
			"key":      key ,
			"wid":      wid ,
			"doi":      doi ,
			"title":    paper.get( "title" ) or "" ,
			"ref_wids": ref_wids ,
			"cb_wids":  cb_wids ,
		}

		# Searchable entry for the paper ITSELF ( the "In Library" tab ). Built
		# from the library record + its OpenAlex meta when present. The haystack
		# is SPECIAL for the library : title + abstract + the FULL-TEXT OCR body
		# ( prma ocr ) , so you can full-text-search papers you have. This is the
		# ONLY place the library gets a haystack -- the external pools never
		# include these ( they're filtered out by lib_wids + dedup ).
		lib_e     = utils.openalex_entry_scalars( oa ) if oa else {}
		lib_title = paper.get( "title" ) or lib_e.get( "title" ) or "(untitled)"
		abstract  = _reconstruct_abstract( oa.get( "abstract_inverted_index" ) ) if oa else ""
		ocr_body  = _ocr_fulltext( paper )
		prefix    = utils.doi_to_filename( key )
		montage_fp= Path( args.output ).joinpath( "images" , f"{prefix}-Figures.png" )
		md_fp     = Path( args.output ).joinpath( "md" , f"{prefix}.md" )
		lib_meta[ key ] = {
			"key":        key ,
			"wid":        wid ,
			"title":      lib_title ,
			"doi":        doi or lib_e.get( "doi" ) ,
			"year":       lib_e.get( "year" ) ,
			"pubdate":    lib_e.get( "pubdate" ) or "" ,
			"cited_by":   lib_e.get( "cited_by" ) ,
			"authors":    lib_e.get( "authors" ) or [] ,
			"pdf":        paper.get( "pdf_path" ) or "" , # LOCAL path -> served via /pdf?key=
			"montage":    f"/images/{prefix}-Figures.png" if montage_fp.exists() else "" ,
			"has_md":     md_fp.exists() ,                # -> "Read" accordion available
			"created_at": paper.get( "created_at" ) or "" , # when first added to the library
			"hay":        ( lib_title + " " + abstract + " " + ocr_body ).lower() ,
		}

	# Drop papers whose file is gone.
	for fn in list( paper_state.keys() ):
		if fn not in present:
			gone = paper_state.pop( fn )
			lib_meta.pop( ( gone or {} ).get( "key" ) , None )
			changed = True

	if not changed:
		# Nothing on disk moved -> reuse the persisted index verbatim ( no
		# dedup recompute , no 168 MB rewrite ). Just materialize pools.
		pools = _pools( state , getattr( args , "top_author_count" , 100 ) )
		log( f"dashboard :: no library changes -- index current ( {time.time()-t0:.1f}s )" )
		return pools

	# 2.) Library dedup index ( exact DOI + normalized title ) + library WIDs.
	prog( "Indexing library identity…" )
	lib_dois = set( ps[ "doi" ] for ps in paper_state.values() if ps.get( "doi" ) )
	lib_dois.discard( None )
	lib_titles = [ utils.normalize_title( ps[ "title" ] )
		for ps in paper_state.values() if ps.get( "title" ) ]
	lib_titles = [ t for t in lib_titles if t ]
	lib_index = {
		"dois":        lib_dois ,
		"titles_set":  set( lib_titles ) ,
		"titles_list": lib_titles ,
	}
	lib_wids = set( ps[ "wid" ] for ps in paper_state.values() if ps.get( "wid" ) )

	# 3.) Reference tally ( how many library papers cite each non-library work ).
	ref_tally = Counter()
	for ps in paper_state.values():
		for w in ps[ "ref_wids" ]:
			if w and w not in lib_wids:
				ref_tally[ w ] += 1

	# 4.) Stream reference JSONs we don't have an entry for yet ( new refs only ).
	need = [ w for w in ref_tally if w not in ref_meta ]
	prog( f"Reading {len( need )} new reference file(s)…" )
	for i , w in enumerate( tqdm( need , desc="New references" ) ):
		if i % 1000 == 0:
			prog( f"Reading new reference files… {i}/{len( need )}" )
		fp = refs_dir.joinpath( f"{w}.json" )
		meta = {}
		if fp.exists():
			try: meta = utils.read_json( fp ) or {}
			except Exception: meta = {}
		ref_meta[ w ] = _work_entry( meta )
	for w in list( ref_meta.keys() ):       # prune refs nothing cites anymore
		if w not in ref_tally:
			ref_meta.pop( w , None )

	# 5.) Cited-by tally ( metadata already gathered inline above ).
	cb_tally = Counter()
	for ps in paper_state.values():
		for w in ps[ "cb_wids" ]:
			if w and w not in lib_wids:
				cb_tally[ w ] += 1
	for w in list( cb_meta.keys() ):
		if w not in cb_tally:
			cb_meta.pop( w , None )

	# 6.) Exact dedup for both pools ( fast ).
	prog( "Deduplicating against library…" )
	ref_dup = _compute_dedup( ref_tally , ref_meta , lib_index )
	cb_dup  = _compute_dedup( cb_tally  , cb_meta  , lib_index )

	# 7.) Persist updated state.
	prog( "Saving index…" )
	state[ "built_at" ] = time.strftime( "%Y-%m-%d %H:%M:%S" )
	state[ "ref_dup" ]  = ref_dup
	state[ "cb_dup" ]   = cb_dup
	save_state( args , state )

	pools = _pools( state , getattr( args , "top_author_count" , 100 ) )
	log(
		f"dashboard :: indexed in {time.time()-t0:.1f}s -- "
		f"reprocessed {n_reproc} paper(s) , streamed {len( need )} new ref(s) ; "
		f"have={pools[ 'lib_count' ]} missing-refs={len( pools[ 'references' ] )} "
		f"cited-by={len( pools[ 'cited_by' ] )} authors={len( pools[ 'authors' ] )}"
	)
	return pools


# ---------------------------------------------------------------------------
# Derive servable pools from persisted state
# ---------------------------------------------------------------------------

def _pool_rows( tally , meta , dup , lib_wids , pool_name ):
	rows = []
	for wid , n in tally.items():
		if wid in lib_wids or dup.get( wid ):
			continue
		e = meta.get( wid )
		if not e:
			continue
		# Skip works whose OpenAlex JSON came back empty ( failed / tombstoned
		# fetch ) : no title , no abstract , nothing to search or show. They'd
		# otherwise headline the default Conn. sort as "(no metadata)".
		# ( _make_haystack yields a lone space for empty meta , so strip. )
		if not ( e.get( "hay" ) or "" ).strip() and not e.get( "doi" ):
			continue
		rows.append( {
			"pool":      pool_name ,
			"key":       wid ,
			"wid":       wid ,
			"title":     e.get( "title" ) or "(no metadata)" ,
			"doi":       e.get( "doi" ) ,
			"year":      e.get( "year" ) ,
			"journal":   "" ,
			"cited_by":  e.get( "cited_by" ) ,
			"lib_cites": n ,
			"pdf":       e.get( "pdf" ) or "" ,
			"pdf_local": False ,
			"pubdate":   e.get( "pubdate" ) or "" ,
			"authors":   e.get( "authors" ) or [] ,
			"hay":       e.get( "hay" ) or "" ,
		} )
	return rows


def _pools( state , top_n=100 ):
	"""Materialize the servable EXTERNAL pools ( papers you don't have ) from
	state : references ( works your library cites , missing ) and cited_by
	( works that cite your library , missing ). Cheap : a few dict
	constructions per work. lib_count is just how many papers you HAVE
	( shown for context ) -- the library itself isn't searched here."""
	paper_state = state.get( "paper_state" ) or {}
	lib_wids = set( ps[ "wid" ] for ps in paper_state.values() if ps.get( "wid" ) )

	ref_tally = Counter()
	cb_tally  = Counter()
	for ps in paper_state.values():
		for w in ps.get( "ref_wids" ) or []:
			if w and w not in lib_wids:
				ref_tally[ w ] += 1
		for w in ps.get( "cb_wids" ) or []:
			if w and w not in lib_wids:
				cb_tally[ w ] += 1

	references = _pool_rows( ref_tally , state.get( "ref_meta" ) or {} ,
		state.get( "ref_dup" ) or {} , lib_wids , "references" )
	cited_by   = _pool_rows( cb_tally , state.get( "cb_meta" ) or {} ,
		state.get( "cb_dup" ) or {} , lib_wids , "cited_by" )
	authors    = dash_index.build_authors( references , top_n )
	library    = _lib_rows( state.get( "lib_meta" ) or {} )

	return {
		"built_at":      state.get( "built_at" ) ,
		"lib_count":     len( paper_state ) ,
		"missing_count": len( references ) ,
		"references":    references ,
		"cited_by":      cited_by ,
		"authors":       authors ,
		"library":       library ,
	}


def _lib_rows( lib_meta ):
	"""Servable rows for the 'In Library' tab : the papers you HAVE , as their
	own searchable pool. Kept entirely separate from the external pools."""
	rows = []
	for key , e in ( lib_meta or {} ).items():
		rows.append( {
			"pool":      "library" ,
			"key":       key ,
			"wid":       e.get( "wid" ) or "" ,
			"title":     e.get( "title" ) or "(untitled)" ,
			"doi":       e.get( "doi" ) ,
			"year":      e.get( "year" ) ,
			"journal":   "" ,
			"cited_by":  e.get( "cited_by" ) ,
			"lib_cites": None ,                 # N/A for your own papers
			"pdf":        e.get( "pdf" ) or "" ,
			"pdf_local":  True ,                 # local file -> open via /pdf?key=
			"montage":    e.get( "montage" ) or "" ,   # prma images grid -> /images/...
			"has_md":     bool( e.get( "has_md" ) ) ,  # prma md -> "Read" accordion
			"pubdate":    e.get( "pubdate" ) or "" ,
			"created_at": e.get( "created_at" ) or "" ,  # In-Library "Added" column
			"authors":    e.get( "authors" ) or [] ,
			"hay":        e.get( "hay" ) or "" ,
		} )
	return rows


def load_pools( args , top_n=100 ):
	"""Load persisted pools , or None if no index has been built yet."""
	state = load_state( args )
	if not state:
		return None
	return _pools( state , top_n )


# ---------------------------------------------------------------------------
# One-stop reindex : refresh the OpenAlex cache , then (re)build the index
# ---------------------------------------------------------------------------

def reindex( args , full=False , log=print , progress=None ):
	"""Refresh the OpenAlex cache ( snapshot + meta / references / cited-by
	fetch for any NEW papers -- incremental , download only ) , then build the
	index from it. Honors --skip-snapshot to index straight off whatever is
	already cached.

	Note : the cache refresh only DOWNLOADS files ( tasks.main = snapshot +
	OpenAlex.update_cache ). It never runs the missing.xlsx computation."""
	if not getattr( args , "skip_snapshot" , False ):
		if progress: progress( "Refreshing OpenAlex cache ( download )…" )
		from ..tasks import main as main_task
		log( "dashboard :: refreshing OpenAlex cache ( snapshot + fetch new ) ..." )
		main_task.main( args )
	else:
		log( "dashboard :: --skip-snapshot ; indexing off the existing cache" )
	return build( args , full=full , log=log , progress=progress )
