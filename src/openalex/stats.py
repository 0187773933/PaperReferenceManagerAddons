#!/usr/bin/env python3
import time
from pathlib import Path
from pprint import pprint
from tqdm import tqdm
from collections import Counter

from ..utils import utils

def reconstruct_abstract( inv_index ):
	if not inv_index: return ""
	positions = [ ( pos , word ) for word , poses in inv_index.items() for pos in poses ]
	positions.sort()
	return " ".join( w for _ , w in positions )

def make_haystack( meta ):
	title = meta.get( "title" ) or meta.get( "display_name" ) or ""
	abstract = reconstruct_abstract( meta.get( "abstract_inverted_index" ) )
	return ( title + " " + abstract ).lower()

class OpenAlexStats():
	def __init__( self , args ):
		self.args = args
		self.config = utils.read_yaml( self.args.config.joinpath( "config.yaml" ) )

		self.storage_dir = self.args.output.joinpath( "cache" , "openalex" )
		self.storage_dir.mkdir( parents=True , exist_ok=True )
		self.references_dir = self.storage_dir.joinpath( "references" )
		self.references_dir.mkdir( parents=True , exist_ok=True )

		# Populated by stats(); reused by Search
		self._index = []   # list of ( wid , meta , haystack , cite_count , included_in_missing )
		self.xlsx_path = self.args.output.joinpath( "missing.xlsx" )

	def compute( self , snapshot ):
		# 1. OpenAlex data we have for library papers
		zp = {}
		for fp in tqdm( list( self.storage_dir.glob( "*.json" ) ) , desc="Loading library" ):
			d = utils.read_json( fp ) or {}
			oid = d.get( "id" , "" )
			if isinstance( oid , str ) and oid.startswith( "https://openalex.org/" ):
				zp[ oid.rsplit( "/" , 1 )[ -1 ] ] = d

		# 2. Source of truth: snapshot
		lib_index = utils.build_library_dedup_index( snapshot )
		lib_wids = set( zp.keys() ) | { fp.stem for fp in self.storage_dir.glob( "*.json" ) }

		# 3. Tally backward refs
		counts = Counter()
		for wid , p in tqdm( zp.items() , desc="Tallying refs" ):
			for r in p.get( "referenced_works" ) or []:
				rw = r.rsplit( "/" , 1 )[ -1 ]
				if rw in lib_wids:
					continue
				counts[ rw ] += 1

		# 4. Single pass: load each ref, dedup, stash haystack
		self._index = []
		rows , skipped = [] , 0
		for rw , n in tqdm( counts.most_common() , desc="Building rows" ):
			fp = self.references_dir / f"{rw}.json"
			meta = ( utils.read_json( fp ) or {} ) if fp.exists() else {}
			haystack = make_haystack( meta )

			is_dup = utils.is_library_dup( meta , lib_index )
			self._index.append( ( rw , meta , haystack , n , not is_dup ) )

			if is_dup:
				skipped += 1
				continue
			rows.append( utils.openalex_to_xlsx_row( rw , meta , n ) )

		# 5. Forward direction: tally papers citing your library (read from cached JSONs)
		forward_counts = Counter()
		forward_meta = {}
		for lib_paper in tqdm( zp.values() , desc="Tallying cited-by" ):
			for cw in lib_paper.get( "cited_by_works" ) or []:
				cw_id = cw.get( "id" ) or ""
				cwid = cw_id.rsplit( "/" , 1 )[ -1 ] if cw_id else ""
				if not cwid or cwid in lib_wids:
					continue
				forward_counts[ cwid ] += 1
				if cwid not in forward_meta:
					forward_meta[ cwid ] = cw

		forward_rows , forward_skipped = [] , 0
		for cwid , n in tqdm( forward_counts.most_common() , desc="Building cited-by rows" ):
			meta = forward_meta[ cwid ]
			if utils.is_library_dup( meta , lib_index ):
				forward_skipped += 1
				continue
			forward_rows.append( utils.openalex_to_xlsx_row( cwid , meta , n ) )

		# 6. Top 100 Authors aggregated across non-dup missing references
		author_papers = Counter()
		author_cites = Counter()
		author_meta = {}
		for wid , meta , hay , cite_count , included in tqdm( self._index , desc="Tallying authors" ):
			if not included:
				continue
			seen = set()
			for a in meta.get( "authorships" ) or []:
				author = a.get( "author" ) or {}
				aid = author.get( "id" )
				if not aid or aid in seen:
					continue
				seen.add( aid )
				author_papers[ aid ] += 1
				author_cites[ aid ] += cite_count
				if aid not in author_meta:
					author_meta[ aid ] = {
						"name": author.get( "display_name" ) or "(unknown)" ,
						"orcid": author.get( "orcid" ) ,
					}

		AUTHOR_HEADERS = [ "Library Cites" , "Papers" , "Author" , "ORCID" , "OpenAlex Link" ]
		ranked_authors = sorted(
			author_cites.items() ,
			key=lambda kv: ( kv[ 1 ] , author_papers[ kv[ 0 ] ] ) ,
			reverse=True ,
		)[ : self.args.top_author_count ]
		author_rows = []
		for aid , total in ranked_authors:
			info = author_meta.get( aid , {} )
			orcid_url = info.get( "orcid" )
			author_rows.append([
				total ,
				author_papers[ aid ] ,
				info.get( "name" ) or "(unknown)" ,
				utils.Link( orcid_url , orcid_url ) if orcid_url else "" ,
				utils.Link( aid , aid ) if aid else "" ,
			])

		# 7. Build sheets
		HEADERS_ROW = [ "Cites" , "Title" , "Year" , "Proxy" , "DOI" , "Link" , "PDF" , "OA Cited-By" , "WID" ]
		sheets = [
			( "Top 1000 by Cites"     , HEADERS_ROW    , rows[ :1000 ] ),
			( "Top 1000 Cited-By"     , HEADERS_ROW    , forward_rows[ :1000 ] ),
			( f"Top {self.args.top_author_count} Authors"       , AUTHOR_HEADERS , author_rows ),
			# ( "Top 1000 by Recency" , HEADERS_ROW , sorted( rows , key=lambda r: r[ 2 ] or 0 , reverse=True )[ :1000 ] ),
		]

		utils.write_xlsx( self.xlsx_path , sheets )
		print( f"resolved={len(zp)} missing={len(rows)} (deduped {skipped}) forward={len(forward_rows)} (deduped {forward_skipped})" )
		print( f"Created {self.xlsx_path}" )
		return self._index
