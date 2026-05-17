import time
import requests
from pathlib import Path
from tqdm import tqdm

from .api import OpenAlexAPI
from .stats import OpenAlexStats
from .search import OpenAlexSearch

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

	def update_cache( self , snapshot ):
		snapshot_keys = snapshot.keys()
		for k , key in enumerate( tqdm( snapshot_keys , desc="Papers" , position=0 ) ):

			# 1.) Download Info for Papers in Snapshot
			paper_doi = snapshot[ key ].get( "doi" )
			paper_title = snapshot[ key ].get( "title" )
			paper_title_normalized = utils.openalex_normalize_title( paper_title )
			_id = snapshot[ key ].get( "id" )
			_cached_fp = self.storage_dir.joinpath( f"{_id}.json" )
			if not paper_doi:
				if _cached_fp.exists():
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
				continue
			paper_doi_normalized = utils.normalize_doi( paper_doi )
			if not paper_doi_normalized:
				print( f"\nCould not normalize DOI: {paper_doi!r} ({paper_title})" )
				continue
			paper_doi_b64 = utils.base64_encode( paper_doi_normalized )
			paper_cached_fp = self.storage_dir.joinpath( f"{paper_doi_b64}.json" )
			if paper_cached_fp.exists():
				continue
			paper_data = self.API.get_doi( paper_doi_normalized )
			if not paper_data:
				print( f"\nNo OpenAlex data for DOI: {paper_doi_normalized}" )
				continue
			utils.write_json( paper_cached_fp , paper_data )

			# 2.) Download all of its References
			# referenced_works = paper_data.get( "referenced_works" )
			# if not referenced_works:
			# 	continue
			# for i , item in enumerate( tqdm( referenced_works , desc="References" , position=1 , leave=False ) ):
			# 	wid = item.split( "/" )[ -1 ]
			# 	reference_cached_fp = self.references_dir.joinpath( f"{wid}.json" )
			# 	if reference_cached_fp.exists():
			# 		continue
			# 	reference_data = self.API.get_id( wid )
			# 	if not reference_data:
			# 		reference_data = {}
			# 	utils.write_json( reference_cached_fp , reference_data )

	def search( self , oa_index , searches ):
		return self.Search.add_search_sheets( oa_index , searches )