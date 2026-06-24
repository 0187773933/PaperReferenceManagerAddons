import time
import requests
from pathlib import Path
from tqdm import tqdm

from ..utils import utils

class OpenAlexAPI():
	def __init__( self , args ):
		self.args = args
		self.config = utils.read_yaml( self.args.config.joinpath( "config.yaml" ) )

		openalex = self.config.get( "openalex" ) or {}
		self.api_key  = openalex.get( "api_key" )
		self.base_url = "https://api.openalex.org/works"
		self.headers  = openalex.get( "headers" ) or {}
		self.max_retries = openalex.get( "max_retries" , 5 )

		self.params = { "api_key": self.api_key }

		self.storage_dir = self.args.output.joinpath( "cache" , "openalex" )
		self.storage_dir.mkdir( parents=True , exist_ok=True )
		self.references_dir = self.storage_dir.joinpath( "references" )
		self.references_dir.mkdir( parents=True , exist_ok=True )
		self.problem_dois = set()

	def _request( self , url , params=None ):
		merged = { **self.params , **( params or {} ) }
		for attempt in range( self.max_retries ):
			try:
				r = requests.get( url , params=merged , headers=self.headers , timeout=30 )
				if r.status_code == 200:
					return r.json()
				elif r.status_code == 429:
					retry = int( r.headers.get( "Retry-After" , 5 ) )
					print( f"\nRate limited. Sleeping {retry}s" )
					time.sleep( retry )
					continue
				else:
					# 404 is the EXPECTED "this work no longer exists" signal :
					# OpenAlex continuously merges duplicate works and deletes the
					# loser's id , so other papers' referenced_works / cited_by
					# lists routinely point at ids that now 404. Every caller
					# already treats None as "no data" and records it in the
					# problems log , so don't spam the console with the HTML body.
					# Other ( genuinely unexpected ) statuses still get a concise
					# one-liner -- without the noisy response body.
					if r.status_code != 404:
						print( f"\nOpenAlex {r.status_code}: {r.url}" )
					return None
			except requests.exceptions.RequestException as e:
				wait = min( 2 ** attempt , 60 )
				print( f"\nNetwork error ({e}). Retry {attempt+1}/{self.max_retries}. Sleeping {wait}s" )
				time.sleep( wait )
		print( f"\nFailed after {self.max_retries} retries: {url}" )
		return None

	def get_doi( self , doi ):
		if not doi:
			return None
		return self._request( f"{self.base_url}/https://doi.org/{doi}" )

	def get_id( self , open_alex_wid ):
		return self._request( f"{self.base_url}/{open_alex_wid}" )

	def search_title( self , title , per_page=10 ):
		data = self._request( self.base_url , {
			"search": f'"{title}"' ,
			"per-page": per_page ,
			"select": "id,doi,title,display_name,publication_year,cited_by_count,relevance_score,authorships" ,
		})
		return ( data or {} ).get( "results" , [] )

	def get_cited_by( self , wid , per_page=200 ):
		data = self._request( self.base_url , {
			"filter": f"cites:{wid}" ,
			"per-page": per_page ,
			"sort": "cited_by_count:desc" ,
		})
		return ( data or {} ).get( "results" , [] )

	def batch_get_ids( self , wids , per_page=50 ):
		"""Fetch many works in one request via filter=ids.openalex:W1|W2|...
		Yields work dicts. Caller batches; we cap per-request size to keep URLs sane."""
		clean = [ w for w in ( wids or [] ) if w and w.startswith( "W" ) ]
		if not clean:
			return
		per_page = max( 1 , min( per_page , 50 ) )
		for i in range( 0 , len( clean ) , per_page ):
			chunk = clean[ i : i + per_page ]
			data = self._request( self.base_url , {
				"filter": f"ids.openalex:{ '|'.join( chunk ) }" ,
				"per-page": per_page ,
			})
			for w in ( data or {} ).get( "results" , [] ) or []:
				yield w
