import os
import re
import glob
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pprint import pprint
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..utils import utils


# Zotero's ` date ` field is free text -- "2024" , "2024-05-01" , "May 2024" ,
# "in press" . The first standalone 4-digit number in a plausible range is the
# year ; anything else has none , which the row shows as a blank.
def _year_of( raw ):
	m = re.search( r"\b( 1[6-9]\d{2} | 20\d{2} | 21\d{2} )\b" , str( raw or "" ) , re.X )
	return int( m.group( 1 ) ) if m else None


# The primary key a DOI-less Zotero item gets in output/cache/papers/ , spelled
# the way papers.synthetic_key spells it ( imported lazily : src/db imports back
# into utils , and this module is loaded by the CLI before either ) . Keeping the
# two in step is what lets /sort recognize a recently-added row it already has.
def _synthetic_key( zotero_item_key ):
	from ..db import papers
	return papers.synthetic_key( papers.SOURCE_ZOTERO , zotero_item_key )


class Zotero():
	def __init__( self , args ):
		self.args = args
		if args.zotero_sqlite:
			self.sqlite_path = Path( args.zotero_sqlite )
		else:
			self.sqlite_path = self.find_db()
		self.storage = self.sqlite_path.parent / "storage"
		# Set by _open_copy when the last read had to drop the -wal : that view
		# is behind , so it must not prune ( see _push_to_db ).
		self.read_fell_back = False

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

	# One fixed , reused location for the working copy of the DB --
	# output/cache/zotero/zotero.sqlite . Every run overwrites it in
	# place , so a crash can never strand an orphan the way the old
	# per-run tempdir did ; worst case one stale ~250 MB file sits here
	# and the next run reuses it.
	def snapshot_cache_path( self ):
		out = getattr( self.args , "output" , None ) or Path.cwd().joinpath( "output" )
		cache_dir = Path( out ).joinpath( "cache" , "zotero" )
		cache_dir.mkdir( parents=True , exist_ok=True )
		return cache_dir.joinpath( "zotero.sqlite" )

	@contextmanager
	def open_snapshot( self ):
		# Read from a copy , never the live DB , so we don't lock Zotero out.
		#
		# The copy goes to a FIXED path ( see snapshot_cache_path ) rather
		# than a fresh tempdir : nothing to leak , since each run truncates
		# the previous one. That shared path means concurrent callers ( the
		# exists server ) could otherwise read a half-written copy , so the
		# flock serializes copy+read.
		cache_db = self.snapshot_cache_path()
		conn = None
		with self._snapshot_lock( cache_db ):
			try:
				conn = self._open_copy( cache_db )
				yield conn
			finally:
				if conn is not None:
					try: conn.close()
					except Exception: pass

	def _open_copy( self , cache_db ):
		"""Copy the live DB -- WAL included -- and open the copy.

		Anything going wrong on the WAL side ( the -wal copy raising , the copy
		not answering a first query , or a copy that never settled failing its
		integrity check ) falls back to the copy this has always taken : the
		main file alone , WAL dropped. That view can only be behind by whatever
		Zotero hasn't checkpointed yet , so the worst case here is the old
		behaviour. `read_fell_back` records it , because a view that is behind
		must not be mistaken for papers having been REMOVED ( see _push_to_db's
		prune ).

		Both paths refuse a copy that never settled AND fails the check : that
		raises rather than hand back a torn library , where the old code could
		return one silently ( a Zotero checkpoint landing mid-copy )."""
		self.read_fell_back = False
		try:
			return self._settled_copy( cache_db , with_wal=True )
		except ( OSError , sqlite3.Error ) as e:
			print(
				f"Zotero :: couldn't read zotero.sqlite-wal alongside the DB ( {e} ) ; "
				f"reading the main file only -- papers saved since Zotero's last "
				f"checkpoint may not show yet"
			)
			self.read_fell_back = True
			return self._settled_copy( cache_db , with_wal=False )

	def _settled_copy( self , cache_db , with_wal ):
		settled = self._copy_live_db( cache_db , with_wal=with_wal )
		return self._connect_copy( cache_db , verify=not settled )

	def _connect_copy( self , cache_db , verify=False ):
		# Opened read-write on purpose : a -journal _copy_live_db caught hot is
		# rolled back to the last commit here , and a copied -wal is replayed
		# ( SQLite rebuilds its index from the WAL itself , keeping only
		# committed frames whose checksums and salts check out ). Both happen
		# on the first read , which the probe below forces -- so a copy that
		# won't open fails HERE , where _open_copy can still fall back.
		#
		# `verify` : the copy never settled ( see _copy_live_db ) , so pay for a
		# full integrity_check before trusting it -- ~0.13s on a 35 MB library ,
		# and only ever on that rare path , never on the steady-state one.
		conn = sqlite3.connect( cache_db )
		try:
			conn.row_factory = sqlite3.Row
			conn.execute( "SELECT count(*) FROM items" ).fetchone()
			if verify:
				ok = conn.execute( "PRAGMA integrity_check(1)" ).fetchone()[ 0 ]
				if ok != "ok":
					raise sqlite3.DatabaseError( f"copy taken mid-write fails integrity_check ( {ok} )" )
		except Exception:
			conn.close()
			raise
		return conn

	# Byte 18 of the SQLite header is the file-format write version : 1 for a
	# rollback journal , 2 for WAL. Zotero has run in both ; which one decides
	# which sidecar holds the commits a copy of the main file alone would miss.
	@staticmethod
	def _is_wal( header ):
		return len( header ) > 18 and header[ 18 ] == 2

	# SQLite stores a change counter in the file header ( bytes 24..28 ) that
	# bumps on every commit , plus a version-valid-for number ( 92..96 ).
	# Sampling those either side of a copy is an O(1) way to ask "did Zotero
	# commit while we were reading?" -- compare against PRAGMA quick_check ,
	# which costs ~0.8s on a 250 MB library and would dominate the ~0.14s
	# fast path the exists server depends on.
	#
	# In WAL mode that counter isn't reliable ( commits land in the -wal , and
	# only a checkpoint writes the main file ) , so two more things go in :
	#   - the main file's mtime : a checkpoint writing it mid-copy is what
	#     would tear our copy of it ;
	#   - the -wal's 32-byte header ( checkpoint sequence + salts ) , which is
	#     rewritten whenever the WAL restarts from the top after a checkpoint.
	#     A restart mid-copy could leave us an old frame SQLite would replay.
	# Deliberately NOT the -wal's size or mtime : plain appends ( every commit )
	# are harmless to copy mid-way. Within one WAL generation frames are only
	# ever added , so what we copied is a prefix , and SQLite drops a torn or
	# uncommitted tail by checksum. Watching appends too would never settle
	# while Zotero is busy.
	def _db_state_token( self , path ):
		try:
			with open( path , "rb" ) as f:
				header = f.read( 100 )
			if len( header ) < 100:
				return None
			st = os.stat( path )
			token = ( header[ 24:28 ] , header[ 92:96 ] , st.st_size , st.st_mtime_ns )
		except OSError:
			return None
		if self._is_wal( header ):
			try:
				with open( str( path ) + "-wal" , "rb" ) as f:
					token += ( f.read( 32 ) , )
			except OSError:
				token += ( None , )     # no -wal right now ( Zotero closed )
		return token

	def _copy_live_db( self , cache_db , attempts=6 , with_wal=True ):
		"""Byte-copy the live zotero.sqlite into the cache.

		A raw copy is the ONLY option here : Zotero holds an EXCLUSIVE lock
		on its DB while running , so sqlite3's backup API -- and even a plain
		read-only connection -- fail outright with 'database is locked'. The
		live files are only ever READ ; nothing here opens them as a database
		or touches the live -shm.

		A raw copy isn't atomic , though , so guard the ways it can lie :
		  - WAL mode ( what current Zotero runs ) : a commit lives in
		    zotero.sqlite-wal until Zotero checkpoints it into the main file ,
		    which can be an hour later. The main file alone is that far behind
		    -- a paper saved a minute ago isn't in it -- so the -wal is copied
		    alongside. `with_wal=False` skips it : the old , main-file-only
		    copy _open_copy falls back to.
		  - rollback-journal mode : a copy taken mid-transaction holds
		    uncommitted pages. Copying the -journal alongside lets SQLite roll
		    our copy back to the last commit when it's opened.
		  - if Zotero commits ( rollback mode ) or checkpoints / restarts its
		    WAL ( WAL mode ) DURING the copy , the copy can be torn AND the
		    sidecar we grabbed goes stale ( applying a stale one would itself
		    corrupt the copy ). The state token catches exactly that , so we
		    retry -- after a short , growing pause , since a checkpoint is a
		    burst that is over in milliseconds.
		Each sidecar is copied only when the header says it's the one in use ,
		so a leftover -wal beside a rollback-mode DB is never replayed.

		Returns True once a copy settled ( the token didn't move across it ) ,
		False if every attempt was interrupted -- the caller then checks the
		copy before trusting it.
		"""
		live_journal = Path( str( self.sqlite_path ) + "-journal" )
		live_wal     = Path( str( self.sqlite_path ) + "-wal" )
		for attempt in range( attempts ):
			if attempt:
				time.sleep( 0.05 * attempt )      # 50 , 100 , ... 250 ms : ~0.75s all told
			# Clear inherited sidecars first : a -wal / -journal left by an
			# earlier run would otherwise be replayed into this fresh copy.
			for sidecar in ( "-wal" , "-shm" , "-journal" ):
				Path( str( cache_db ) + sidecar ).unlink( missing_ok=True )
			before = self._db_state_token( self.sqlite_path )
			shutil.copy2( self.sqlite_path , cache_db )
			with open( cache_db , "rb" ) as f:
				wal_mode = self._is_wal( f.read( 20 ) )
			if wal_mode:
				if with_wal and live_wal.exists():
					try:
						shutil.copy2( live_wal , str( cache_db ) + "-wal" )
					except FileNotFoundError:
						pass   # Zotero closed + checkpointed mid-copy ; the token check sees it
			elif live_journal.exists():
				try:
					shutil.copy2( live_journal , str( cache_db ) + "-journal" )
				except FileNotFoundError:
					pass   # committed + deleted mid-copy ; the token check sees it
			after = self._db_state_token( self.sqlite_path )
			if before is not None and before == after:
				return True
		print(
			f"Zotero :: zotero.sqlite changed during all {attempts} copy attempts ; "
			f"proceeding with a possibly mid-write copy"
		)
		return False

	@contextmanager
	def _snapshot_lock( self , cache_db ):
		"""Serialize access to the shared cache file across processes.
		flock is POSIX-only ; on platforms without it ( Windows ) we run
		unlocked , which matches the old per-run-tempdir behavior."""
		try:
			import fcntl
		except ImportError:
			yield
			return
		lock_path = Path( str( cache_db ) + ".lock" )
		with open( lock_path , "w" ) as lock_file:
			fcntl.flock( lock_file , fcntl.LOCK_EX )
			try:
				yield
			finally:
				fcntl.flock( lock_file , fcntl.LOCK_UN )

	def take_titles_and_dois( self ):
		"""Fast path for the 'exists' server : pull ONLY title + DOI
		fields straight from the SQLite copy and return normalized
		( titles_set , dois_set ) . No creator / attachment / tag /
		collection joins ; no roundtrip through output/cache/papers/ .
		Typical run is ~50-200 ms for a 2000-paper library vs ~10-30
		seconds for take_snapshot() + _push_to_db()."""
		with self.open_snapshot() as conn:
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

	# The N papers most recently added to Zotero , read straight off the live
	# SQLite -- NOT off output/cache/papers/ and NOT off the dashboard index.
	# That is the whole point : a paper saved into Zotero thirty seconds ago is
	# in neither of those until a snapshot + reindex has run , so /sort could
	# not offer it without either a restart or a full pipeline pass. This path
	# sees it immediately.
	#
	# Same fast shape as take_titles_and_dois : one ordered scan of items for
	# the newest keys , then the field / creator rows for JUST those items.
	# Scoped to `limit` , so it stays a few milliseconds on top of the SQLite
	# copy however large the library is.
	def take_recent( self , limit=10 ):
		limit = max( 1 , min( int( limit or 10 ) , 200 ) )
		# Over-fetch : the newest rows include items we drop below ( no title
		# and no DOI -- a bare web link , an empty placeholder ) , and dropping
		# them must not shorten the answer.
		scan = min( limit * 4 + 20 , 500 )
		with self.open_snapshot() as conn:
			c = conn.cursor()
			order , ids = [] , {}
			for row in c.execute( """
				SELECT items.itemID , items.key , items.dateAdded
				FROM items
				JOIN itemTypes       ON itemTypes.itemTypeID  = items.itemTypeID
				LEFT JOIN deletedItems ON deletedItems.itemID = items.itemID
				WHERE deletedItems.itemID IS NULL
				  AND itemTypes.typeName NOT IN ( 'attachment' , 'note' , 'annotation' )
				ORDER BY items.dateAdded DESC
				LIMIT ?
			""" , ( scan , ) ):
				order.append( row[ "itemID" ] )
				ids[ row[ "itemID" ] ] = {
					"zkey":  row[ "key" ] ,
					"added": ( row[ "dateAdded" ] or "" ) ,
					"meta":  {} ,
					"authors": [] ,
				}
			if not order:
				return []

			marks = " , ".join( "?" * len( order ) )
			for row in c.execute( f"""
				SELECT itemData.itemID , fields.fieldName , itemDataValues.value
				FROM itemData
				JOIN fields         ON fields.fieldID         = itemData.fieldID
				JOIN itemDataValues ON itemDataValues.valueID = itemData.valueID
				WHERE itemData.itemID IN ( {marks} )
			""" , order ):
				ids[ row[ "itemID" ] ][ "meta" ][ row[ "fieldName" ] ] = row[ "value" ]

			for row in c.execute( f"""
				SELECT itemCreators.itemID , creators.firstName , creators.lastName
				FROM itemCreators
				JOIN creators     ON creators.creatorID         = itemCreators.creatorID
				JOIN creatorTypes ON creatorTypes.creatorTypeID = itemCreators.creatorTypeID
				WHERE itemCreators.itemID IN ( {marks} )
				  AND creatorTypes.creatorType = 'author'
				ORDER BY itemCreators.itemID , itemCreators.orderIndex
			""" , order ):
				name = " ".join( x for x in ( row[ "firstName" ] , row[ "lastName" ] ) if x )
				if name:
					ids[ row[ "itemID" ] ][ "authors" ].append( name )

		out = []
		for item_id in order:
			it    = ids[ item_id ]
			meta  = it[ "meta" ]
			doi   = utils.normalize_doi( meta.get( "DOI" ) )
			title = ( meta.get( "title" ) or "" ).strip()
			if not title and not doi:
				continue     # nothing to show and nothing to key on
			out.append( {
				"key":     doi or _synthetic_key( it[ "zkey" ] or item_id ) ,
				"zkey":    it[ "zkey" ] or "" ,
				"title":   title or doi ,
				"doi":     doi or "" ,
				"year":    _year_of( meta.get( "date" ) ) ,
				"journal": ( meta.get( "publicationTitle" ) or meta.get( "bookTitle" )
					or meta.get( "proceedingsTitle" ) or "" ) ,
				"authors": it[ "authors" ] ,
				"added":   it[ "added" ] ,
			} )
			if len( out ) >= limit:
				break
		return out

	def take_snapshot( self ):
		with self.open_snapshot() as conn:
			return self._read_papers( conn )

	def _read_papers( self , conn ):
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
		if prune and self.read_fell_back:
			# This snapshot is the main file alone ( see _open_copy ) , so it is
			# missing whatever Zotero hasn't checkpointed yet. A paper added
			# since then isn't GONE , it's just not in this view -- pruning off it
			# would delete that paper here and re-add ( and re-process ) it on
			# the next full read.
			print( "Zotero :: snapshot read without the -wal ; skipping the prune this time" )
			prune = False
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
