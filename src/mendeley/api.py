import json
import requests
from pathlib import Path
from pprint import pprint

from .auth import MendeleyAuth

class MendeleyAPI():
	def __init__( self , args ):
		self.args = args
		self.Auth = MendeleyAuth( args )
		self.api_base   = "https://api.mendeley.com"
		self.cache_path = Path.cwd().joinpath( "output" , "cache" , "mendeley.jsonl" )
		self._index_titles = None
		self._index_dois   = None
		self._index_ids    = None   # ids the current index was built from
		self._index_time   = 0.0    # last time we refreshed
		self._index_ttl    = 300    # seconds before we re-hit snapshot()

	def take_snapshot( self , modified_since=None ):
		self.access_token = self.Auth.get_access_token()
		sess = requests.Session()
		sess.headers[ "Authorization" ] = f"Bearer {self.access_token}"
		url = self.api_base + "/documents"
		params = { "limit": 500 , "view": "all" }
		if modified_since:
			params[ "modified_since" ] = modified_since
		while url:
			r = sess.get( url , params=params , headers={
				"Accept": "application/vnd.mendeley-document.1+json"
			} , timeout=30 )
			r.raise_for_status()
			for item in r.json():
				ids = item.get( "identifiers" , {} ) or {}
				doi = ids.get( "doi" )
				doc = {
					"doi":      doi ,
					"id":       item.get( "id" ) ,
					"title":    item.get( "title" ) ,
					"url":      f"https://doi.org/{doi}" ,
					"date":     item.get( "year" ) ,
					"modified": item.get( "last_modified" ) ,
				}
				yield doc
			params = None
			url = r.links.get( "next" , {} ).get( "url" )

	def snapshot( self ):
		papers = {}
		if self.cache_path.exists():
			with self.cache_path.open( encoding="utf-8" ) as f:
				for line in f:
					if line.strip():
						d = json.loads( line )
						papers[ d[ "id" ] ] = d
		modified_since = None
		if papers:
			stamps = [ d.get( "modified" ) for d in papers.values() if d.get( "modified" ) ]
			if stamps:
				modified_since = max( stamps )
		new_count = 0
		for doc in self.take_snapshot( modified_since=modified_since ):
			if doc[ "id" ] not in papers:
				new_count += 1
			papers[ doc[ "id" ] ] = doc  # add or overwrite if edited
		self.cache_path.parent.mkdir( parents=True , exist_ok=True )
		with self.cache_path.open( "w" , encoding="utf-8" ) as f:
			for d in papers.values():
				f.write( json.dumps( d , ensure_ascii=False ) + "\n" )
		print( f"{new_count} new/updated -> {len(papers)} total in {self.cache_path}" )
		return papers

	def _index( self ):
		if (
			self._index_titles is not None
			and ( time.time() - self._index_time ) < self._index_ttl
		):
			return self._index_titles , self._index_dois
		papers = self.snapshot()
		ids = frozenset( papers.keys() )
		self._index_time = time.time()
		if ids == self._index_ids and self._index_titles is not None:
			return self._index_titles , self._index_dois
		titles , dois = set() , set()
		for d in papers.values():
			if d.get( "title" ):
				nt = normalize_title( d[ "title" ] )
				if nt:
					titles.add( nt )
			if d.get( "doi" ):
				nd = normalize_doi( d[ "doi" ] )
				if nd:
					dois.add( nd )
		self._index_titles = titles
		self._index_dois   = dois
		self._index_ids    = ids
		return titles , dois

	def title_exists( self , title ):
		titles , _ = self._index()
		if isinstance( title , ( list , tuple , set ) ):
			return { t: normalize_title( t ) in titles for t in title }
		return normalize_title( title ) in titles

	def doi_exists( self , doi ):
		_ , dois = self._index()
		if isinstance( doi , ( list , tuple , set ) ):
			return { x: ( normalize_doi( x ) in dois ) for x in doi }
		return normalize_doi( doi ) in dois