import time
import requests
from pathlib import Path
from tqdm import tqdm

from .api import OpenAlexAPI
from .stats import OpenAlexStats
from .search import OpenAlexSearch

from ..db    import papers as papers_db
from ..utils import utils

class OpenAlex():
	def __init__( self , args ):
		self.args = args
		self.API = OpenAlexAPI( args )
		self.Stats = OpenAlexStats( args )
		self.Search = OpenAlexSearch( args )
		self.storage_dir = self.args.output.joinpath( "cache" , "openalex" )
		self.storage_dir.mkdir( parents=True , exist_ok=True )
		self.references_dir = self.storage_dir.joinpath( "references" )
		self.references_dir.mkdir( parents=True , exist_ok=True )
		self.problem_dois = set()

	def _persist_paper_wid( self , doi , wid ):
		"""Pin the OpenAlex work id ( e.g. 'W2911234567' ) onto the
		unified db/papers/{doi_to_filename}.json record at key
		'openalex_id'. No-op when either input is missing or when the
		record already has that wid -- so a cold cache that gets hit
		( e.g. a re-run of ` prma ` ) gets the wid backfilled , while
		steady-state runs don't churn paper files."""
		if not ( doi and wid ):
			return False
		paper = papers_db.load( self.args , doi )
		if paper is None:
			return False
		if paper.get( "openalex_id" ) == wid:
			return False
		paper[ "openalex_id" ] = wid
		try:
			papers_db.save( self.args , paper )
		except Exception as e:
			print( f"\nOpenAlex :: could not persist wid for {doi} ( {e} )" )
			return False
		return True

	def update_cache( self , snapshot ):
		"""Walk the snapshot and ensure for every paper :
		  - an OpenAlex meta JSON is cached at
		    output/cache/openalex/{base64( normalized_doi )}.json ,
		    populated with referenced_works + cited_by_works ;
		  - every referenced-work meta is cached at
		    output/cache/openalex/references/{wid}.json ;
		  - the unified db/papers/{doi_to_filename}.json record carries a
		    top-level 'openalex_id' field ( the WID , e.g. 'W2911234567' ).

		Idempotent. Cache hits short-circuit the per-paper API calls but
		STILL backfill openalex_id onto db/papers/ , so libraries whose
		cache predates the wid-persistence change pick the field up on
		the next ` prma ` run without needing a force-refresh."""
		snapshot_keys = snapshot.keys()
		n_fetched = 0
		n_cache_hit = 0
		n_wid_persisted = 0
		n_missing = 0
		for k , key in enumerate( tqdm( snapshot_keys , desc="Papers" , position=0 ) ):

			# 1.) Download Info for Papers in Snapshot
			paper_doi = snapshot[ key ].get( "doi" )
			paper_title = snapshot[ key ].get( "title" )
			paper_title_normalized = utils.openalex_normalize_title( paper_title )
			_id = snapshot[ key ].get( "id" )
			_cached_fp = self.storage_dir.joinpath( f"{_id}.json" )
			if not paper_doi:
				if _cached_fp.exists():
					n_cache_hit += 1
					continue
				else:
					print( "searching title" , paper_title_normalized )
					search_results = self.API.search_title( paper_title_normalized )
					paper_dois = [ p.get( "doi" ) for p in search_results if p.get( "doi" ) is not None ]
					if paper_dois:
						paper_doi = paper_dois[ 0 ] # Todo
					else:
						print( "still nothing" , snapshot[ key ] , search_results )
					utils.write_json( _cached_fp , { "id": _id , "doi": paper_doi , "title": paper_title } )
			if not paper_doi:
				n_missing += 1
				continue
			paper_doi_normalized = utils.normalize_doi( paper_doi )
			if not paper_doi_normalized:
				print( f"\nCould not normalize DOI: {paper_doi!r} ({paper_title})" )
				n_missing += 1
				continue
			paper_doi_b64 = utils.base64_encode( paper_doi_normalized )
			paper_cached_fp = self.storage_dir.joinpath( f"{paper_doi_b64}.json" )

			# Cache HIT path : just read the cached meta so we can still
			# backfill openalex_id onto db/papers/ , then skip the
			# expensive per-reference download ( those were already
			# cached when this paper was first fetched ).
			if paper_cached_fp.exists():
				try:
					paper_data = utils.read_json( paper_cached_fp ) or {}
				except Exception as e:
					print( f"\nOpenAlex :: cached file {paper_cached_fp} unreadable ( {e} )" )
					paper_data = {}
				oa_wid = ( paper_data.get( "id" ) or "" ).rsplit( "/" , 1 )[ -1 ]
				if self._persist_paper_wid( paper_doi_normalized , oa_wid ):
					n_wid_persisted += 1
				n_cache_hit += 1
				continue

			# Cache MISS path : hit the API.
			paper_data = self.API.get_doi( paper_doi_normalized )
			if not paper_data:
				print( f"\nNo OpenAlex data for DOI: {paper_doi_normalized}" )
				n_missing += 1
				continue

			# 2.) Fetch all of its Cited-By Papers
			oa_wid = ( paper_data.get( "id" ) or "" ).rsplit( "/" , 1 )[ -1 ]
			if oa_wid and ( paper_data.get( "cited_by_count" ) or 0 ) > 0:
				paper_data[ "cited_by_works" ] = self.API.get_cited_by( oa_wid )
			else:
				paper_data[ "cited_by_works" ] = []
			utils.write_json( paper_cached_fp , paper_data )
			n_fetched += 1

			# Pin the wid onto the db/papers/ record now that we have it.
			if self._persist_paper_wid( paper_doi_normalized , oa_wid ):
				n_wid_persisted += 1

			# 3.) Download all of its References
			referenced_works = paper_data.get( "referenced_works" )
			if not referenced_works:
				continue
			for i , item in enumerate( tqdm( referenced_works , desc="References" , position=1 , leave=False ) ):
				wid = item.split( "/" )[ -1 ]
				reference_cached_fp = self.references_dir.joinpath( f"{wid}.json" )
				if reference_cached_fp.exists():
					continue
				reference_data = self.API.get_id( wid )
				if not reference_data:
					reference_data = {}
				utils.write_json( reference_cached_fp , reference_data )

		print(
			f"OpenAlex :: update_cache done -- "
			f"fetched={n_fetched} cache-hits={n_cache_hit} "
			f"wid-persisted={n_wid_persisted} missing-doi-or-data={n_missing}"
		)

	def search( self , oa_index , searches ):
		return self.Search.add_search_sheets( oa_index , searches )