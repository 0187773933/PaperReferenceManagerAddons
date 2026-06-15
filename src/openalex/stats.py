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
		self._index = []   # list of ( wid , entry , haystack , cite_count , included_in_missing )
		self.xlsx_path = self.args.output.joinpath( "missing.xlsx" )

		# Per-WID computation cache for compute(). Each reference work's
		# library-independent derived fields ( haystack + the xlsx-row
		# scalars + a compact author list ) plus its last dedup decision
		# live here , so a re-run only reads / parses / dedups references
		# it hasn't seen before instead of re-walking all ~57k reference
		# JSONs ( ~1.9 GB ) every time. Delete this file to force a full
		# recompute.
		self.cache_path = self.args.output.joinpath( "cache" , "missing_index_cache.json" )
		self._cache_version = 1

	def _entry_from_meta( self , meta ):
		"""Compact , library-independent cache entry for one reference work.
		Everything here derives only from the work's own OpenAlex meta , so
		it never goes stale once cached ( reference JSONs are write-once )."""
		e = utils.openalex_entry_scalars( meta )
		e[ "hay" ] = make_haystack( meta )
		return e

	def _load_cache( self ):
		if not self.cache_path.exists():
			return {}
		try:
			data = utils.read_json( self.cache_path ) or {}
		except Exception as e:
			print( f"missing :: cache unreadable ( {e} ) ; rebuilding" )
			return {}
		if data.get( "version" ) != self._cache_version:
			# Schema bump : ignore the old cache rather than misread it.
			return {}
		return data

	def _save_cache( self , lib_hash , entries ):
		# Compact ( un-indented ) write : this file holds ~57k entries , so
		# pretty-printing would roughly double its size and write time.
		import json
		payload = {
			"version":  self._cache_version ,
			"lib_hash": lib_hash ,
			"entries":  entries ,
		}
		try:
			with open( self.cache_path , "w" , encoding="utf-8" ) as f:
				json.dump( payload , f , ensure_ascii=False )
		except Exception as e:
			print( f"missing :: could not write cache ( {e} )" )

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
		lib_hash  = utils.hash_library_index( lib_index )
		lib_wids = set( zp.keys() ) | { fp.stem for fp in self.storage_dir.glob( "*.json" ) }

		# 3. Tally backward refs
		counts = Counter()
		for wid , p in tqdm( zp.items() , desc="Tallying refs" ):
			for r in p.get( "referenced_works" ) or []:
				rw = r.rsplit( "/" , 1 )[ -1 ]
				if rw in lib_wids:
					continue
				counts[ rw ] += 1

		# 4. Single pass: derive each ref's fields + dedup decision , reusing
		#    the per-WID cache where possible.
		#    - cached entry present  : reuse haystack / row scalars / authors
		#      ( library-independent ) for free , skipping the ~34 KB JSON
		#      read + abstract reconstruction ;
		#    - library unchanged ( cache lib_hash matches ) : reuse the cached
		#      dedup decision too , skipping the fuzzy title match ;
		#    - library changed : re-run dedup from the cached doi / title
		#      scalars ( still no JSON read ).
		#    `kept` becomes the next cache , so references no library paper
		#    cites anymore are pruned automatically.
		cache          = self._load_cache()
		old_entries    = cache.get( "entries" ) or {}
		dedup_reusable = bool( cache ) and cache.get( "lib_hash" ) == lib_hash
		kept           = {}

		self._index = []
		rows , skipped = [] , 0
		n_new_refs , n_redup = 0 , 0
		for rw , n in tqdm( counts.most_common() , desc="Building rows" ):
			entry = old_entries.get( rw )
			if entry is None:
				fp = self.references_dir / f"{rw}.json"
				meta = ( utils.read_json( fp ) or {} ) if fp.exists() else {}
				entry = self._entry_from_meta( meta )
				entry[ "is_dup" ] = utils.is_library_dup_fields(
					entry[ "doi" ] , entry[ "title" ] , lib_index )
				n_new_refs += 1
			elif not dedup_reusable:
				entry[ "is_dup" ] = utils.is_library_dup_fields(
					entry[ "doi" ] , entry[ "title" ] , lib_index )
				n_redup += 1

			kept[ rw ] = entry
			included = not entry[ "is_dup" ]
			self._index.append( ( rw , entry , entry[ "hay" ] , n , included ) )

			if not included:
				skipped += 1
				continue
			rows.append( utils.openalex_entry_to_xlsx_row( rw , entry , n ) )

		self._save_cache( lib_hash , kept )
		print(
			f"missing :: cache -- {n_new_refs} new refs computed , "
			f"{'dedup reused' if dedup_reusable else f'dedup re-run for {n_redup} ( library changed )'} , "
			f"{len(kept)} entries cached"
		)

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

		# 6. Top 100 Authors aggregated across non-dup missing references.
		#    entry[ 'authors' ] is already deduped per paper to
		#    ( id , name , orcid ) triples ( see openalex_entry_scalars ).
		author_papers = Counter()
		author_cites = Counter()
		author_meta = {}
		for wid , entry , hay , cite_count , included in tqdm( self._index , desc="Tallying authors" ):
			if not included:
				continue
			for aid , name , orcid in entry.get( "authors" ) or []:
				if not aid:
					continue
				author_papers[ aid ] += 1
				author_cites[ aid ] += cite_count
				if aid not in author_meta:
					author_meta[ aid ] = { "name": name or "(unknown)" , "orcid": orcid }

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

		# 7. Newest : the missing ( non-dup ) references sorted by most
		#    recent publication. We sort on entry[ 'pubdate' ] ( 'YYYY-MM-DD' ,
		#    lexicographically sortable ) with publication_year as a coarse
		#    fallback when a work has no full date , so this is finer-grained
		#    than sorting the rows on the Year column alone.
		newest_index = [
			( wid , entry , n )
			for wid , entry , hay , n , included in self._index
			if included
		]
		newest_index.sort(
			key=lambda t: ( t[ 1 ].get( "pubdate" ) or "" , t[ 1 ].get( "year" ) or 0 ) ,
			reverse=True ,
		)
		newest_rows = [
			utils.openalex_entry_to_xlsx_row( wid , entry , n )
			for wid , entry , n in newest_index[ :1000 ]
		]

		# 8. Build sheets
		HEADERS_ROW = [ "Cites" , "Title" , "Year" , "Proxy" , "DOI" , "Link" , "PDF" , "OA Cited-By" , "WID" ]
		sheets = [
			( "Top 1000 by Cites"     , HEADERS_ROW    , rows[ :1000 ] ),
			( "Top 1000 Newest"       , HEADERS_ROW    , newest_rows ),
			( "Top 1000 Cited-By"     , HEADERS_ROW    , forward_rows[ :1000 ] ),
			( f"Top {self.args.top_author_count} Authors"       , AUTHOR_HEADERS , author_rows ),
		]

		utils.write_xlsx( self.xlsx_path , sheets )
		print( f"resolved={len(zp)} missing={len(rows)} (deduped {skipped}) forward={len(forward_rows)} (deduped {forward_skipped})" )
		print( f"Created {self.xlsx_path}" )
		return self._index
