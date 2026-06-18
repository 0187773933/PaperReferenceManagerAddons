from .api import MendeleyAPI
from .local import MendeleyLocal
from ..utils import utils

class Mendeley:
	def __init__( self , args ):
		self.args = args
		self.API = MendeleyAPI( self.args )
		self.Local = MendeleyLocal( args )
		self.cache_path = self.args.output.joinpath( "cache" , "mendeley.jsonl" )

	def take_titles_and_dois( self ):
		"""Fast path for the 'exists' server : run the manager's
		in-memory snapshot ( jsonl cache for API mode , local SQLite
		for 'local' mode ) and return normalized ( titles , dois )
		sets directly. No roundtrip through output/cache/papers/ ."""
		if self.args.mendeley_source == "local":
			snap = self.Local.snapshot()
		else:
			snap = self.API.snapshot()
		titles , dois = set() , set()
		for _ , paper in ( snap or {} ).items():
			t = paper.get( "title" )
			if t:
				nt = utils.normalize_title( t )
				if nt:
					titles.add( nt )
			d = paper.get( "doi" )
			if d:
				nd = utils.normalize_doi( d )
				if nd:
					dois.add( nd )
		return titles , dois

	# Take a fresh snapshot of Mendeley and push every paper into the
	# unified output/cache/papers/{doi}.json store. NO per-manager
	# pickle is written.
	#
	# Calls the underlying API / Local snapshot directly to avoid
	# the legacy self.snapshot() pattern that rebinds the method to
	# the resulting dict ( latent bug -- second call would TypeError ).
	def save_snapshot( self ):
		if self.args.mendeley_source == "local":
			snap = self.Local.snapshot()
		else:
			snap = self.API.snapshot()
		self._push_to_db( snap )

	def _push_to_db( self , snap ):
		# Mendeley's snapshot is APPEND-ONLY by default. The API uses an
		# incremental `modified_since` fetch against a local jsonl cache ;
		# a transient API failure could miss IDs and a sync would then
		# wrongly delete papers that still exist in Mendeley. The user
		# can opt-in to pruning via --prune-mendeley when they're
		# confident the snapshot is complete.

		from ..db import papers
		pdf_cache = self.args.output.joinpath( "pdfs" , "mendeley" )
		prune = (
			getattr( self.args , "prune_mendeley" , False )
			and not getattr( self.args , "no_prune" , False )
		)
		seen_keys = set()

		n_new , n_upd , n_noop , n_no_doi = 0 , 0 , 0 , 0
		for paper_id , paper in snap.items():
			doi = utils.normalize_doi( paper.get( "doi" ) )
			title = paper.get( "title" )
			pdf_hosted = paper.get( "pdf_hosted" ) or []
			pdfs = [
				str( pdf_cache.joinpath( f[ "file_name" ] ) )
				for f in pdf_hosted
				if f.get( "file_name" )
			]
			# No DOI : still include under a synthetic key ( see zotero._push_to_db
			# for the rationale ). Skip only truly empty placeholders.
			if doi:
				pkey = doi
			else:
				if not ( title or pdfs ):
					continue
				pkey = papers.synthetic_key(
					papers.SOURCE_MENDELEY , paper.get( "id" ) or paper_id ,
				)
				n_no_doi += 1
			seen_keys.add( pkey )
			source_fields = {
				"id":         paper.get( "id" ) ,
				"url":        paper.get( "url" ) ,
				"date":       paper.get( "date" ) ,
				"modified":   paper.get( "modified" ) ,
				"type":       paper.get( "type" ) ,
				"pdf_hosted": pdf_hosted ,
				"pdf_links":  paper.get( "pdf_links" ) or [] ,
				"pdfs":       pdfs ,
			}
			_ , created , changed = papers.upsert_source(
				self.args , doi , papers.SOURCE_MENDELEY , source_fields ,
				title=title , key=pkey ,
			)
			if created:
				n_new += 1
			elif changed:
				n_upd += 1
			else:
				n_noop += 1

		n_detached , n_deleted = 0 , 0
		if prune:
			n_detached , n_deleted = papers.prune_source(
				self.args , papers.SOURCE_MENDELEY , seen_keys ,
			)

		total = papers.count( self.args )
		print(
			f"Mendeley :: snapshot -> papers/ : +{n_new} new , ~{n_upd} updated , "
			f"={n_noop} unchanged , "
			f"-{n_detached} source-detached , -{n_deleted} paper-deleted , "
			f"included {n_no_doi} no-doi ; total = {total}"
		)

	def download( self ):
		if self.args.mendeley_source == "api":
			self.API.download_snapshot_pdfs()
