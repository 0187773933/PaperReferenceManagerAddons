#!/usr/bin/env python3
import time
from pathlib import Path
from pprint import pprint
from tqdm import tqdm
from collections import Counter
from rapidfuzz import fuzz , process

from ..utils import utils

FUZZ_TITLE_THRESHOLD = 92

# ---------- search helpers ----------
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
		lib_dois = { utils.normalize_doi( i[ "doi" ] ) for i in snapshot.values() if i.get( "doi" ) }
		lib_dois.discard( None )
		# Lowercased + alphanumeric-only titles for both exact-set and fuzzy comparison.
		lib_titles_list = [
			utils.normalize_title( i[ "title" ] )
			for i in snapshot.values() if i.get( "title" )
		]
		lib_titles_list = [ t for t in lib_titles_list if t ]
		lib_titles_set = set( lib_titles_list )
		# use file stems as fallback so files with missing/malformed id fields are still covered
		lib_wids = set( zp.keys() ) | { fp.stem for fp in self.storage_dir.glob( "*.json" ) }

		# 3. Tally refs (skip library wids early)
		counts = Counter()
		for wid , p in tqdm( zp.items() , desc="Tallying refs" ):
			for r in p.get( "referenced_works" ) or []:
				rw = r.rsplit( "/" , 1 )[ -1 ]
				if rw in lib_wids:
					continue
				counts[ rw ] += 1

		# 4. Single pass: load each ref once, build row, stash haystack
		#    self._index keeps everything for downstream search sheets
		self._index = []
		rows , skipped = [] , 0
		for rw , n in tqdm( counts.most_common() , desc="Building rows" ):
			fp = self.references_dir / f"{rw}.json"
			meta = ( utils.read_json( fp ) or {} ) if fp.exists() else {}
			haystack = make_haystack( meta )

			rt = meta.get( "title" ) or meta.get( "display_name" )
			rt_norm = utils.normalize_title( rt ) if rt else None
			rd = meta.get( "doi" )
			rd_norm = utils.normalize_doi( rd ) if rd else None

			is_dup = False
			if rd_norm and rd_norm in lib_dois:
				is_dup = True
			elif rt_norm:
				if rt_norm in lib_titles_set:
					is_dup = True
				else:
					match = process.extractOne(
						rt_norm ,
						lib_titles_list ,
						scorer=fuzz.token_set_ratio ,
						score_cutoff=FUZZ_TITLE_THRESHOLD ,
					)
					if match:
						is_dup = True
			self._index.append( ( rw , meta , haystack , n , not is_dup ) )

			if is_dup:
				skipped += 1
				continue
			rows.append( utils.openalex_to_xlsx_row( rw , meta , n ) )

		# 5. Build base sheets
		HEADERS_ROW = [ "Cites" , "Title" , "Year" , "Proxy" , "DOI" , "Link" , "OA Cited-By" , "WID" ]
		sheets = [
			( "Top 1000 by Cites"   , HEADERS_ROW , rows[ :1000 ] ),
			# ( "Top 1000 by Recency" , HEADERS_ROW , sorted( rows , key=lambda r: r[ 2 ] or 0 , reverse=True )[ :1000 ] ),
		]

		utils.write_xlsx( self.xlsx_path , sheets )
		print( f"resolved={len(zp)} missing={len(rows)} (deduped {skipped}) -> {self.xlsx_path}" )
		return self._index