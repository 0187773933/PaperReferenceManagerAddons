#!/usr/bin/env python3
"""
Keyword spider crawl over the OpenAlex graph.

The crawler is fundamentally an OpenAlex client. The local caches
( cache/openalex/*.json , cache/openalex/references/*.json ) are consulted
only to ( a ) avoid re-fetching papers we already have on disk and ( b ) mark
"novel = not seen anywhere yet" for the report.

Forward / backward passes as search wedges
------------------------------------------
Given a paper that matches our keywords , its two neighborhoods both wedge
into the topical region we care about , from opposite sides:

  - Backward wedge :: referenced_works   -- older , foundational papers
  - Forward wedge  :: cited_by_works     -- newer , downstream papers

A paper reached from multiple seed neighborhoods is much more likely to be
on-topic than any single hit -- so we accumulate a connectivity signal across
the walk and roll it into the score.

Scoring
-------
    score( wid ) = α * keyword_hits
                 + β * log10( cites + 1 )
                 + γ * incoming_links_from_visited

Best-first traversal pops the highest-scored frontier node , fetches its full
OpenAlex record , and expands both wedges. The walk is bounded by
max_visits , max_depth , and api_budget.
"""

import math
import heapq
from collections import defaultdict
from tqdm import tqdm

from ..openalex.api import OpenAlexAPI
from ..openalex.stats import make_haystack
from ..utils import utils


def _wid_of( item ):
	if isinstance( item , str ):
		return item.rsplit( "/" , 1 )[ -1 ] or None
	if isinstance( item , dict ):
		oid = item.get( "id" ) or ""
		return oid.rsplit( "/" , 1 )[ -1 ] if oid else None
	return None


