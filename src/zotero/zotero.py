import os
import glob
import tempfile
import shutil
import sqlite3
from pprint import pprint
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..utils import utils

class Zotero():
	def __init__( self , args ):
		self.args = args
		if args.zotero_sqlite:
			self.sqlite_path = Path( args.zotero_sqlite )
		else:
			self.sqlite_path = self.find_db()
		self.storage = self.sqlite_path.parent / "storage"

	def find_db( self ):
		candidates = [
			os.path.expanduser( "~/Zotero/zotero.sqlite" ),
			os.path.join( os.environ.get( "USERPROFILE" , "" ) , "Zotero" , "zotero.sqlite" ) ,
			os.path.expanduser( "~/.zotero/zotero/*/zotero.sqlite" ) ,
		]
		for pattern in candidates:
			hits = glob.glob( pattern )
			if hits:
				return Path( hits[ 0 ] )
		raise FileNotFoundError( "No zotero.sqlite found - check paths above" )

	def open_snapshot( self ):
		tmpdir = Path( tempfile.mkdtemp( prefix="zotero_db_" ) )
		tmpdb = tmpdir / "zotero.sqlite"
		shutil.copy2( self.sqlite_path , tmpdb )
		conn = sqlite3.connect( tmpdb )
		conn.row_factory = sqlite3.Row
		return conn

	def take_titles_and_dois( self ):
		"""Fast path for the 'exists' server : pull ONLY title + DOI
		fields straight from the SQLite copy and return normalized
		( titles_set , dois_set ) . No creator / attachment / tag /
		collection joins ; no roundtrip through output/cache/papers/ .
		Typical run is ~50-200 ms for a 2000-paper library vs ~10-30
		seconds for take_snapshot() + _push_to_db()."""
		conn = self.open_snapshot()
		try:
			c = conn.cursor()
			titles , dois = set() , set()
			for row in c.execute( """
				SELECT fields.fieldName , itemDataValues.value
				FROM itemData
				JOIN fields          ON fields.fieldID         = itemData.fieldID
				JOIN itemDataValues  ON itemDataValues.valueID = itemData.valueID
				JOIN items           ON items.itemID           = itemData.itemID
				JOIN itemTypes       ON itemTypes.itemTypeID   = items.itemTypeID
				LEFT JOIN deletedItems ON deletedItems.itemID  = items.itemID
				WHERE deletedItems.itemID IS NULL
				  AND itemTypes.typeName NOT IN ( 'attachment' , 'note' , 'annotation' )
				  AND fields.fieldName    IN ( 'title' , 'DOI' )
			""" ):
				field = row[ "fieldName" ]
				value = row[ "value" ]
				if not value:
					continue
				if field == "title":
					t = utils.normalize_title( value )
					if t:
						titles.add( t )
				elif field == "DOI":
					d = utils.normalize_doi( value )
					if d:
						dois.add( d )
			return titles , dois
		finally:
			try: conn.close()
			except Exception: pass

	def take_snapshot( self ):
		conn = self.open_snapshot()
		c = conn.cursor()

		# --------------------------------------------------
		# 1) BASE ITEMS: ONLY "REAL" BIB ITEMS (exclude attachments/notes/annotations)
		# --------------------------------------------------
		# Zotero's UI count (~681) corresponds to bibliographic items, not the raw items table.
		EXCLUDE_TYPES = ( "attachment" , "note" , "annotation" )

		papers = {}

		for row in c.execute("""
			SELECT items.itemID, items.key, itemTypes.typeName
			FROM items
			JOIN itemTypes ON itemTypes.itemTypeID = items.itemTypeID
			LEFT JOIN deletedItems ON deletedItems.itemID = items.itemID
			WHERE deletedItems.itemID IS NULL
			  AND itemTypes.typeName NOT IN ('attachment','note','annotation')
		"""):
			itemID = row["itemID"]
			papers[itemID] = {
				"itemID": itemID,
				"key": row["key"],
				"type": row["typeName"],
				"doi": None,
				"attachments": [],
				"meta": {},
				"creators": [],
				"tags": [],
				"collections": []
			}

		# --------------------------------------------------
		# 2) METADATA (title, DOI, journal, year, etc)
		# --------------------------------------------------
		for row in c.execute("""
			SELECT itemData.itemID, fields.fieldName, itemDataValues.value
			FROM itemData
			JOIN fields ON fields.fieldID = itemData.fieldID
			JOIN itemDataValues ON itemDataValues.valueID = itemData.valueID
		"""):
			itemID = row["itemID"]
			if itemID not in papers:
				continue

			field = row["fieldName"]
			value = row["value"]

			papers[itemID]["meta"][field] = value

			if field == "DOI" and value:
				papers[itemID]["doi"] = utils.normalize_doi(value)

		# --------------------------------------------------
		# 3) CREATORS (authors/editors)
		# --------------------------------------------------
		for row in c.execute("""
			SELECT itemCreators.itemID,
				   creators.firstName,
				   creators.lastName,
				   creatorTypes.creatorType
			FROM itemCreators
			JOIN creators ON creators.creatorID = itemCreators.creatorID
			JOIN creatorTypes ON creatorTypes.creatorTypeID = itemCreators.creatorTypeID
			ORDER BY itemCreators.itemID, itemCreators.orderIndex
		"""):
			itemID = row["itemID"]
			if itemID not in papers:
				continue

			papers[itemID]["creators"].append({
				"type": row["creatorType"],
				"first": row["firstName"],
				"last": row["lastName"]
			})

		# --------------------------------------------------
		# 4) ALL ATTACHMENTS (child items) grouped onto their parent bib item
		# --------------------------------------------------
		for row in c.execute("""
			SELECT itemAttachments.parentItemID AS parentID,
				   itemAttachments.itemID       AS attachItemID,
				   items.key                    AS attachKey,
				   itemAttachments.path         AS path,
				   itemAttachments.contentType  AS contentType,
				   itemAttachments.linkMode     AS linkMode
			FROM itemAttachments
			JOIN items ON items.itemID = itemAttachments.itemID
		"""):

			parentID = row["parentID"]

			# if missing parent, create placeholder
			if parentID not in papers:
				papers[parentID] = {
					"itemID": parentID,
					"key": None,
					"type": "unknown",
					"doi": None,
					"attachments": [],
					"meta": {},
					"creators": [],
					"tags": [],
					"collections": []
				}

			path = row["path"]
			attachKey = row["attachKey"]

			file_path = None

			if path:
				if path.startswith("storage:"):
					rel = path.replace("storage:", "", 1)
					file_path = self.storage / attachKey / rel

				elif path.startswith("file:"):
					file_path = Path(unquote(urlparse(path).path))

			papers[parentID]["attachments"].append({
				"key": attachKey,
				"parent_id": parentID ,
				"contentType": row["contentType"],
				"linkMode": row["linkMode"],
				"path": path,
				"abs_path": str(file_path) if file_path else None
			})

		# --------------------------------------------------
		# 5) TAGS (only for base bib items)
		# --------------------------------------------------
		for row in c.execute("""
			SELECT itemTags.itemID, tags.name
			FROM itemTags
			JOIN tags ON tags.tagID = itemTags.tagID
		"""):
			itemID = row["itemID"]
			if itemID not in papers:
				continue
			papers[itemID]["tags"].append(row["name"])

		# --------------------------------------------------
		# 6) COLLECTIONS (only for base bib items)
		# --------------------------------------------------
		for row in c.execute("""
			SELECT collectionItems.itemID, collections.collectionName
			FROM collectionItems
			JOIN collections ON collections.collectionID = collectionItems.collectionID
		"""):
			itemID = row["itemID"]
			if itemID not in papers:
				continue
			papers[itemID]["collections"].append(row["collectionName"])

		conn.close()

		# Sort tag/collection lists for stability
		for item in papers.values():
			item["tags"] = sorted(set(item["tags"]))
			item["collections"] = sorted(set(item["collections"]))

		# Return keyed by Zotero key (one per bib item)
		return {item["key"]: item for item in papers.values()}

	# Take a fresh snapshot of Zotero and push every paper into the
	# unified output/cache/papers/{doi}.json store. NO per-manager
	# pickle is written -- the papers/ directory is the only source of
	# truth.
	#
	# Idempotent : papers already in the DB get their 'zotero' source
	# field refreshed ; papers from Mendeley ( or any other manager )
	# are untouched.
	def save_snapshot( self ):
		full = self.take_snapshot()      # full per-item dicts incl. tags/collections
		self._push_to_db( full )

	def _push_to_db( self , full ):
		from ..db import papers
		# Zotero's SQLite snapshot is authoritative ( full local copy ) ,
		# so we sync : papers that disappeared from Zotero get their
		# 'zotero' source detached , and zotero-only papers get deleted
		# entirely. Use --no-prune to disable.
		prune = not getattr( self.args , "no_prune" , False )
		seen_keys = set()
		non_imported = []

		n_new , n_upd , n_noop , n_no_doi = 0 , 0 , 0 , 0
		for key , item in full.items():
			meta = item.get( "meta" ) or {}
			doi = utils.normalize_doi(
				item.get( "doi" ) or meta.get( "DOI" )
			)
			title = item.get( "title" ) or meta.get( "title" )
			pdfs = [
				a.get( "abs_path" )
				for a in ( item.get( "attachments" ) or [] )
				if a.get( "abs_path" ) and str( a.get( "abs_path" ) ).lower().endswith( ".pdf" )
			]
			# No DOI : still include the paper under a synthetic key so it
			# flows through the PDF pipeline ( yolo / images / md / ... ) and
			# gets a shot at OpenAlex title-search. Skip only truly empty
			# placeholders ( no title AND no PDF ) to avoid DB junk.
			if doi:
				pkey = doi
			else:
				if not ( title or pdfs ):
					# Nothing for the pipeline to act on -- record it so the
					# user can see what their library holds that we skip.
					non_imported.append( {
						"id":          key or item.get( "itemID" ) ,
						"itemID":      item.get( "itemID" ) ,
						"type":        item.get( "type" ) ,
						"url":         meta.get( "url" ) ,
						"date":        meta.get( "date" ) ,
						"creators":    item.get( "creators" ) or [] ,
						"tags":        item.get( "tags" ) or [] ,
						"collections": item.get( "collections" ) or [] ,
					} )
					continue
				# Prefer the Zotero item key ; fall back to the always-unique
				# itemID for orphan placeholder items whose key is None.
				pkey = papers.synthetic_key(
					papers.SOURCE_ZOTERO , key or item.get( "itemID" ) ,
				)
				n_no_doi += 1
			seen_keys.add( pkey )
			source_fields = {
				"key":         key ,
				"itemID":      item.get( "itemID" ) ,
				"type":        item.get( "type" ) ,
				"url":         meta.get( "url" ) ,
				"date":        meta.get( "date" ) ,
				"creators":    item.get( "creators" ) or [] ,
				"tags":        item.get( "tags" ) or [] ,
				"collections": item.get( "collections" ) or [] ,
				"pdfs":        pdfs ,
			}
			_ , created , changed = papers.upsert_source(
				self.args , doi , papers.SOURCE_ZOTERO , source_fields ,
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
				self.args , papers.SOURCE_ZOTERO , seen_keys ,
			)

		n_skipped = papers.save_non_imported(
			self.args , papers.SOURCE_ZOTERO , non_imported ,
		)

		total = papers.count( self.args )
		print(
			f"Zotero :: snapshot -> papers/ : +{n_new} new , ~{n_upd} updated , "
			f"={n_noop} unchanged , "
			f"-{n_detached} source-detached , -{n_deleted} paper-deleted , "
			f"included {n_no_doi} no-doi , skipped {n_skipped} non-imported ; "
			f"total = {total}"
		)
