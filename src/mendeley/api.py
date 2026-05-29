import json
import requests
from pathlib import Path
import time
from pprint import pprint
from rapidfuzz import fuzz , process
from tqdm import tqdm

from .auth import MendeleyAuth
from ..utils import utils

class MendeleyAPI():
	def __init__( self , args ):
		self.args = args
		self.Auth = MendeleyAuth( args )
		self.api_base   = "https://api.mendeley.com"
		self.cache_path = self.args.output.joinpath( "cache" , "mendeley.jsonl" )
		self.pdf_download_path = self.args.output.joinpath( "pdfs" , "mendeley" )
		self.pdf_download_path.mkdir( parents=True , exist_ok=True )
		self._index_titles = None
		self._index_dois   = None
		self._index_ids    = None   # ids the current index was built from
		self._index_time   = 0.0    # last time we refreshed
		self._index_ttl    = 300    # seconds before we re-hit snapshot()
		self.access_token = None
		self.session = None

	def refresh_session( self ):
		if not self.access_token:
			self.access_token = self.Auth.get_access_token()
		if not self.session:
			self.session = requests.Session()
			self.session.headers[ "Authorization" ] = f"Bearer {self.access_token}"

	def files_for_document( self , document_id ):
		try:
			r = self.session.get( self.api_base + "/files" ,
				params={ "document_id": document_id } ,
				headers={ "Accept": "application/vnd.mendeley-file.1+json" } ,
				timeout=30
			)
			r.raise_for_status()
			return r.json()
		except Exception as e:
			print( e )
			return {}

	def download_file( self , file_id , file_name ):
		try:
			r = self.session.get(  self.api_base + f"/files/{file_id}" ,
				headers={ "Accept": "application/vnd.mendeley-file.1+json" } ,
				timeout=30 ,
				allow_redirects=False
			)
			if r.status_code not in ( 302 , 303 ):
				raise RuntimeError(
					f"Expected redirect for file download, got HTTP {r.status_code}"
				)
			download_url = r.headers.get( "Location" )
			if not download_url:
				raise RuntimeError( "Mendeley file response did not include Location header" )
			file_response = requests.get(
				download_url ,
				timeout=120
			)
			file_response.raise_for_status()
			out_path = self.pdf_download_path.joinpath( file_name )
			out_path.write_bytes( file_response.content )
			return out_path
		except Exception as e:
			print( e )
			return False

	def take_snapshot( self , modified_since=None ):
		self.refresh_session()
		url = self.api_base + "/documents"
		params = { "limit": 500 , "view": "all" }
		if modified_since:
			params[ "modified_since" ] = modified_since
		print( "Mendeley :: Taking Snapshot" )
		while url:
			r = self.session.get( url , params=params , headers={
				"Accept": "application/vnd.mendeley-document.1+json"
			} , timeout=30 )
			r.raise_for_status()
			for item in r.json():
				ids = item.get( "identifiers" , {} ) or {}
				doi = ids.get( "doi" )
				websites = item.get( "websites" )
				file_attached = item.get( "file_attached" )
				doc_id = item.get( "id" )
				pdf_hosted = []
				pdf_links = []
				if file_attached == True:
					files = self.files_for_document( doc_id )
					for file in files:
						if file.get( "mime_type" ) == "application/pdf":
							pdf_hosted.append( { "id": file.get( "id" ) , "file_name": file.get( "file_name" ) } )
				if websites:
					for x in websites:
						if ".pdf" in x:
							pdf_links.append( x )
				doc = {
					"doi":      doi ,
					"id":       doc_id ,
					"title":    item.get( "title" ) ,
					"url":      f"https://doi.org/{doi}" ,
					"date":     item.get( "year" ) ,
					"modified": item.get( "last_modified" ) ,
					"type": item.get( "type" ) ,
					"pdf_hosted": pdf_hosted ,
					"pdf_links": pdf_links
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
				nt = utils.normalize_title( d[ "title" ] )
				if nt:
					titles.add( nt )
			if d.get( "doi" ):
				nd = utils.normalize_doi( d[ "doi" ] )
				if nd:
					dois.add( nd )
		self._index_titles = titles
		self._index_dois   = dois
		self._index_ids    = ids
		return titles , dois

	def title_exists( self , title , threshold=96 ):
		titles , _ = self._index()
		if isinstance( title , ( list , tuple , set ) ):
			return {
				t: bool( process.extractOne(
					utils.normalize_title( t ) , titles ,
					scorer=fuzz.token_sort_ratio ,
					score_cutoff=threshold
				) )
				for t in title
			}
		return bool( process.extractOne(
			utils.normalize_title( title ) , titles ,
			scorer=fuzz.token_sort_ratio ,
			score_cutoff=threshold
		) )

	def doi_exists( self , doi ):
		_ , dois = self._index()
		if isinstance( doi , ( list , tuple , set ) ):
			return { x: ( utils.normalize_doi( x ) in dois ) for x in doi }
		return utils.normalize_doi( doi ) in dois

	def download_snapshot_pdfs( self ):
		if not self.cache_path.exists():
			print( f"Mendeley :: No snapshot cache at {self.cache_path}" )
			self.snapshot()
		else:
			self.refresh_session()
		papers = []
		with self.cache_path.open( encoding="utf-8" ) as fh:
			for line in fh:
				if line.strip():
					papers.append( json.loads( line ) )
		total_files = sum( len( p.get( "pdf_hosted" ) or [] ) for p in papers )
		print( f"Mendeley :: Downloading up to {total_files} PDFs across {len(papers)} papers -> {self.pdf_download_path}" )
		downloaded , skipped , failed = 0 , 0 , 0
		file_bar = tqdm( total=total_files , desc="PDFs" , position=1 , leave=False )
		for paper in tqdm( papers , desc="Papers" , position=0 ):
			pdf_hosted = paper.get( "pdf_hosted" ) or []
			for entry in pdf_hosted:
				file_id = entry.get( "id" )
				file_name = entry.get( "file_name" )
				if not file_id or not file_name:
					failed += 1
					file_bar.update( 1 )
					continue
				out_path = self.pdf_download_path.joinpath( file_name )
				if out_path.exists():
					skipped += 1
					file_bar.update( 1 )
					continue
				result = self.download_file( file_id , file_name )
				if result:
					downloaded += 1
				else:
					failed += 1
				file_bar.update( 1 )
		file_bar.close()
		print( f"Mendeley :: PDFs downloaded={downloaded} skipped={skipped} failed={failed}" )