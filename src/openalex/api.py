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
		self.base_url = "https://api.openalex.org/works/"
		self.headers  = openalex.get( "headers" ) or {}
		self.max_retries = openalex.get( "max_retries" , 5 )

		self.params = { "api_key": self.api_key }

		self.storage_dir = self.args.output.joinpath( "cache" , "openalex" )
		self.storage_dir.mkdir( parents=True , exist_ok=True )
		self.references_dir = self.storage_dir.joinpath( "references" )
		self.references_dir.mkdir( parents=True , exist_ok=True )
		self.problem_dois = set()

	def get_doi( self , doi ):
		if not doi:
			return None
		url = self.base_url + f"https://doi.org/{doi}"
		while True:
			r = requests.get( url , params=self.params , headers=self.headers )
			if r.status_code == 200:
				return r.json()
			elif r.status_code == 429:
				retry = int( r.headers.get( "Retry-After" , 5 ) )
				print( f"\nRate limited. Sleeping {retry}s" )
				time.sleep( retry )
			else:
				return None

	def search_title( self , title , per_page=10 ):
		url = self.base_url.rstrip( "/" )
		params = dict( self.params )
		params.update({
			"search": f'"{title}"',
			"per-page": per_page,
			"select": "id,doi,title,display_name,publication_year,cited_by_count,relevance_score,authorships"
		})
		while True:
			r = requests.get( url , params=params , headers=self.headers )
			if r.status_code == 200:
				return r.json().get( "results" , [] )
			elif r.status_code == 429:
				retry = int( r.headers.get( "Retry-After" , 5 ) )
				print( f"\nRate limited. Sleeping {retry}s" )
				time.sleep( retry )
			else:
				print( f"\nOpenAlex title search failed: {r.status_code} {r.url} {r.text[:500]}" )
				return []

	def get_id( self , open_alex_wid ):
		url = self.base_url + open_alex_wid
		for attempt in range( self.max_retries ):
			try:
				r = requests.get( url , params=self.params , headers=self.headers , timeout=30 )
				if r.status_code == 200:
					return r.json()
				elif r.status_code == 429:
					retry = int( r.headers.get( "Retry-After" , 5 ) )
					print( f"\nRate limited resolving refs. Sleeping {retry}s" )
					time.sleep( retry )
					continue
				else:
					return None
			except requests.exceptions.RequestException as e:
				wait = min( 2 ** attempt , 60 )
				print( f"\nNetwork error ({e}). Retry {attempt+1}/{self.max_retries}. Sleeping {wait}s" )
				time.sleep( wait )
		print( f"\nFailed after {self.max_retries} retries: {open_alex_wid}" )
		return None