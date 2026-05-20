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
					print( f"\nOpenAlex {r.status_code}: {r.url} {r.text[:200]}" )
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
		return self._request( self.base_url + open_alex_wid )

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