class Crawler():
	def __init__( self , args ):
		self.args = args
		self.API = OpenAlexAPI( args )
		self.cache_dir    = self.args.output.joinpath( "cache" , "crawler" )
		self.cache_dir.mkdir( parents=True , exist_ok=True )
		self.oa_cache_dir = self.args.output.joinpath( "cache" , "openalex" )
		self.oa_refs_dir  = self.oa_cache_dir.joinpath( "references" )
		self.xlsx_path    = self.args.output.joinpath( "crawl.xlsx" )

	# ------------------------------------------------------------------
	# Cache lookups
	# ------------------------------------------------------------------

	def _cached_path( self , wid ):
		for fp in (
			self.cache_dir.joinpath( f"{wid}.json" ) ,
			self.oa_cache_dir.joinpath( f"{wid}.json" ) ,
			self.oa_refs_dir.joinpath( f"{wid}.json" ) ,
		):
			if fp.exists():
				return fp
		return None

	def _read_cached( self , wid ):
		fp = self._cached_path( wid )
		if not fp:
			return None
		try:
			return utils.read_json( fp )
		except Exception as e:
			print( f"\nCrawler :: bad cache {fp}: {e}" )
			return None

	def _store( self , meta ):
		wid = _wid_of( meta )
		if not wid:
			return
		fp = self.cache_dir.joinpath( f"{wid}.json" )
		if fp.exists():
			return
		# Don't duplicate what's already on disk in another oa cache
		if self._cached_path( wid ):
			return
		utils.write_json( fp , meta )

	# ------------------------------------------------------------------
	# Fetch helpers ( cache-first , then API )
	# ------------------------------------------------------------------

	def _get_one( self , wid , do_fetch , api_budget ):
		"""Return ( meta , fetched ). fetched=True iff we hit the API."""
		meta = self._read_cached( wid )
		if meta is not None:
			return meta , False
		if not wid or not wid.startswith( "W" ):
			return None , False
		if not do_fetch or api_budget[ 0 ] <= 0:
			return None , False
		api_budget[ 0 ] -= 1
		data = self.API.get_id( wid )
		if data:
			self._store( data )
		return data , True

	def _get_many( self , wids , do_fetch , api_budget ):
		"""Yield ( wid , meta , fetched ) for each requested wid.
		Cache hits are emitted immediately ; misses are batch-fetched."""
		missing = []
		for wid in wids:
			if not wid:
				continue
			meta = self._read_cached( wid )
			if meta is not None:
				yield wid , meta , False
			else:
				missing.append( wid )
		if not missing or not do_fetch or api_budget[ 0 ] <= 0:
			return
		# Batch fetch , consuming api_budget by chunks
		BATCH = 50
		for i in range( 0 , len( missing ) , BATCH ):
			if api_budget[ 0 ] <= 0:
				return
			chunk = missing[ i : i + BATCH ]
			api_budget[ 0 ] -= 1
			got = {}
			for w in self.API.batch_get_ids( chunk , per_page=BATCH ):
				wid = _wid_of( w )
				if wid:
					got[ wid ] = w
					self._store( w )
			for wid in chunk:
				yield wid , got.get( wid ) , True

	def _get_cited_by( self , wid , do_fetch , api_budget ):
		"""Forward wedge. Returns a list of full work dicts ( may be empty ).
		Reuses cached metadata when the seed paper already stashed cited_by_works."""
		seed = self._read_cached( wid )
		if seed and seed.get( "cited_by_works" ):
			return seed[ "cited_by_works" ] or []
		if not wid or not wid.startswith( "W" ):
			return []
		if not do_fetch or api_budget[ 0 ] <= 0:
			return []
		api_budget[ 0 ] -= 1
		results = self.API.get_cited_by( wid ) or []
		# Cache them so subsequent visits don't re-hit
		for w in results:
			self._store( w )
		return results

	def _iter_library_metas( self ):
		for fp in self.oa_cache_dir.glob( "*.json" ):
			try:
				d = utils.read_json( fp ) or {}
			except Exception:
				continue
			wid = _wid_of( d ) or fp.stem
			yield wid , d

	# ------------------------------------------------------------------
	# Scoring
	# ------------------------------------------------------------------

	def _score( self , meta , predicates , in_links , cite_weight , link_weight ):
		hay = make_haystack( meta )
		hits = sum( 1 for p in predicates if p( hay ) )
		cites = meta.get( "cited_by_count" ) or 0
		return (
			hits
			+ cite_weight * math.log10( cites + 1 )
			+ link_weight * in_links
		) , hits

	# ------------------------------------------------------------------
	# The crawl
	# ------------------------------------------------------------------

	def crawl(
		self ,
		searches ,
		snapshot=None ,
		out_name=None ,
		max_visits=500 ,
		max_depth=2 ,
		min_seed_hits=1 ,
		min_novel_hits=1 ,
		do_fetch=True ,
		api_budget=1000 ,
		cite_weight=0.4 ,
		link_weight=0.5 ,
		expand_cited_by=True ,
	):
		if not searches:
			print( "Crawler :: no searches provided" )
			return
		if out_name:
			self.xlsx_path = self.args.output.joinpath( out_name )
		names = [ n for n , _ in searches ]
		predicates = [ p for _ , p in searches ]
		print( f"Crawler :: predicates={names}" )
		print( f"Crawler :: max_visits={max_visits} max_depth={max_depth} "
		       f"do_fetch={do_fetch} api_budget={api_budget} "
		       f"cite_weight={cite_weight} link_weight={link_weight}" )

		# Inventory what we already have on disk.
		# lib_wids = the library ; used as a fast-path skip for novel detection.
		# refs/crawler caches are for avoiding re-fetch , NOT for suppressing
		# reports -- a paper sitting in those caches may still be a valid
		# predicate-matched candidate the user hasn't reviewed yet.
		lib_wids = { fp.stem for fp in self.oa_cache_dir.glob( "*.json" ) }
		ref_wids = { fp.stem for fp in self.oa_refs_dir.glob( "*.json" ) }
		crawler_wids = { fp.stem for fp in self.cache_dir.glob( "*.json" ) }
		print( f"Crawler :: caches -- library={len(lib_wids)} "
		       f"refs={len(ref_wids)} crawler={len(crawler_wids)} "
		       f"( only library is used for novel-exclusion )" )

		# DOI/title dedup against the live library snapshot ( catches papers
		# that are in the library but missing from the OpenAlex cache , and
		# papers reached under a different work-id ).
		lib_index = utils.build_library_dedup_index( snapshot ) if snapshot else None
		print( f"Crawler :: lib_index dois={len(lib_index['dois']) if lib_index else 0} "
		       f"titles={len(lib_index['titles_set']) if lib_index else 0}" )

		# Seed from library papers matching the predicates
		seeds = []
		for wid , meta in tqdm( list( self._iter_library_metas() ) , desc="Scanning library" ):
			_ , hits = self._score( meta , predicates , 0 , cite_weight , link_weight )
			if hits >= min_seed_hits:
				seeds.append( ( wid , meta ) )
		print( f"Crawler :: seeded {len(seeds)} library papers" )
		if not seeds:
			print( "Crawler :: no seed papers matched predicates -- nothing to do" )
			return

		# Best-first crawl
		frontier   = []                   # heap : ( -score , counter , wid , depth )
		best_score = {}                   # wid -> best score seen so far
		in_links   = defaultdict( int )   # wid -> incoming-link count from visited frontier
		depths     = {}                   # wid -> shallowest depth reached
		metas      = {}                   # wid -> meta we've resolved
		visited    = set()
		novel      = {}                   # wid -> ( meta , score , hits , depth )
		counter    = 0
		budget     = [ api_budget ]

		def _push( wid , meta , depth ):
			nonlocal counter
			s , hits = self._score(
				meta , predicates , in_links[ wid ] , cite_weight , link_weight
			)
			if ( wid not in best_score ) or ( s > best_score[ wid ] ):
				best_score[ wid ] = s
				depths[ wid ] = min( depth , depths.get( wid , depth ) )
				heapq.heappush( frontier , ( -s , counter , wid , depth ) )
				counter += 1
			if hits < min_novel_hits or wid in lib_wids:
				return
			if lib_index and utils.is_library_dup( meta , lib_index , fuzzy=False ):
				return
			prev = novel.get( wid )
			if ( prev is None ) or ( s > prev[ 1 ] ):
				novel[ wid ] = ( meta , s , hits , depth )

		for wid , meta in seeds:
			metas[ wid ] = meta
			_push( wid , meta , 0 )

		pbar = tqdm( total=max_visits , desc="Crawling" )
		fetched = 0
		while frontier and len( visited ) < max_visits:
			neg_score , _ , wid , depth = heapq.heappop( frontier )
			if wid in visited:
				continue
			visited.add( wid )
			pbar.update( 1 )
			pbar.set_postfix(
				depth=depth ,
				novel=len( novel ) ,
				api=api_budget - budget[ 0 ] ,
			)

			meta = metas.get( wid ) or self._read_cached( wid )
			if meta is None:
				meta , did = self._get_one( wid , do_fetch , budget )
				if did:
					fetched += 1
			if not meta:
				continue
			metas[ wid ] = meta

			if depth >= max_depth:
				continue

			# Backward wedge -- references , batch-fetched
			ref_wids = [ _wid_of( r ) for r in ( meta.get( "referenced_works" ) or [] ) ]
			ref_wids = [ r for r in ref_wids if r and r not in visited ]
			for cwid , cmeta , did in self._get_many( ref_wids , do_fetch , budget ):
				if did:
					fetched += 1
				if not cmeta:
					continue
				metas[ cwid ] = cmeta
				in_links[ cwid ] += 1
				_push( cwid , cmeta , depth + 1 )

			# Forward wedge -- cited-by , single API call returning full works
			if expand_cited_by:
				for cw in self._get_cited_by( wid , do_fetch , budget ):
					cwid = _wid_of( cw )
					if not cwid or cwid in visited:
						continue
					metas[ cwid ] = cw
					in_links[ cwid ] += 1
					_push( cwid , cw , depth + 1 )
		pbar.close()

		# Reports
		ranked_novel = sorted( novel.items() , key=lambda kv: kv[ 1 ][ 1 ] , reverse=True )
		novel_rows = []
		for wid , ( meta , score , hits , depth ) in ranked_novel:
			base = utils.openalex_to_xlsx_row( wid , meta , meta.get( "cited_by_count" ) or 0 )
			novel_rows.append([
				round( score , 3 ) , hits , in_links[ wid ] , depth ,
			] + base )

		visited_rows = []
		for wid in visited:
			meta = metas.get( wid ) or self._read_cached( wid ) or {}
			score = best_score.get( wid , 0.0 )
			_ , hits = self._score(
				meta , predicates , in_links[ wid ] , cite_weight , link_weight
			) if meta else ( 0.0 , 0 )
			base = utils.openalex_to_xlsx_row( wid , meta , meta.get( "cited_by_count" ) or 0 )
			visited_rows.append([
				round( score , 3 ) , hits , in_links[ wid ] , depths.get( wid , 0 ) ,
			] + base )
		visited_rows.sort( key=lambda r: r[ 0 ] , reverse=True )

		HEADERS = [ "Score" , "Hits" , "InLinks" , "Depth" , "Cites" , "Title" , "Year" ,
		            "Proxy" , "DOI" , "Link" , "PDF" , "OA Cited-By" , "WID" ]
		sheets = [
			( "Novel Discoveries" , HEADERS , novel_rows[ :2000 ] ) ,
			( "Visited"           , HEADERS , visited_rows[ :2000 ] ) ,
		]
		utils.write_xlsx( self.xlsx_path , sheets )
		print(
			f"Crawler :: visited={len(visited)} novel={len(novel)} "
			f"api_fetches={fetched} ( budget_left={budget[ 0 ]} ) -> {self.xlsx_path}"
		)
