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

	def save_snapshot( self ):
		snap = self.snapshot()
		cache_dir = self.args.output.joinpath( "cache" )
		cache_dir.mkdir( parents=True , exist_ok=True )
		out_path = cache_dir.joinpath( "zotero.snapshot" )
		utils.write_pickle( out_path , snap )
		print( f"Zotero :: snapshot pickled -- {len(snap)} papers -> {out_path}" )
		return out_path

	def snapshot( self ):
		_snapshot = self.take_snapshot()
		papers = {}
		for key in _snapshot:
			item = _snapshot[ key ]
			item_id = str( item.get( "itemID" ) )
			doi = item.get( "doi" ) or item.get( "meta" ).get( "DOI" )
			title = item.get( "title" ) or item.get( "meta" ).get( "title" )
			url = item.get( "meta" ).get( "url" )
			date = item.get( "meta" ).get( "date" )
			pdf_paths = [
				p.get( "abs_path" )
				for p in item.get( "attachments" , [] )
				if p.get( "abs_path" ) and p.get( "abs_path" ).lower().endswith( ".pdf" )
			]
			paper = {
				"doi": doi ,
				"id": item_id ,
				"title": title ,
				"url": url ,
				"date": date ,
				"pdfs": pdf_paths
			}
			papers[ key ] = paper
		return papers

	def yolo( self ):
		from pathlib import Path
		from tqdm import tqdm
		from ..pdf import pdf
		papers = self.snapshot()

		jobs = []
		skip_no_doi , skip_no_pdf , skip_missing , skip_done = 0 , 0 , 0 , 0
		for key , paper in papers.items():
			if not utils.normalize_doi( paper.get( "doi" ) ):
				skip_no_doi += 1
				continue
			pdfs = paper.get( "pdfs" ) or []
			if not pdfs:
				skip_no_pdf += 1
				continue
			for raw in pdfs:
				pdf_path = Path( raw )
				if not pdf_path.exists():
					skip_missing += 1
					continue
				if pdf_path.with_suffix( ".yolo.json" ).exists():
					skip_done += 1
					continue
				jobs.append( pdf_path )

		print(
			f"Zotero :: YOLO -- {len(jobs)} pdfs to process "
			f"( skipped: no-doi={skip_no_doi} no-pdf={skip_no_pdf} "
			f"not-on-disk={skip_missing} already-done={skip_done} )"
		)

		outer = tqdm( jobs , desc="PDFs" , position=1 , leave=True , unit="pdf" )
		for pdf_path in outer:
			outer.set_postfix_str( pdf_path.name[ :60 ] )
			yolo_path = pdf_path.with_suffix( ".yolo.json" )
			result = pdf.yolo( pdf_path , do_deskew=self.args.pdf_deskew )
			utils.write_json( yolo_path , result )