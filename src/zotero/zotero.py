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
		seen_dois = set()

		n_new , n_upd , n_no_doi = 0 , 0 , 0
		for key , item in full.items():
			meta = item.get( "meta" ) or {}
			doi = utils.normalize_doi(
				item.get( "doi" ) or meta.get( "DOI" )
			)
			if not doi:
				n_no_doi += 1
				continue
			seen_dois.add( doi )
			pdfs = [
				a.get( "abs_path" )
				for a in ( item.get( "attachments" ) or [] )
				if a.get( "abs_path" ) and str( a.get( "abs_path" ) ).lower().endswith( ".pdf" )
			]
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
			_ , created = papers.upsert_source(
				self.args , doi , papers.SOURCE_ZOTERO , source_fields ,
				title=item.get( "title" ) or meta.get( "title" ) ,
			)
			if created:
				n_new += 1
			else:
				n_upd += 1

		n_detached , n_deleted = 0 , 0
		if prune:
			n_detached , n_deleted = papers.prune_source(
				self.args , papers.SOURCE_ZOTERO , seen_dois ,
			)

		total = papers.count( self.args )
		print(
			f"Zotero :: snapshot -> papers/ : +{n_new} new , ~{n_upd} updated , "
			f"-{n_detached} source-detached , -{n_deleted} paper-deleted , "
			f"skipped {n_no_doi} no-doi ; total = {total}"
		)

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

	def _yolo_dir( self ):
		d = self.args.output.joinpath( "cache" , "yolo" , "zotero" )
		d.mkdir( parents=True , exist_ok=True )
		return d

	def _yolo_path_for( self , pdf_path , doi ):
		"""{normalizedDOI}-{pdf_stem}.yolo.json -- DOI guards against PDF
		filename collisions across different papers ; pdf_stem disambiguates
		multiple PDFs that share a DOI."""
		prefix = utils.doi_to_filename( doi )
		return self._yolo_dir().joinpath( f"{prefix}-{pdf_path.stem}.yolo.json" )

	def yolo( self ):
		from pathlib import Path
		from tqdm import tqdm
		from ..pdf import pdf
		papers = self.snapshot()

		jobs = []
		skip_no_doi , skip_no_pdf , skip_missing , skip_done = 0 , 0 , 0 , 0
		for key , paper in papers.items():
			doi = utils.normalize_doi( paper.get( "doi" ) )
			if not doi:
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
				if self._yolo_path_for( pdf_path , doi ).exists():
					skip_done += 1
					continue
				jobs.append( ( pdf_path , doi ) )

		print(
			f"Zotero :: YOLO -- {len(jobs)} pdfs to process "
			f"( skipped: no-doi={skip_no_doi} no-pdf={skip_no_pdf} "
			f"not-on-disk={skip_missing} already-done={skip_done} )"
		)

		outer = tqdm( jobs , desc="PDFs" , position=1 , leave=True , unit="pdf" )
		for pdf_path , doi in outer:
			outer.set_postfix_str( pdf_path.name[ :60 ] )
			yolo_path = self._yolo_path_for( pdf_path , doi )
			result = pdf.yolo( pdf_path , do_deskew=self.args.pdf_deskew )
			utils.write_json( yolo_path , result )

	# Runs yolo on the fly for any PDF that doesn't already have a yolo.json ,
	# then extracts figures/tables ( with captions ) to
	# output/images/zotero/{ALL/{normalizedDOI}-{kind}-{N}.png , {normalizedDOI}-Figures.png } .
	def images( self ):
		from pathlib import Path
		from tqdm import tqdm
		from ..pdf import pdf as pdf_mod
		from ..pdf import images as images_mod
		images_dir = self.args.output.joinpath( "images" , "zotero" )
		images_dir.mkdir( parents=True , exist_ok=True )
		all_dir = images_dir.joinpath( "ALL" )
		all_dir.mkdir( parents=True , exist_ok=True )

		# Pre-scan ALL/ once to find already-done DOI prefixes.
		done_prefixes = set()
		for f in all_dir.iterdir():
			if f.suffix != ".png":
				continue
			for marker in ( "-figure-" , "-table-" ):
				idx = f.stem.rfind( marker )
				if idx > 0:
					done_prefixes.add( f.stem[ :idx ] )
					break

		papers = self.snapshot()
		jobs = []
		skip_no_doi , skip_no_pdf , skip_missing , skip_done = 0 , 0 , 0 , 0
		needs_yolo = 0
		for key , paper in papers.items():
			doi = utils.normalize_doi( paper.get( "doi" ) )
			if not doi:
				skip_no_doi += 1
				continue
			pdfs = paper.get( "pdfs" ) or []
			if not pdfs:
				skip_no_pdf += 1
				continue
			prefix = utils.doi_to_filename( doi )
			if prefix in done_prefixes:
				# Skip the whole paper -- all of its PDFs share the same DOI prefix.
				skip_done += len( pdfs )
				continue
			for raw in pdfs:
				pdf_path = Path( raw )
				if not pdf_path.exists():
					skip_missing += 1
					continue
				yolo_path = self._yolo_path_for( pdf_path , doi )
				if not yolo_path.exists():
					needs_yolo += 1
				jobs.append( ( pdf_path , yolo_path , prefix ) )

		print(
			f"Zotero :: IMAGES -- {len(jobs)} pdfs to process -> {images_dir} "
			f"( will run YOLO inline for {needs_yolo} ; "
			f"skipped: no-doi={skip_no_doi} no-pdf={skip_no_pdf} "
			f"not-on-disk={skip_missing} already-done={skip_done} )"
		)

		include_tables = getattr( self.args , "images_include_tables" , False )
		montage        = not getattr( self.args , "images_no_montage" , False )
		size_name      = getattr( self.args , "images_montage_size" , "medium" )
		montage_scale  = images_mod.MONTAGE_SCALES.get( size_name , 1.0 )
		do_deskew      = getattr( self.args , "pdf_deskew" , False )
		outer = tqdm( jobs , desc="PDFs" , position=1 , leave=True , unit="pdf" )
		total_imgs = 0
		for pdf_path , yolo_path , prefix in outer:
			outer.set_postfix_str( pdf_path.name[ :60 ] )
			# Run yolo on the fly if we don't have a cached result yet.
			if not yolo_path.exists():
				yolo_result = pdf_mod.yolo( pdf_path , do_deskew=do_deskew )
				utils.write_json( yolo_path , yolo_result )
			n = images_mod.extract(
				pdf_path , yolo_path , images_dir , prefix ,
				include_tables=include_tables ,
				montage=montage ,
				montage_scale=montage_scale ,
			)
			total_imgs += n

		print( f"Zotero :: IMAGES -- wrote {total_imgs} cropped images" )

	# Runs yolo on the fly for any PDF that doesn't already have a yolo.json ,
	# then parses each PDF into a structured array of section blocks
	# ( title , abstract , Figure N , methods , results , ... ) and writes
	# output/text/zotero/{normalizedDOI}.json .
	def ocr( self ):
		from pathlib import Path
		from tqdm import tqdm
		from ..pdf import pdf as pdf_mod
		from ..pdf import ocr as ocr_mod
		text_dir = self.args.output.joinpath( "text" , "zotero" )
		text_dir.mkdir( parents=True , exist_ok=True )

		papers = self.snapshot()
		jobs = []
		skip_no_doi , skip_no_pdf , skip_missing , skip_done = 0 , 0 , 0 , 0
		needs_yolo = 0
		for key , paper in papers.items():
			doi = utils.normalize_doi( paper.get( "doi" ) )
			if not doi:
				skip_no_doi += 1
				continue
			pdfs = paper.get( "pdfs" ) or []
			if not pdfs:
				skip_no_pdf += 1
				continue
			prefix = utils.doi_to_filename( doi )
			out_path = text_dir.joinpath( f"{prefix}.json" )
			if out_path.exists():
				skip_done += 1
				continue
			# Pick the first available PDF on disk.
			pdf_path = None
			for raw in pdfs:
				p = Path( raw )
				if p.exists():
					pdf_path = p
					break
			if pdf_path is None:
				skip_missing += 1
				continue
			yolo_path = self._yolo_path_for( pdf_path , doi )
			if not yolo_path.exists():
				needs_yolo += 1
			jobs.append( ( pdf_path , yolo_path , out_path ) )

		print(
			f"Zotero :: OCR -- {len(jobs)} pdfs to process -> {text_dir} "
			f"( will run YOLO inline for {needs_yolo} ; "
			f"skipped: no-doi={skip_no_doi} no-pdf={skip_no_pdf} "
			f"not-on-disk={skip_missing} already-done={skip_done} )"
		)

		do_deskew = getattr( self.args , "pdf_deskew" , False )
		force_ocr = getattr( self.args , "ocr_force" , False )
		max_pages = getattr( self.args , "ocr_max_pages" , None )
		engine    = getattr( self.args , "ocr_engine" , ocr_mod.DEFAULT_ENGINE )
		lang      = getattr( self.args , "ocr_lang" , "en" )
		skip_yolo = ( engine == ocr_mod.ENGINE_MINERU )
		outer = tqdm( jobs , desc="PDFs" , position=1 , leave=True , unit="pdf" )
		total_blocks = 0
		for pdf_path , yolo_path , out_path in outer:
			outer.set_postfix_str( pdf_path.name[ :60 ] )
			if not skip_yolo and not yolo_path.exists():
				yolo_result = pdf_mod.yolo( pdf_path , do_deskew=do_deskew )
				utils.write_json( yolo_path , yolo_result )
			blocks = ocr_mod.parse(
				pdf_path , yolo_path ,
				force_ocr=force_ocr , max_pages=max_pages ,
				engine=engine , lang=lang ,
			)
			utils.write_json( out_path , blocks )
			total_blocks += len( blocks )

		print( f"Zotero :: OCR -- wrote {total_blocks} blocks across {len(jobs)} pdfs ( engine={engine} , lang={lang} )" )