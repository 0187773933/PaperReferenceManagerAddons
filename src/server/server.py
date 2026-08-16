#!/usr/bin/env python3
"""
HTTP "exists" server.

Exposes the snapshot ( titles + DOIs ) over a tiny POST /exists endpoint so
browser userscripts / external tools can ask "do I already have this paper".

Two entry points share this module:
  - prma server ( wired through src/cli/cli.py + src/tasks/tasks.py )
  - python server.py ( root-level shim , kept for the existing .bat files in ./windows-scripts/ )

Both end up calling run( args ). The args namespace must have:
    manager , zotero , mendeley , output , config ,
    mendeley_source , mendeley_sqlite , zotero_sqlite ,
    host , port , debounce , ttl

Beyond that endpoint the same server hosts the dashboard , the figure reports and
( with --watch ) the live per-paper worker. Set exists_only=True ( ` prma --exists ` )
for MINIMAL mode : the userscript surface only -- POST /exists , GET /api/version
and POST /refresh -- with none of the rest built or started.
"""

import os
import re
import sys
import html
import json
import time
import shutil
import contextlib
import mimetypes
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler , HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse , parse_qs , quote , unquote
from typing import Dict , List , Optional , Set , Tuple

from rapidfuzz import fuzz , process

from ..utils import utils
from ..tasks import snapshot as snap_module

# The dashboard's single-page UI lives next to the dashboard data layer so
# it's easy to edit by hand. Served verbatim at GET / .
DASHBOARD_HTML_PATH = Path( __file__ ).resolve().parent.parent.joinpath(
	"dashboard" , "dashboard.html" )

# Companion page that renders the unified problems log ( see
# src/db/papers.py ) -- everything the pipeline couldn't fully process.
# Served at GET /errors ; its data comes from GET /api/errors .
ERRORS_HTML_PATH = Path( __file__ ).resolve().parent.parent.joinpath(
	"dashboard" , "errors.html" )

# Companion page that renders the pipeline completeness tally written by
# ` prma status ` ( output/cache/status.json ). Served at GET /status ;
# its data comes from GET /api/status .
STATUS_HTML_PATH = Path( __file__ ).resolve().parent.parent.joinpath(
	"dashboard" , "status.html" )

# Companion page that RENDERS one paper's ` prma md ` document -- headings ,
# figures and all -- in the browser. Served at GET /md/<doi> ; its sections come
# from GET /api/paper?key=<doi> , the same payload the dashboard's inline "Read"
# accordion draws. ?raw=1 still hands back the Markdown file itself.
MD_HTML_PATH = Path( __file__ ).resolve().parent.parent.joinpath(
	"dashboard" , "md.html" )

# The hand-curation surface : a drag-and-drop TIER LIST of the papers you've
# decided are the premier references , with modality / tag / notes columns and
# CSV in-out. Served at GET /tiers ; its document lives server-side ( see
# src/db/tiers.py ) behind GET / POST /api/tiers .
TIERS_HTML_PATH = Path( __file__ ).resolve().parent.parent.joinpath(
	"dashboard" , "tiers.html" )

# Its sibling , and the one built for this library's actual question : the same
# curation surface , but the sections ARE tag sets ( "fMRI" , "EEG" , "fMRI EEG"
# for the studies that ran both ) , so where a paper sits and what it's tagged
# are the same fact. Served at GET /sort ; document behind GET / POST /api/sort .
SORT_HTML_PATH = Path( __file__ ).resolve().parent.parent.joinpath(
	"dashboard" , "sort.html" )


class _RequestLog:
	"""Thread-safe ring of recent /exists queries + running totals, read by the
	opt-in TUI. Request threads append ( a deque append under a lock -- no I/O ,
	no processing touched ). Harmless and effectively free when the TUI is off."""

	def __init__( self , maxlen=200 ):
		self._lock  = threading.Lock()
		self._rows  = deque( maxlen=maxlen )   # (ts, exists, title, doi) newest-first
		self.served = 0
		self.hits   = 0

	def add( self , cleaned , results ):
		exists_by_id = { r.get( "id" ): r.get( "exists" ) for r in results }
		ts = time.strftime( "%H:%M:%S" )
		with self._lock:
			self.served += len( results )
			for q in cleaned:
				ex = bool( exists_by_id.get( q.get( "id" ) ) )
				if ex:
					self.hits += 1
				title = ( q.get( "title" ) or "" ).strip()
				doi   = ( q.get( "doi" )   or "" ).strip()
				self._rows.appendleft( ( ts , ex , title[ :200 ] , doi[ :100 ] ) )

	def snapshot( self ):
		with self._lock:
			return { "served": self.served , "hits": self.hits ,
			         "rows": list( self._rows ) }


# Module-level so the request handler ( Handler.do_POST ) and the TUI share one.
REQUEST_LOG = _RequestLog()


class DashboardData:
	"""
	The full-text-searchable dashboard's in-memory index, served from the
	SAME process as the lightweight /exists endpoint.

	The index is HEAVY to build ( snapshot + OpenAlex cache update + the
	missing computation -- minutes on a large library ) , so it is NOT
	built at server startup. The /exists endpoint stays instantly
	available ; the dashboard index builds LAZILY in a background daemon
	thread the first time someone opens the dashboard ( the page's
	/api/meta poll triggers it ) , and again on demand via POST /api/refresh.

	State machine ( self.status ) :
	  idle      -> never built ; ensure_build() kicks off a thread
	  building  -> a build thread is running
	  ready     -> pools are populated and queryable
	  error     -> last build raised ; message in self.error
	"""

	def __init__( self , args ):
		self.args        = args
		self._data_lock  = threading.Lock()   # guards the pool snapshots
		self._build_lock = threading.Lock()   # ensures a single builder
		self.status      = "idle"
		self.error       = ""
		self.message     = ""
		self.references  = []
		self.cited_by    = []
		self.authors     = []
		self.library     = []
		self.built_at    = None
		self.lib_count   = 0
		self.missing_count = 0
		self._loaded_mtime = 0.0   # mtime of the on-disk index we last loaded
		# Skip set : dashboard row keys the user has hidden ( the 'Skip' button ).
		# Independent of the index ; loaded once and kept in memory , persisted on
		# every change. Copy-on-write ( see set_skip ) so search readers never
		# iterate a mutating set.
		self.skipped     = set()
		self._skip_lock  = threading.Lock()

	def ensure_build( self , refresh=False ):
		"""Start a background build if one isn't already running. Non-blocking ,
		idempotent. refresh=False indexes the existing on-disk cache ( the
		lazy first-open path -- fast , no network ) ; refresh=True first
		downloads any new OpenAlex data ( the Rebuild button / ` prma reindex `
		path )."""
		with self._build_lock:
			if self.status == "building":
				return
			self.status  = "building"
			self.message = "Starting…"
		t = threading.Thread( target=self._build , args=( refresh , ) , daemon=True )
		t.start()

	def _build( self , refresh ):
		try:
			self._rebuild( refresh )
			with self._build_lock:
				self.status = "ready"
				self.error  = ""
		except Exception as e:
			print( f"dashboard :: index build failed ( {e} )" )
			with self._build_lock:
				self.status = "error"
				self.error  = str( e )

	def _rebuild( self , refresh ):
		"""(Re)build the dashboard's own index ( see src/dashboard/indexer.py )
		and swap the new pools in. refresh=True also downloads fresh OpenAlex
		data first. The indexer streams progress strings back through the
		callback so a long build shows live status instead of looking stalled ,
		and persists to disk ( we stamp the mtime so maybe_reload() doesn't
		reload our own write )."""
		from ..dashboard import indexer

		def prog( msg ):
			self.message = msg

		full = getattr( self.args , "reindex_full" , False )
		if refresh:
			pools = indexer.reindex( self.args , full=full , progress=prog )
		else:
			pools = indexer.build( self.args , full=full , progress=prog )
		self._apply_pools( pools )
		self._loaded_mtime = indexer.state_mtime( self.args )

	def _apply_pools( self , pools ):
		with self._data_lock:
			self.references    = pools.get( "references" ) or []
			self.cited_by      = pools.get( "cited_by" )   or []
			self.authors       = pools.get( "authors" )    or []
			self.library       = pools.get( "library" )    or []
			self.built_at      = pools.get( "built_at" )
			self.lib_count     = pools.get( "lib_count" , 0 )
			self.missing_count = pools.get( "missing_count" , len( self.references ) )
		self._start_text_fold()

	def _start_text_fold( self ):
		"""Fold every pool's searchable text -- accents stripped , punctuation
		flattened , DOI / key / journal folded in ( see index.prepare_rows ) --
		so a colon , a slash or an umlaut can't hide a paper from search.

		In the BACKGROUND : the library's haystacks carry each paper's whole OCR
		body , ~20s of string work across a big library , and neither startup nor
		the first query should wait on it. A search that lands mid-fold is still
		correct -- index.search folds whatever rows it meets unfolded , the pass
		is per row and idempotent -- it just pays for those rows itself."""
		pools = ( self.references , self.cited_by , self.library )

		def work():
			from ..dashboard import index as dash_index
			for pool in pools:
				try:
					dash_index.prepare_rows( pool )
				except Exception as e:
					print( f"dashboard :: search text fold failed ( {e} )" )

		threading.Thread( target=work , daemon=True , name="prma-search-fold" ).start()

	def load_from_disk( self ):
		"""Populate pools from a previously persisted index, if one exists.
		Returns True on a hit. Lets the server come up instantly serving the
		last ` prma reindex ` result instead of building on startup."""
		from ..dashboard import indexer
		pools = indexer.load_pools( self.args , getattr( self.args , "top_author_count" , 100 ) )
		if not pools:
			return False
		self._apply_pools( pools )
		self._loaded_mtime = indexer.state_mtime( self.args )
		with self._build_lock:
			self.status = "ready"
		return True

	def maybe_reload( self ):
		"""If a ` prma reindex ` ( in another process ) wrote a newer index
		file since we last loaded, pick it up. Cheap mtime check ; only
		reads the file when it actually changed. Never interrupts an
		in-process build."""
		from ..dashboard import indexer
		if self.status == "building":
			return
		m = indexer.state_mtime( self.args )
		if m and m > self._loaded_mtime:
			print( "dashboard :: detected fresher on-disk index ; reloading" )
			self.load_from_disk()

	def rebuild_after_process( self ):
		"""Rebuild the index off disk ( no network ) after the live --watch
		worker processed new paper(s) , and swap the fresh pools in WITHOUT
		flipping the dashboard into the 'building' state -- a user mid-search
		isn't interrupted ; the new rows just show up on their next query.
		The build is incremental ( only the changed papers + their new refs )
		so this is cheap. Errors are logged , never raised.

		If a manual build happens to be running ( status 'building' ) we leave
		its state alone ; otherwise we mark 'ready' so the fresh pools are
		immediately queryable even if nobody has opened the dashboard yet."""
		from ..dashboard import indexer
		try:
			pools = indexer.build( self.args )
			self._apply_pools( pools )
			self._loaded_mtime = indexer.state_mtime( self.args )
			with self._build_lock:
				if self.status != "building":
					self.status = "ready"
					self.error  = ""
		except Exception as e:
			print( f"dashboard :: post-process reindex failed ( {e} )" )

	def meta( self ):
		with self._data_lock:
			return {
				"status":        self.status ,
				"message":       self.message ,
				"error":         self.error ,
				"have":          self.lib_count ,        # papers you already have
				"references":    len( self.references ) , # missing : you cite them
				"cited_by":      len( self.cited_by ) ,   # missing : they cite you
				"authors":       len( self.authors ) ,
				"library":       len( self.library ) ,    # searchable : papers you HAVE
				"built_at":      self.built_at ,
				"manager":       getattr( self.args , "manager" , "" ) ,
			}

	def _pool( self , name ):
		if name == "references":
			return self.references
		if name == "cited_by":
			return self.cited_by
		if name == "library":
			return self.library
		if name == "external":
			# Everything you don't have : works you cite + works citing you ,
			# deduped by WID ( a work can be on both sides ).
			seen , out = set() , []
			for r in self.references + self.cited_by:
				if r[ "wid" ] in seen:
					continue
				seen.add( r[ "wid" ] )
				out.append( r )
			return out
		return []

	def load_skips( self ):
		"""Populate the in-memory skip set from disk ( call at startup )."""
		from ..db import skips
		try:
			self.skipped = skips.load( self.args )
		except Exception as e:
			print( f"dashboard :: could not load skip list ( {e} )" )
			self.skipped = set()

	def set_skip( self , key , skipped ):
		"""Record ( skipped=True ) or clear ( False ) a row's skipped state and
		persist it. Copy-on-write : rebind self.skipped to a NEW set so a
		concurrent search() reader holding the old reference never iterates a
		mutating set. Returns the new boolean state."""
		from ..db import skips
		key = ( key or "" ).strip()
		if not key:
			return False
		with self._skip_lock:
			s = set( self.skipped )
			if skipped: s.add( key )
			else:       s.discard( key )
			self.skipped = s
			try:
				skips.save( self.args , s )
			except Exception as e:
				print( f"dashboard :: could not persist skip list ( {e} )" )
		return bool( skipped )

	def search( self , query , pool , sort , limit , offset=0 , direction=None ,
			hide_skipped=True , mode="text" ):
		"""mode="text" : the dashboard's boolean full-text search.
		mode="title"   : the /sort + /tiers boards' fuzzy TITLE lookup -- type
		part of a title , get the closest titles back , best first ( see
		dashboard/index.py :: title_search ). `sort` / `direction` don't apply
		there : the order IS the closeness."""
		from ..dashboard import index as dash_index
		skipped = self.skipped   # copy-on-write set : safe to read lock-free
		with self._data_lock:
			if self.status != "ready":
				return { "status": self.status , "pool": pool , "total": 0 ,
					"shown": 0 , "offset": offset , "results": [] }
			rows = self._pool( pool )
			# Drop skipped rows BEFORE sort + paging so total / offset stay
			# correct ( the data is still in the pool -- this is just the view ).
			if hide_skipped and skipped:
				rows = [ r for r in rows if r.get( "key" ) not in skipped ]
			if mode == "title":
				total , page , fuzzy = dash_index.title_search(
					rows , query , limit=limit , offset=offset )
			else:
				total , page , fuzzy = dash_index.search(
					rows , query , sort=sort , limit=limit ,
					offset=offset , direction=direction )
		results = []
		for r in page:
			pub = dash_index.to_public( r )
			pub[ "skipped" ] = r.get( "key" ) in skipped
			results.append( pub )
		return {
			"status":  "ready" ,
			"pool":    pool ,
			"mode":    mode ,
			"total":   total ,
			"offset":  offset ,
			"shown":   len( page ) ,
			# True -> the query matched nothing and these are near-miss titles ,
			# so the page can say so instead of pretending they're hits.
			"fuzzy":   fuzzy ,
			"results": results ,
		}

	def author_table( self , limit ):
		with self._data_lock:
			if self.status != "ready":
				return { "status": self.status , "total": 0 , "results": [] }
			rows = self.authors[ : max( 0 , limit ) ]
			return { "status": "ready" , "total": len( self.authors ) ,
				"results": list( rows ) }


class FigureSelectionState:
	"""In-memory holder for ONE figure report's curation -- the FIGURES the user
	picked ( selected ) and the PAPERS they skipped -- persisted via
	src/db/figure_state.py so it follows the user across browsers and survives a
	report rebuild ( the page used to keep this in localStorage , which broke on a
	browser switch ).

	There is one instance PER REPORT , each bound to its own collection :
	  "method-images"  /method-images  ( the keyword-matched design figures )
	  "images"         /images         ( every cropped figure )
	They are entirely independent -- picking a figure on one does not pick it on
	the other. /images does READ the method-images collection to badge figures
	already curated over there , but that read goes through the same
	/api/method-images/state route and never writes.

	Mirrors DashboardData's skip handling : loaded once at startup , mutated one
	id at a time from the page ( so two browsers editing at once don't clobber
	each other's whole set ) , and persisted on every change. Guarded by a lock ;
	each write is a tiny JSON dump.

	`selected` is an ordered LIST -- insertion order is the pick order the page's
	"Last pick" relies on , and re-picking a figure moves its id to the END , same
	as the page's own toggle. `skipped` is a set ; order is irrelevant."""

	def __init__( self , args , collection ):
		self.args       = args
		self.collection = collection
		self._lock      = threading.Lock()
		self.selected   = []       # ordered : insertion order IS pick order
		self.skipped    = set()

	def load( self ):
		"""Populate from disk ( call at startup ). Never raises."""
		from ..db import figure_state
		try:
			sel , skip = figure_state.load( self.args , self.collection )
			with self._lock:
				self.selected = list( sel )
				self.skipped  = set( skip )
		except Exception as e:
			print( f"{self.collection} :: could not load curation state ( {e} )" )

	def snapshot( self ):
		with self._lock:
			return { "selected": list( self.selected ) ,
			         "skipped":  sorted( self.skipped ) }

	def _persist_locked( self ):
		from ..db import figure_state
		try:
			figure_state.save( self.args , self.selected , self.skipped , self.collection )
		except Exception as e:
			print( f"{self.collection} :: could not persist curation state ( {e} )" )

	def set_selected( self , fid , on ):
		"""Add ( on=True ) or remove a figure id. Re-adding moves it to the END so
		the page's "Last pick" stays truthful."""
		fid = ( fid or "" ).strip()
		if not fid:
			return
		with self._lock:
			self.selected = [ s for s in self.selected if s != fid ]
			if on:
				self.selected.append( fid )
			self._persist_locked()

	def set_skipped( self , key , on ):
		"""Skip ( on=True ) or un-skip a paper key."""
		key = ( key or "" ).strip()
		if not key:
			return
		with self._lock:
			s = set( self.skipped )
			if on: s.add( key )
			else:  s.discard( key )
			self.skipped = s
			self._persist_locked()


class BoardState:
	"""In-memory holder for ONE hand-curated board document -- the tier list
	( src/db/tiers.py , /tiers ) or the tag-sectioned sort board
	( src/db/sortboard.py , /sort ). Both are the same kind of thing to the
	server : one JSON document the page owns.

	Unlike the figure curation next door -- which is toggled one id at a time --
	a board page owns a whole DOCUMENT ( group order , per-row tag / notes cells ,
	the column set ) , so it posts the document back whole and this replaces it
	wholesale under a lock. `rev` bumps on every accepted write ; the page polls
	it ( GET /api/<board>/version ) and reloads when a SECOND tab moved the
	document underneath it , which is the only concurrency this ever sees.

	`store` is the src/db module that owns the file -- load / save / default_doc
	is the whole interface , which is why a second board cost a module and one
	more instance rather than a second copy of any of this."""

	def __init__( self , args , store , name ):
		self.args  = args
		self.store = store
		self.name  = name
		self._lock = threading.Lock()
		self.doc   = None
		self.rev   = 0

	def load( self ):
		"""Populate from disk ( call at startup ). Never raises."""
		try:
			with self._lock:
				self.doc = self.store.load( self.args )
		except Exception as e:
			print( f"{self.name} :: could not load the board ( {e} )" )
			self.doc = None

	def snapshot( self ):
		with self._lock:
			doc = self.doc if self.doc is not None else self.store.default_doc()
			return { "rev": self.rev , "doc": doc }

	def replace( self , doc ):
		"""Write a whole document ( the page's save ). Returns the normalized
		document as stored , with the new rev."""
		with self._lock:
			self.doc = self.store.save( self.args , doc )
			self.rev += 1
			return { "rev": self.rev , "doc": self.doc }


class PaperMeta:
	"""The per-row lookup both board pages ask for : which links a paper can
	offer ( PDF / figures / MD / Methods ) and -- on request -- the modality
	stamp ` prma modalities ` already pinned on it , which is what lets a freshly
	added row arrive pre-tagged fMRI / EEG instead of blank ( and , on /sort ,
	drop straight into the section that says so ).

	Everything but the stamp comes off the in-memory dashboard index and is
	free ; the stamp costs one paper-record read -- a few hundred KB -- so it is
	memoized per key against the record's mtime and only read when the page
	actually asks for modalities. One instance , shared by every board."""

	def __init__( self , args , dash ):
		self.args    = args
		self.dash    = dash
		self._mods   = {}      # paper key -> ( mtime , used[] , inferred , stale )
		self._lib    = None    # paper key -> library row , rebuilt when the index
		self._lib_at = None    #              is ( keyed on dash.built_at )

	def _lib_index( self ):
		"""key -> library row , off the dashboard's in-memory pool. Rebuilt only
		when the index itself was rebuilt."""
		built = getattr( self.dash , "built_at" , None )
		if self._lib is None or self._lib_at != built:
			rows = getattr( self.dash , "library" , None ) or []
			self._lib    = { r.get( "key" ) : r for r in rows if r.get( "key" ) }
			self._lib_at = built
		return self._lib

	def _modalities( self , key ):
		"""( used , inferred , stale ) for one paper from the ` prma modalities `
		stamp. stale = the stamp was minted under a different methods.py
		vocabulary , so it is shown but flagged. ( [] , False , False ) when the
		paper has no record or was never stamped."""
		from ..db    import papers as papers_db
		from ..tasks import modalities as modalities_task
		try:
			p     = papers_db.paper_path( self.args , key )
			mtime = p.stat().st_mtime if p.exists() else 0.0
		except Exception:
			return [] , False , False
		if not mtime:
			return [] , False , False
		hit = self._mods.get( key )
		if hit and hit[ 0 ] == mtime:
			return hit[ 1 ] , hit[ 2 ] , hit[ 3 ]
		used , inferred , stale = [] , False , False
		try:
			paper = papers_db.load( self.args , key ) or {}
			rec   = modalities_task.read( self.args , paper )
			if rec is None:
				# Never stamped , or stamped under an older vocabulary. Show the
				# old answer rather than nothing , flagged so the page can say so.
				raw   = paper.get( modalities_task.STAMP_KEY )
				rec   = raw if isinstance( raw , dict ) else {}
				stale = bool( rec )
			used     = [ m for m in ( rec.get( "used" ) or [] ) if isinstance( m , str ) ]
			inferred = bool( rec.get( "inferred" ) )
		except Exception:
			pass
		self._mods[ key ] = ( mtime , used , inferred , stale )
		return used , inferred , stale

	def meta( self , keys , want_mods=False ):
		"""What a page needs to draw a row it only knows the KEY of : the library
		identity ( title / DOI / year ) , which links exist for it , and
		optionally the modality stamp. Keys that aren't library papers come back
		with in_library=false and nothing else -- the page already holds their
		title / DOI from the search hit that added them."""
		lib , out = self._lib_index() , {}
		for key in ( keys or [] )[ :500 ]:
			if not isinstance( key , str ) or not key:
				continue
			row   = lib.get( key ) or {}
			entry = {
				"in_library": bool( row ) ,
				"title":      row.get( "title" ) or "" ,
				"doi":        row.get( "doi" ) or "" ,
				"year":       row.get( "year" ) ,
				"wid":        row.get( "wid" ) or "" ,
				"cited_by":   row.get( "cited_by" ) ,
				"pdf":        bool( row.get( "pdf" ) ) ,
				"montage":    row.get( "montage" ) or "" ,
				"has_md":     bool( row.get( "has_md" ) ) ,
				"methods":    "" ,
			}
			prefix = utils.doi_to_filename( key ) or ""
			if prefix:
				try:
					if self.args.output.joinpath( "methods" , f"{prefix}.txt" ).exists():
						entry[ "methods" ] = f"/methods/{quote( prefix )}.txt"
				except Exception:
					pass
			if want_mods:
				used , inferred , stale = self._modalities( key )
				entry[ "mods" ]     = used
				entry[ "inferred" ] = inferred
				entry[ "stale" ]    = stale
			out[ key ] = entry
		return out


# The two figure reports , by mode name ( == figure_state collection == the
# /api/<mode>/… prefix the page fetches ). Both modules expose the same three
# entry points -- report_path / report_is_stale / rebuild -- which is all the
# server ever needs from them , so every route below is written once against
# the pair rather than twice against each.
FIGURE_REPORTS = ( "method-images" , "images" )


def _report_module( mode ):
	from ..tasks import all_images , method_images
	return all_images if mode == "images" else method_images


def _figure_mode( path , suffix ):
	"""'/api/images/state' -> 'images' , for the routes shared by both reports.
	Returns None when `path` isn't one of them."""
	for mode in FIGURE_REPORTS:
		if path == f"/api/{mode}/{suffix}":
			return mode
	return None


def _load_dashboard_html():
	"""Read the dashboard SPA fresh on each request so hand-edits to
	dashboard.html show up on a browser reload without restarting the
	server. Falls back to a stub if the file is missing."""
	try:
		return DASHBOARD_HTML_PATH.read_text( encoding="utf-8" )
	except Exception as e:
		return f"<h1>dashboard.html not found</h1><pre>{e}</pre>"


def _load_errors_html():
	"""Read the errors page fresh on each request ( same hand-edit-friendly
	pattern as the dashboard ). Falls back to a stub if missing."""
	try:
		return ERRORS_HTML_PATH.read_text( encoding="utf-8" )
	except Exception as e:
		return f"<h1>errors.html not found</h1><pre>{e}</pre>"


def _load_status_html():
	"""Read the status page fresh on each request. Falls back to a stub."""
	try:
		return STATUS_HTML_PATH.read_text( encoding="utf-8" )
	except Exception as e:
		return f"<h1>status.html not found</h1><pre>{e}</pre>"


def _load_md_html():
	"""Read the Markdown reader page fresh on each request. Falls back to a stub."""
	try:
		return MD_HTML_PATH.read_text( encoding="utf-8" )
	except Exception as e:
		return f"<h1>md.html not found</h1><pre>{e}</pre>"


def _load_tiers_html():
	"""Read the tier-list page fresh on each request. Falls back to a stub."""
	try:
		return TIERS_HTML_PATH.read_text( encoding="utf-8" )
	except Exception as e:
		return f"<h1>tiers.html not found</h1><pre>{e}</pre>"


def _load_sort_html():
	"""Read the sort-board page fresh on each request. Falls back to a stub."""
	try:
		return SORT_HTML_PATH.read_text( encoding="utf-8" )
	except Exception as e:
		return f"<h1>sort.html not found</h1><pre>{e}</pre>"


# Serializes status recomputes so a flurry of /status loads can't kick off
# several full library walks at once ; they share the one in-flight result.
_STATUS_LOCK = threading.Lock()


def _md_inline( s ):
	"""Inline Markdown -> HTML for one block : images , links , bold. Text is
	HTML-escaped first ; the markdown delimiters ( ! [ ] ( ) * ) survive
	escaping so the regexes still match. Image regex runs before the link
	regex so '![alt](src)' isn't mis-parsed as a link."""
	s = html.escape( s )
	s = re.sub( r"!\[([^\]]*)\]\(([^)]+)\)" ,
		r'<img alt="\1" src="\2" loading="lazy">' , s )
	s = re.sub( r"\[([^\]]+)\]\(([^)]+)\)" ,
		r'<a href="\2" target="_blank">\1</a>' , s )
	s = re.sub( r"\*\*([^*]+)\*\*" , r"<strong>\1</strong>" , s )
	return s


def _md_to_html( md ):
	"""Minimal Markdown -> HTML covering exactly what ` prma md ` emits :
	'### ' subsection headings , **bold** captions , ![img](src) figures ,
	[text](url) links , '- ' bullet lists ( the Source Code section ) , and
	blank-line-separated paragraphs."""
	out = []
	for block in re.split( r"\n\s*\n" , md ):
		block = block.strip( "\n" )
		if not block.strip():
			continue
		stripped = block.strip()
		m = re.match( r"^#{3,6}\s+(.*)$" , stripped )
		lines = [ ln for ln in block.split( "\n" ) if ln.strip() ]
		if m and "\n" not in stripped:
			out.append( f"<h4>{_md_inline( m.group( 1 ) )}</h4>" )
		elif lines and all( ln.lstrip().startswith( "- " ) for ln in lines ):
			items = "".join(
				f"<li>{_md_inline( ln.lstrip()[ 2: ] )}</li>" for ln in lines )
			out.append( f"<ul>{items}</ul>" )
		else:
			out.append( f"<p>{_md_inline( block ).replace( chr( 10 ) , '<br>' )}</p>" )
	return "\n".join( out )


def _md_slug( ident ):
	"""Filename stem of a paper's ` prma md ` render : its DOI with the path
	separators folded to '_' ( what utils.doi_to_filename writes ) , tolerating an
	identifier that already carries the '.md' suffix -- so the DOI form the
	dashboard's MD button uses ( /md/10.1038/foo ) and the file form the figure
	reports link ( ../md/10.1038_foo.md ) both land on the same document.
	Returns None for anything that could climb out of output/md/ ."""
	s = ( ident or "" ).strip().strip( "/" )
	if s.lower().endswith( ".md" ):
		s = s[ :-3 ]
	s = s.replace( "\\" , "_" ).replace( "/" , "_" )
	if not s or s.startswith( "." ) or "\x00" in s:
		return None
	return s


def _paper_md_payload( args , key ):
	"""Split a library paper's ` prma md ` render into per-section HTML -- drawn
	both by the dashboard's inline 'Read' accordion and by the standalone reader
	page at /md/<doi> . Relative image links ( ../images/... ) are rewritten to
	the server's /images/ route so figures resolve. Sections split on the
	'## ' headers the md renderer emits ; the '# Title' line becomes the page
	title , anything before the first '## ' becomes an 'Overview' section."""
	from ..db import papers as papers_db
	paper  = papers_db.load( args , key )
	rk     = papers_db.record_key( paper ) if paper else key
	prefix = utils.doi_to_filename( rk ) if rk else None
	md_dir  = args.output.joinpath( "md" )
	md_path = md_dir.joinpath( f"{prefix}.md" ) if prefix else None
	if not ( md_path and md_path.exists() ):
		# Not a known record key -> read the identifier as the md file's own name
		# ( what the figure reports link ) rather than as a paper key.
		slug    = _md_slug( key )
		md_path = md_dir.joinpath( f"{slug}.md" ) if slug else None
	if not ( md_path and md_path.exists() ):
		return { "available": False }
	try:
		text = md_path.read_text( encoding="utf-8" )
	except Exception as e:
		return { "available": False , "error": str( e ) }
	text = text.replace( "](../images/" , "](/images/" )

	title , raw = ( paper or {} ).get( "title" ) or "" , []
	cur , buf = None , []
	for line in text.splitlines():
		if line.startswith( "## " ):
			if cur is not None or any( s.strip() for s in buf ):
				raw.append( ( cur , buf ) )
			cur , buf = line[ 3: ].strip() , []
		elif line.startswith( "# " ) and cur is None:
			# The document's own '# Title' line is the HEADING , not body text , so
			# it's always consumed here -- otherwise it prints again inside
			# 'Overview' , under the title the reader page already shows. The
			# record's title wins when we have one ; this is the fallback.
			title = title or line[ 2: ].strip()
		else:
			buf.append( line )
	if cur is not None or any( s.strip() for s in buf ):
		raw.append( ( cur , buf ) )

	sections = [
		{ "title": ( t or "Overview" ) , "html": _md_to_html( "\n".join( b ) ) }
		for t , b in raw
	]
	return { "available": True , "key": key , "title": title ,
		# The reader page's DOI / Proxy buttons. Empty when the identifier didn't
		# resolve to a record ( a bare filename ) -- the page just omits them.
		"doi": utils.normalize_doi( ( paper or {} ).get( "doi" ) ) or "" ,
		"sections": sections }


def _status_payload( args , regen=False ):
	"""Return the pipeline-completeness tally for the /status page.

	regen=True ( what opening /status does ) recomputes the tally fresh from
	the current on-disk DB and re-persists output/cache/status.json , so the
	page always reflects whatever pipeline tasks have produced. It does NOT
	take a manager snapshot / hit OpenAlex -- that network refresh stays with
	the ` prma status ` CLI ; the page just re-tallies what's on disk.

	regen=False just reads the last persisted status.json ( or returns
	available: False if ` prma status ` was never run )."""
	from ..tasks import status as status_task
	if regen:
		with _STATUS_LOCK:
			try:
				data = status_task.compute( args )
			except Exception as e:
				return { "available": False , "error": str( e ) }
			try:
				p = status_task.status_path( args )
				p.parent.mkdir( parents=True , exist_ok=True )
				utils.write_json( p , data )
			except Exception:
				pass   # serving the fresh tally matters more than persisting it
			data[ "available" ] = True
			return data

	p = status_task.status_path( args )
	if not p.exists():
		return { "available": False }
	try:
		data = utils.read_json( p )
	except Exception as e:
		return { "available": False , "error": str( e ) }
	data[ "available" ] = True
	return data


def _errors_payload( args ):
	"""Shape the unified problems log ( output/cache/problems.json ) for the
	errors page , GROUPED BY PAPER so each paper appears once with all its
	problems nested underneath ( expand to see them ).

	A problem's group key is its 'paper' field when present ( a missing
	reference is attributed to the paper that cites it ) , else its own 'id'
	( which is the paper's internal key for the per-paper kinds , or the
	manager item key for a non-imported snapshot item ). Group titles are
	resolved best-effort from the unified DB. Groups are sorted by problem
	count then most-recent failure ; problems within a group newest-first.
	Returns { total , paper_count , generated_at , groups } ."""
	from ..db import papers as papers_db
	data = papers_db.load_problems( args )
	groups , total = {} , 0
	for kind in data:
		for entry in ( data.get( kind ) or {} ).values():
			total += 1
			gkey = entry.get( "paper" ) or entry.get( "id" )
			g = groups.get( gkey )
			if g is None:
				g = groups[ gkey ] = { "id": gkey , "problems": [] }
			g[ "problems" ].append( entry )

	out = []
	for gkey , g in groups.items():
		probs = sorted(
			g[ "problems" ] ,
			key=lambda e: ( e.get( "last_seen" ) or "" ) ,
			reverse=True ,
		)
		# Best-effort title from the unified DB ( DOI / synthetic keys resolve ;
		# manager item keys and reference WIDs simply won't ).
		title = None
		try:
			paper = papers_db.load( args , gkey )
		except Exception:
			paper = None
		if paper:
			title = paper.get( "title" )
		out.append( {
			"id":        gkey ,
			"title":     title ,
			"kinds":     sorted( { p.get( "kind" ) for p in probs if p.get( "kind" ) } ) ,
			"count":     len( probs ) ,
			"last_seen": max( ( p.get( "last_seen" ) or "" ) for p in probs ) if probs else "" ,
			"problems":  probs ,
		} )
	out.sort( key=lambda gp: ( gp[ "count" ] , gp[ "last_seen" ] ) , reverse=True )

	generated_at = None
	p = papers_db.problems_path( args )
	if p.exists():
		generated_at = time.strftime(
			"%Y-%m-%d %H:%M:%S" , time.localtime( p.stat().st_mtime ) )
	return {
		"total":        total ,
		"paper_count":  len( out ) ,
		"generated_at": generated_at ,
		"groups":       out ,
	}


class SnapshotCache:
	"""
	Auto-refreshes when underlying source changes.
	- Zotero: watches sqlite + WAL + SHM mtimes.
	- Mendeley API: no local file to watch ; refreshes every `ttl` seconds.
	A short debounce avoids stat-storming under burst traffic.
	"""

	def __init__( self , args , debounce: float = 0.5 , ttl: float = 60.0 ):
		self.args     = args
		self.debounce = debounce
		self.ttl      = ttl
		self._titles: Optional[ Set[ str ] ] = None
		self._dois:   Optional[ Set[ str ] ] = None
		self._last_attempt = 0.0
		self._last_refresh = 0.0
		self._last_sig: Optional[ Tuple[ float , ... ] ] = None
		self._watch_files: List[ Path ] = self._resolve_watch_files()

	def _resolve_watch_files( self ) -> List[ Path ]:
		if self.args.manager.lower() == "zotero":
			from ..zotero.zotero import Zotero
			z  = Zotero( self.args )
			db = z.sqlite_path
			return [ db , db.with_suffix( ".sqlite-wal" ) , db.with_suffix( ".sqlite-shm" ) ]
		return []

	def _source_sig( self ) -> Optional[ Tuple[ float , ... ] ]:
		if not self._watch_files:
			return None
		return tuple( f.stat().st_mtime if f.exists() else 0.0 for f in self._watch_files )

	def _refresh( self ):
		# Fast path : pull title + DOI straight from the manager
		# source ( Zotero SQLite / Mendeley jsonl cache ) instead of
		# round-tripping the unified output/cache/papers/ DB. The
		# 'exists' lookup only needs these two sets , so the heavy
		# upsert + save_snapshot path that ` prma snapshot ` uses
		# would burn 10-30s per refresh for nothing.
		t0 = time.time()
		titles , dois = snap_module.titles_and_dois( self.args )
		if not titles and not dois:
			raise RuntimeError( "snapshot returned nothing — check --manager flag" )
		self._titles = titles
		self._dois   = dois
		self._last_refresh = time.time()
		print(
			f"snapshot refreshed — {len(titles)} titles, {len(dois)} DOIs "
			f"( {self._last_refresh - t0:.2f}s )"
		)

	def get( self , force: bool = False ) -> Tuple[ Set[ str ] , Set[ str ] ]:
		now = time.time()

		if force:
			self._refresh()
			self._last_sig     = self._source_sig()
			self._last_attempt = now
			return self._titles , self._dois

		if self._titles is not None and ( now - self._last_attempt ) < self.debounce:
			return self._titles , self._dois
		self._last_attempt = now

		if self._titles is None:
			self._refresh()
			self._last_sig = self._source_sig()
			return self._titles , self._dois

		sig = self._source_sig()
		if sig is not None:
			if sig != self._last_sig:
				self._refresh()
				self._last_sig = sig
		else:
			if ( now - self._last_refresh ) > self.ttl:
				self._refresh()

		return self._titles , self._dois


# ---------------------------------------------------------------------------
# Clean , bounded progress for the live --watch worker
# ---------------------------------------------------------------------------
# The per-paper suite runs the library tasks ( yolo / ocr / images / methods /
# code / md ) , each of which prints its own planning + summary lines and spins up its
# own tqdm bar -- so processing a batch of new papers floods the console. For
# the background worker we don't want that : we swallow ALL of that sub-task
# chatter and render our own tidy , self-updating single status line ( one
# carriage-returned row : paper k/N -> stage bar -> elapsed -> title ). The rich ,
# granular live view lives on the dashboard's /api/jobs toast ; the console just
# needs to stay readable.

@contextlib.contextmanager
def _silence_tasks():
	"""Silence the per-task chatter while the worker processes papers :
	  - swallow stdout ( every ` YOLO :: ... ` / ` OCR :: ... ` / planning line
	    the tasks print ) ;
	  - disable every tqdm bar the tasks create ( tqdm writes to stderr ) .
	Yields the REAL stdout so the worker can draw its own progress block on it.
	Restored on exit even if a stage raises.

	Process-wide for the duration ( the /exists request log is also quiet while
	a paper processes ) -- acceptable : --watch is opted into for background
	processing , and the rich live view is on the dashboard."""
	import tqdm as _tqdm_pkg
	tqdm_cls   = _tqdm_pkg.std.tqdm
	orig_init  = tqdm_cls.__init__
	real_stdout = sys.stdout
	devnull = open( os.devnull , "w" )

	def _disabled_init( self , *a , **k ):
		k[ "disable" ] = True          # force-off ; overrides any caller value
		orig_init( self , *a , **k )

	try:
		sys.stdout = devnull
		tqdm_cls.__init__ = _disabled_init
		yield real_stdout
	finally:
		sys.stdout = real_stdout
		tqdm_cls.__init__ = orig_init
		try: devnull.close()
		except Exception: pass


def _progress_bar( frac , width=22 ):
	frac = 0.0 if frac < 0 else ( 1.0 if frac > 1 else frac )
	fill = int( round( frac * width ) )
	return "█" * fill + "░" * ( width - fill )


class _LiveStatus:
	"""A single self-updating status line for the watch worker -- the same
	technique tqdm uses ( carriage-return rewrite of ONE line ) , chosen over a
	multi-line ANSI block because that stacks / garbles the moment a line wraps
	at the terminal width. Everything stays on one nested line :

	  watch · 2/12 · [███████░░░] 3/6 ocr · 14s · Multi-echo versus single-echo…

	  paper k/N  ·  stage bar s/S <stage>  ·  elapsed in this stage  ·  title

	Processing is strictly sequential ( run_suite finishes yolo before ocr , … ) ;
	this line just advances through the stages. A 1 Hz ticker keeps the elapsed
	counter live so a long OCR never looks frozen , and the line is truncated to
	the terminal width so it can never wrap onto a second row. When stdout isn't
	a TTY ( piped / logged ) it degrades to one printed line per PAPER -- no
	carriage returns or cursor codes to garble a logfile."""

	def __init__( self , stream , total_papers , stages ):
		self.s        = stream
		self.total    = max( 1 , total_papers )
		self.stages   = list( stages )
		self.n_stages = max( 1 , len( self.stages ) )
		self.tty      = bool( getattr( stream , "isatty" , lambda: False )() )
		self.paper_i  = 0
		self.title    = ""
		self.stage_i  = 0
		self.stage    = ""
		self.t_stage  = time.time()
		self._active  = False   # have we drawn a live line that needs clearing?
		self._lock    = threading.Lock()
		self._stop    = False
		self._ticker  = threading.Thread( target=self._tick_loop , daemon=True )
		self._ticker.start()

	def start_paper( self , i , title ):
		self.paper_i = i
		self.title   = title or ""
		self.stage_i = 0
		self.stage   = ""
		self.t_stage = time.time()
		self._render( new_paper=True )

	def start_stage( self , stage ):
		self.stage   = stage
		self.stage_i = ( self.stages.index( stage ) + 1 ) if stage in self.stages \
		             else min( self.stage_i + 1 , self.n_stages )
		self.t_stage = time.time()
		self._render( new_paper=False )

	def _tick_loop( self ):
		while not self._stop:
			time.sleep( 1.0 )
			self._render( new_paper=False , tick=True )

	def _render( self , new_paper , tick=False ):
		with self._lock:
			if self._stop:
				return
			elapsed = int( time.time() - self.t_stage )
			if self.tty:
				bar  = _progress_bar( self.stage_i / self.n_stages , width=10 )
				line = ( f"watch · {self.paper_i}/{self.total} · [{bar}] "
				         f"{self.stage_i}/{self.n_stages} {self.stage or '…'} · "
				         f"{elapsed}s · {self.title}" )
				width = shutil.get_terminal_size( ( 100 , 24 ) ).columns
				if len( line ) > width - 1:
					line = line[ : width - 2 ] + "…"
				self.s.write( "\r\033[K" + line )      # col 0 , clear to EOL , rewrite
				self.s.flush()
				self._active = True
			elif new_paper:
				# Non-TTY : one line per paper ( the heartbeat / per-stage churn
				# would just spam a logfile ).
				self.s.write( f"watch :: processing [{self.paper_i}/{self.total}] {self.title}\n" )
				self.s.flush()

	def close( self , msg=None ):
		self._stop = True
		with self._lock:
			if self.tty and self._active:
				self.s.write( "\r\033[K" )    # wipe the live line
				self._active = False
			if msg:
				self.s.write( msg + "\n" )
			self.s.flush()


class ProcessWorker:
	"""
	Live processing ( ` prma server --watch ` ).

	A background daemon thread that watches the same source the SnapshotCache
	watches ( Zotero SQLite mtime ; a TTL poll for Mendeley's API ) and , when
	the library changes , brings the unified DB in sync and runs the full
	per-paper suite on every paper that's NEW since the last sync :

	  get_common( snapshot + auto OpenAlex for new DOIs )
	    -> for each new key : process.run_suite ( yolo -> ocr -> images ->
	       methods -> code -> md -> modalities [ -> summarize ] , scoped to that one paper )
	    -> ONE dashboard reindex for the batch

	On startup it first sweeps the BACKLOG -- every library paper that still has
	undone pipeline work ( no yolo / md / methods / code ) -- and processes it quietly
	in the background ( disable with backlog=False ) , then watches for newly
	added papers from there on.

	Console output stays tidy : all the sub-task chatter is silenced and progress
	is shown as one self-updating status line ( see
	_silence_tasks / _LiveStatus ). The granular live view is on the
	dashboard's /api/jobs toast , which this also feeds via a thread-safe job log.

	We reuse the SnapshotCache's watch-file signature so the worker and the
	/exists hot path agree on "did the source change", and seed last_sig at
	startup so the WATCH side only acts on papers added AFTER boot ( the backlog
	sweep handles what's already there ).
	"""

	# Floor on how often we re-stat the source , even under a tight mtime
	# change loop , so a burst of Zotero writes coalesces into one scan.
	_MIN_INTERVAL = 2.0

	def __init__( self , args , dash , cache , summarize=False , backlog=True , tui=False ):
		self.args      = args
		self.dash      = dash
		self.cache     = cache
		self.summarize = summarize
		self.backlog   = backlog
		# tui=True suppresses the carriage-return status line ( _LiveStatus ) so the
		# separate read-only ServerTUI ( src/server/tui.py ) can own the screen. It
		# ONLY gates console drawing -- the processing path is identical either way.
		self._tui      = tui
		self._lock     = threading.Lock()
		self._active   = None     # job dict currently processing , or None
		self._recent   = []       # finished jobs , newest first ( capped )
		self._scanning = False
		# Snapshot of the batch in flight ( total / done / current index / label /
		# the ordered stages / the paper titles ) , read by the TUI for its queue +
		# progress bars. Plain dict writes under _lock ; never any I/O.
		self._batch    = None
		# Keys we've already retried via the ready-rescan ( see _ready_keys ) ,
		# so a paper that lands its PDF but still can't be processed doesn't
		# re-trigger a full reindex on every later library edit.
		self._ready_attempted = set()
		# Seed from the current source signature so the WATCH side is a no-op at
		# boot : it only acts on changes AFTER the server comes up. ( The backlog
		# sweep , below , is what processes papers already in the library. )
		self._last_sig  = cache._source_sig()
		self._last_scan = time.time()

	# -- public ( read by the /api/jobs handler ) ----------------------------

	def snapshot_jobs( self ):
		with self._lock:
			return {
				"enabled":  True ,
				"scanning": self._scanning ,
				"active":   dict( self._active ) if self._active else None ,
				"recent":   [ dict( j ) for j in self._recent[ :20 ] ] ,
			}

	def snapshot_batch( self ):
		"""A copy of the in-flight batch for the TUI ( None when idle ). Read-only ;
		the TUI never mutates worker state."""
		with self._lock:
			return dict( self._batch ) if self._batch else None

	# -- lifecycle -----------------------------------------------------------

	def start( self ):
		t = threading.Thread( target=self._loop , daemon=True )
		t.start()
		return self

	def _loop( self ):
		# First , clear whatever's already undone in the library ( background ) ,
		# then settle into watching for newly added papers.
		if self.backlog:
			try:
				self._run_backlog()
			except Exception as e:
				print( f"watch :: backlog sweep failed ( {e!r} )" )
		while True:
			try:
				self._tick()
			except Exception as e:
				print( f"watch :: tick failed ( {e!r} )" )
			time.sleep( self._MIN_INTERVAL )

	# -- backlog : finish papers with undone pipeline work -------------------

	def _backlog_keys( self ):
		"""Library papers the per-paper suite should (re)run on. Delegates the
		decision to process.needs_processing , which is AUTHORITATIVE : a paper is
		'done' once it's either fully produced OR already attempted end-to-end at
		the current SUITE_VERSION with the current PDF ( the paper[ 'processed' ]
		stamp ). That's what stops the backlog from re-sweeping -- and re-
		announcing -- papers that simply can't make further progress ( no PDF / no
		sections / a dead PDF ) on every restart. Newest-updated first , so the
		freshest additions clear soonest."""
		from ..db    import papers as papers_db
		from ..tasks import process as process_task
		need = []
		for key , paper in papers_db.iter_all( self.args ):
			if process_task.needs_processing( self.args , key , paper ):
				need.append( ( paper.get( "updated_at" ) or "" , key ) )
		need.sort( reverse=True )
		return [ k for _ , k in need ]

	def _run_backlog( self ):
		keys = self._backlog_keys()
		if not keys:
			print( "watch :: backlog clear -- every library paper is processed" )
			return
		print(
			f"watch :: backlog -- {len( keys )} paper(s) with undone tasks ; "
			f"processing quietly in the background ( Ctrl-C to stop )"
		)
		self._process_batch( keys , label="backlog" )

	# -- papers whose PDF arrived AFTER they were added -----------------------

	def _ready_keys( self ):
		"""Papers whose PDF has ARRIVED on disk since they were added but that
		still have undone PDF work ( no yolo / md / methods / code ).

		This closes the common race : Zotero registers an item ~tens of seconds
		before its attachment finishes downloading , so a freshly-added paper is
		detected as 'new' , has its OpenAlex meta fetched ( DOI-only , no file
		needed ) , but the PDF pipeline finds no file on disk and skips every
		stage -- and the paper never comes back as 'new' once the file lands , so
		yolo / ocr / images / methods / md would otherwise never run on it until
		the next server restart ( the startup backlog sweep ).

		Differences from _backlog_keys ( the startup sweep ) :
		  - REQUIRES the PDF to exist on disk , so we don't churn a 30s reindex
		    while a download is still pending ( pdf_path can be set to a path
		    that doesn't exist yet ) ;
		  - offers each key only ONCE per session ( _ready_attempted ) , so a
		    paper that lands its PDF but still can't be processed doesn't
		    reprocess on every later library edit."""
		from ..db    import papers as papers_db
		from ..tasks import process as process_task
		out = []
		for key , paper in papers_db.iter_all( self.args ):
			if key in self._ready_attempted:
				continue
			pdf = paper.get( "pdf_path" )
			if not pdf or not Path( pdf ).exists():
				continue                               # no file yet -> wait
			# needs_processing's pdf_sig flips the moment the file lands ( '' ->
			# path|mtime|size ) , so a paper stamped while PDF-less is re-queued.
			if process_task.needs_processing( self.args , key , paper ):
				out.append( ( paper.get( "updated_at" ) or "" , key ) )
		out.sort( reverse=True )
		return [ k for _ , k in out ]

	# -- one polling tick ----------------------------------------------------

	def _should_scan( self ):
		"""True when the source changed since we last looked ( Zotero mtime ) ,
		or -- for a manager with no file to watch ( Mendeley ) -- when the TTL
		has elapsed."""
		sig = self.cache._source_sig()
		now = time.time()
		if sig is None:
			if ( now - self._last_scan ) >= self.cache.ttl:
				self._last_scan = now
				return True
			return False
		if sig != self._last_sig:
			self._last_sig  = sig
			self._last_scan = now
			return True
		return False

	def _tick( self ):
		if not self._should_scan():
			return

		with self._lock:
			self._scanning = True
		try:
			new_keys = self._snapshot_and_diff()
			# Also pick up papers whose PDF only just landed on disk -- the
			# detection-before-download race ( see _ready_keys ). The 'updated'
			# tick that registers the finished download is our signal to finally
			# run the PDF pipeline on them.
			ready = self._ready_keys()
		finally:
			with self._lock:
				self._scanning = False

		# De-dup , brand-new papers first ( a new paper that already has its PDF
		# appears in both lists -- keep it as 'new' ).
		keys = list( dict.fromkeys( list( new_keys ) + ready ) )
		if not keys:
			return

		# Each ready-rescan pick gets exactly one attempt this session.
		for k in ready:
			self._ready_attempted.add( k )

		n_new   = len( new_keys )
		n_ready = len( keys ) - n_new
		parts   = []
		if n_new:
			parts.append( f"{n_new} new" )
		if n_ready:
			parts.append( f"{n_ready} now-ready" )
		print( f"watch :: {' + '.join( parts )} paper(s) -> processing" )
		self._process_batch( keys , label="new" )

	def _snapshot_and_diff( self ):
		"""Snapshot the manager into the unified DB and return the primary
		keys that are NEW since the previous DB state ( sorted ). get_common
		also auto-fetches OpenAlex meta for the new DOIs as a side effect."""
		from ..db    import papers as papers_db
		before = { k for k , _ in papers_db.iter_all( self.args ) }
		view   = snap_module.get_common( self.args )
		after  = set( view.keys() )
		return sorted( after - before )

	def _stage_seq( self ):
		"""The stages run_suite drives ( for the progress block ) , in order."""
		seq = [ "openalex" , "yolo" , "ocr" , "images" , "methods" , "code" , "md" , "modalities" ]
		if self.summarize:
			seq.append( "summarize" )
		return seq

	def _process_batch( self , keys , label ):
		"""Process a list of paper keys ( a backlog sweep or a batch of newly
		added papers ) one at a time , scoped to each , with all sub-task chatter
		silenced and a single tidy nested-bar block on the console. Feeds the
		/api/jobs log as it goes , then does ONE quiet dashboard reindex for the
		whole batch at the end."""
		from ..db    import papers as papers_db
		from ..tasks import process as process_task
		stages = self._stage_seq()
		n_ok , n_err = 0 , 0
		t0 = time.time()

		# Titles up front : they feed the TUI's queue view and are reused per paper
		# below ( so no second DB load ). Pure disk reads ; no terminal I/O.
		titles = [ ( papers_db.load( self.args , k ) or {} ).get( "title" ) or k
		           for k in keys ]
		with self._lock:
			self._batch = { "total": len( keys ) , "done": 0 , "i": 0 ,
			                "label": label , "stages": list( stages ) ,
			                "titles": titles }

		with _silence_tasks() as real_stdout:
			# The plain carriage-return status line only draws when the TUI isn't
			# running ( otherwise both would fight over the same terminal ).
			prog_ui = None if self._tui else _LiveStatus( real_stdout , len( keys ) , stages )
			try:
				for i , key in enumerate( keys , 1 ):
					title = titles[ i - 1 ]
					job = {
						"key": key , "title": title , "stage": "starting" ,
						"status": "processing" , "started": time.time() ,
					}
					with self._lock:
						self._active = job
						if self._batch: self._batch[ "i" ] = i
					if prog_ui: prog_ui.start_paper( i , title )

					def prog( stage , _job=job ):
						with self._lock:
							_job[ "stage" ] = stage
						if prog_ui: prog_ui.start_stage( stage )

					try:
						# reindex=False : one batch-level rebuild below covers all.
						process_task.run_suite(
							self.args , [ key ] ,
							summarize = self.summarize ,
							progress  = prog ,
							reindex   = False ,
						)
						job[ "status" ] = "done"
						n_ok += 1
					except Exception as e:
						job[ "status" ] = "error"
						job[ "error" ]  = str( e )
						n_err += 1
					finally:
						job[ "finished" ] = time.time()
						with self._lock:
							self._active = None
							if self._batch: self._batch[ "done" ] = i
							self._recent.insert( 0 , job )
							del self._recent[ 50: ]
			finally:
				if prog_ui: prog_ui.close()
				with self._lock:
					self._batch = None

		# One reindex for the whole batch ( cheaper than per-paper ) , applied
		# quietly so anyone browsing isn't bounced to a build screen. Same for
		# the library-wide reports ( /method-images ) -- both are whole-library
		# passes , so they run once here rather than inside run_suite.
		self.dash.rebuild_after_process()
		process_task.refresh_reports( self.args )
		errs = f" , {n_err} failed" if n_err else ""
		print(
			f"watch :: {label} done -- processed {n_ok} paper(s){errs} "
			f"in {time.time() - t0:.0f}s ; dashboard index refreshed"
		)


TITLE_THRESHOLD = 96


def lookup( cache: SnapshotCache , queries: List[ Dict ] ) -> List[ Dict ]:
	titles , dois = cache.get()
	results = []
	for q in queries:
		nd  = utils.normalize_doi( q.get( "doi" ) or "" )
		nt  = utils.normalize_title( q.get( "title" ) or "" )
		exists = False

		if nd:
			exists = nd in dois
		elif nt:
			exists = bool( process.extractOne(
				nt , titles ,
				scorer     = fuzz.token_sort_ratio ,
				score_cutoff = TITLE_THRESHOLD ,
			) )

		results.append( {
			"id":     q.get( "id" ) ,
			"exists": exists ,
			"title":  q.get( "title" ) ,
			"doi":    q.get( "doi" ) ,
		} )
	return results


class ThreadingHTTPServer( ThreadingMixIn , HTTPServer ):
	daemon_threads = True


class Handler( BaseHTTPRequestHandler ):
	cache:  SnapshotCache = None   # injected at startup
	dash:   DashboardData = None   # injected at startup ; None in minimal mode
	worker: "ProcessWorker" = None # injected at startup IFF --watch ; else None
	# Minimal mode ( ` prma --exists ` ) : only the userscript surface is up , so
	# every other route is refused with an explanation instead of dereferencing
	# a dash / figstate that was deliberately never built.
	minimal: bool = False
	# One per figure report , injected at startup. Keyed by the report's mode name ,
	# which is also its figure_state collection and its /api/<mode>/… prefix , so a
	# route only has to pull the name out of the path.
	figstate: Dict[ str , "FigureSelectionState" ] = {}
	# The hand-curated boards , injected at startup ; None in minimal mode.
	#   tiers -> /tiers  ( buckets you place papers in )
	#   sort  -> /sort   ( sections defined by the tags a paper carries )
	# Both are the same BoardState over a different src/db store , and both draw
	# their per-row links / modality stamps from the one shared papermeta.
	tiers: "BoardState" = None
	sort:  "BoardState" = None
	papermeta: "PaperMeta" = None

	def log_message( self , *_ ):
		return

	def _send_json( self , code: int , payload: Dict ):
		raw = json.dumps( payload ).encode( "utf-8" )
		self.send_response( code )
		self.send_header( "Content-Type"                , "application/json; charset=utf-8" )
		self.send_header( "Content-Length"              , str( len( raw ) ) )
		self.send_header( "Access-Control-Allow-Origin" , "*" )
		self.send_header( "Access-Control-Allow-Methods", "GET, POST, OPTIONS" )
		self.send_header( "Access-Control-Allow-Headers", "Content-Type" )
		self.end_headers()
		self.wfile.write( raw )

	def _send_html( self , code: int , page: str ):
		raw = page.encode( "utf-8" )
		self.send_response( code )
		self.send_header( "Content-Type"   , "text/html; charset=utf-8" )
		self.send_header( "Content-Length" , str( len( raw ) ) )
		# These pages are read fresh from disk each request ; never let the
		# browser serve a stale copy after a hand-edit / update.
		self.send_header( "Cache-Control"  , "no-store" )
		self.end_headers()
		self.wfile.write( raw )

	def _send_minimal_notice( self ):
		"""The answer to every route that ISN'T part of the userscript surface when
		the server was started with --exists. Minimal mode never builds the
		dashboard index , the figure reports or the live worker , so there's nothing
		behind those routes -- say so plainly rather than 404 ( which reads like a
		bug ) or blow up on a None. HTML for a browser , JSON for a fetch."""
		msg  = ( "minimal mode ( --exists ) : only POST /exists and GET /api/version "
			"are served -- the dashboard , figure reports and background processing "
			"are not running" )
		hint = "restart without --exists ( ` prma server ` , or ` prma ` for the live one )"
		if "text/html" in ( self.headers.get( "Accept" ) or "" ):
			self._send_html( 503 ,
				"<h1>prma &mdash; minimal mode</h1>"
				f"<p>{html.escape( msg )}.</p>"
				f"<p>{html.escape( hint )}.</p>" )
		else:
			self._send_json( 503 , { "error": msg , "hint": hint } )

	def _send_static( self , base , rel ):
		"""Serve a file under `base` ( e.g. output/images/ ) by relative path ,
		with a path-traversal guard so only files actually inside `base` are
		reachable. Used for the figure crops / montages that ` prma md ` links
		and the 'Figures' column points at."""
		base_r = Path( base ).resolve()
		target = ( base_r / unquote( rel ) ).resolve()
		if target != base_r and base_r not in target.parents:
			self._send_json( 403 , { "error": "forbidden" } )
			return
		if not ( target.exists() and target.is_file() ):
			self._send_json( 404 , { "error": "not found" } )
			return
		ctype = mimetypes.guess_type( str( target ) )[ 0 ] or "application/octet-stream"
		try:
			data = target.read_bytes()
		except Exception as e:
			self._send_json( 500 , { "error": str( e ) } )
			return
		self.send_response( 200 )
		self.send_header( "Content-Type"   , ctype )
		self.send_header( "Content-Length" , str( len( data ) ) )
		self.end_headers()
		self.wfile.write( data )

	def _send_figure_report( self , mode ):
		"""Serve one of the two figure reports -- the SAME file on disk its command
		writes , not a second renderer , so the CLI and the server can never drift :

		  "method-images"  ` prma method-images ` -> /method-images
		  "images"         ` prma all-images `    -> /images

		The --watch worker rebuilds both after each batch of new papers ( see
		process.refresh_reports ) , so they stay current on their own.

		Built lazily on first open when it isn't there yet ( the pattern the
		dashboard uses for its index ) : the server is threaded , so the sweep
		doesn't block other requests. When there's nothing to build from we
		explain why rather than 404 -- the usual cause is a library the PDF suite
		hasn't reached ( plus , for method-images , an empty keyword list )."""
		mod  = _report_module( mode )
		args = self.dash.args
		path = mod.report_path( args )
		if not path.exists():
			print( f"server    :: no {mode} report yet ; building it now ( first open )" )
			mod.rebuild( args )
		try:
			page = path.read_text( encoding="utf-8" )
		except Exception as e:
			cmd  = "prma all-images" if mode == "images" else "prma method-images"
			kw   = ( "" if mode == "images" else
				" , plus search terms on the command line or in "
				"<code>config/method-images.txt</code>" )
			page = (
				f"<h1>No {mode} report yet</h1>"
				f"<p>Nothing has been written to <code>{path}</code> ( {e} ).</p>"
				"<p>It needs the PDF suite to have run ( <code>prma process</code> , or "
				f"this server with <code>--watch</code> ){kw}. Run "
				f"<code>{cmd}</code> to see what it says.</p>"
				'<p><a href="/">&larr; Dashboard</a></p>'
			)
		self._send_html( 200 , page )

	def _send_pdf( self , key ):
		"""Stream a library paper's local PDF , looked up by its primary key
		via the unified DB. Only ever serves a path that's actually recorded
		as that paper's pdf_path ( no arbitrary filesystem access )."""
		from ..db import papers as papers_db
		paper = papers_db.load( self.dash.args , key ) if key else None
		pdf   = ( paper or {} ).get( "pdf_path" )
		if not ( pdf and Path( pdf ).exists() ):
			self._send_json( 404 , { "error": "pdf not found on disk" } )
			return
		try:
			data = Path( pdf ).read_bytes()
		except Exception as e:
			self._send_json( 500 , { "error": str( e ) } )
			return
		self.send_response( 200 )
		self.send_header( "Content-Type"        , "application/pdf" )
		self.send_header( "Content-Length"      , str( len( data ) ) )
		self.send_header( "Content-Disposition" , f'inline; filename="{Path( pdf ).name}"' )
		self.end_headers()
		self.wfile.write( data )

	def do_OPTIONS( self ):
		self.send_response( 204 )
		self.send_header( "Access-Control-Allow-Origin" , "*" )
		self.send_header( "Access-Control-Allow-Methods", "GET, POST, OPTIONS" )
		self.send_header( "Access-Control-Allow-Headers", "Content-Type" )
		self.end_headers()

	# -- Dashboard GET routes ( the /exists userscript path is POST-only ) --

	def _qs( self ):
		return parse_qs( urlparse( self.path ).query )

	def _arg( self , qs , name , default ):
		v = qs.get( name )
		return v[ 0 ] if v else default

	def _arg_int( self , qs , name , default ):
		try:
			return int( self._arg( qs , name , str( default ) ) )
		except ( ValueError , TypeError ):
			return default

	def _board( self , path , suffix ):
		"""'/api/sort' -> the sort BoardState , '/api/tiers/version' -> the tier
		one , for the routes the two hand-curated boards share. None when `path`
		isn't one of them."""
		for name , board in ( ( "tiers" , self.tiers ) , ( "sort" , self.sort ) ):
			if path == f"/api/{name}{suffix}":
				return board
		return None

	def do_GET( self ):
		path = urlparse( self.path ).path

		if path == "/api/version":
			# A cheap "did the reference library change" token for the DOI-button
			# userscript : it polls this and , when the token moves , re-queries
			# /exists and recolors the page in place ( a paper you just saved
			# flips green without a reload ). cache.get() refreshes only when the
			# Zotero source actually changed , so this is a bare stat() otherwise
			# -- independent of --watch. The token folds the last-refresh time and
			# the title / DOI counts so it bumps on any add / remove.
			#
			# Handled FIRST because it's the one GET the userscripts need , so it
			# has to sit above the minimal-mode guard below ( it only reads the
			# SnapshotCache , which minimal mode does build ).
			titles , dois = self.cache.get()
			n_t , n_d = len( titles or () ) , len( dois or () )
			ver = f"{getattr( self.cache , '_last_refresh' , 0.0 ):.3f}:{n_t}:{n_d}"
			self._send_json( 200 , { "version": ver , "titles": n_t , "dois": n_d } )
			return

		if self.minimal:
			# ` prma --exists ` : nothing past the userscript surface exists.
			self._send_minimal_notice()
			return

		if path in ( "/" , "/index.html" , "/dashboard" ):
			self._send_html( 200 , _load_dashboard_html() )
			return

		if path in ( "/errors" , "/errors.html" ):
			self._send_html( 200 , _load_errors_html() )
			return

		if path in ( "/tiers" , "/tiers.html" , "/tierlist" ):
			self._send_html( 200 , _load_tiers_html() )
			return

		if path in ( "/sort" , "/sort.html" ):
			self._send_html( 200 , _load_sort_html() )
			return

		board = self._board( path , "" )
		if board:
			# The whole curated document , plus the modality vocabulary the page's
			# autocomplete offers ( < --config >/methods.py -- the same list the
			# figure reports' pills come from , so the two never drift ).
			from ..utils import methods as methods_vocab
			try:
				vocab = list( methods_vocab.labels( self.dash.args ) )
			except Exception:
				vocab = []
			self._send_json( 200 , { "ok": True , "modalities": vocab ,
				**board.snapshot() } )
			return

		board = self._board( path , "/version" )
		if board:
			# Cheap "did another tab save" token , polled by the page ( same idea
			# as the figure reports' /api/<mode>/version ).
			self._send_json( 200 , { "rev": board.snapshot()[ "rev" ] } )
			return

		if path == "/api/errors":
			self._send_json( 200 , _errors_payload( self.dash.args ) )
			return

		if path in ( "/status" , "/status.html" ):
			self._send_html( 200 , _load_status_html() )
			return

		if path in ( "/method-images" , "/method-images.html" ):
			self._send_figure_report( "method-images" )
			return

		# The unfiltered sibling : every cropped figure. Exact match only -- the
		# "/images/" PREFIX below is the crop directory ` prma images ` writes ,
		# which this page's <img> tags point into.
		if path in ( "/images" , "/images.html" ):
			self._send_figure_report( "images" )
			return

		if path == "/api/status":
			# ?regen=1 recomputes fresh from the on-disk DB ( what opening the
			# page does ) ; otherwise serve the last persisted tally.
			regen = self._arg( self._qs() , "regen" , "" ) in ( "1" , "true" , "yes" )
			self._send_json( 200 , _status_payload( self.dash.args , regen=regen ) )
			return

		if path == "/pdf":
			self._send_pdf( self._arg( self._qs() , "key" , "" ) )
			return

		if path == "/api/paper":
			# Sectioned Markdown for the 'In Library' Read accordion.
			self._send_json( 200 , _paper_md_payload(
				self.dash.args , self._arg( self._qs() , "key" , "" ) ) )
			return

		mode = _figure_mode( path , "state" )
		if mode:
			# One figure report's curation ( selected figures + skipped papers ) ,
			# persisted server-side so it follows the user across browsers. Each
			# page fetches its OWN on load ; /images additionally reads the
			# method-images one to badge what's already curated there.
			st = self.figstate.get( mode )
			self._send_json( 200 , st.snapshot() if st
				else { "selected": [] , "skipped": [] } )
			return

		mode = _figure_mode( path , "version" )
		if mode:
			# A cheap "has the report been rebuilt" token ( the report file's mtime )
			# each figure page polls : when it moves , the --watch worker has rebuilt
			# that report.html with newly processed papers , so the page refreshes
			# itself ( no manual reload ). A bare stat() -- no library walk , no
			# rebuild -- so polling it is effectively free.
			try:
				m = _report_module( mode ).report_path( self.dash.args ).stat().st_mtime
			except Exception:
				m = 0.0
			self._send_json( 200 , { "version": f"{m:.3f}" } )
			return

		if path.startswith( "/images/" ):
			self._send_static( self.dash.args.output.joinpath( "images" ) ,
				path[ len( "/images/" ): ] )
			return

		# One paper's ` prma md ` document , RENDERED in the browser ( figures and
		# all ) rather than handed over as a file to download. Reached two ways ,
		# both resolved by _md_slug : /md/<doi> ( the dashboard's MD button ) and
		# the figure reports' relative ../md/<file>.md links , which land here
		# once they're served from /method-images and /images . ?raw=1 still
		# serves the Markdown source for anything that wants the file itself.
		if path.startswith( "/md/" ):
			if self._arg( self._qs() , "raw" , "" ) in ( "1" , "true" , "yes" ):
				slug = _md_slug( unquote( path[ len( "/md/" ): ] ) )
				self._send_static( self.dash.args.output.joinpath( "md" ) ,
					quote( f"{slug}.md" ) if slug else "" )
				return
			self._send_html( 200 , _load_md_html() )
			return

		# The figure reports' other extracted-text link : the plain Methods section
		# ( ../methods/… off disk ) , served as the .txt file it is.
		if path.startswith( "/methods/" ):
			self._send_static( self.dash.args.output.joinpath( "methods" ) ,
				path[ len( "/methods/" ): ] )
			return

		if path == "/api/jobs":
			# Live --watch progress for the dashboard's processing banner.
			# When the server is running WITHOUT --watch , the worker is None
			# and we report disabled so the front-end stays quiet.
			if self.worker is None:
				self._send_json( 200 , { "enabled": False } )
			else:
				self._send_json( 200 , self.worker.snapshot_jobs() )
			return

		if path == "/api/meta":
			# Pick up a fresher ` prma reindex ` if one happened ; otherwise ,
			# if we've NEVER built an index, opening the dashboard is the
			# signal to lazily build one. A persisted index is served as-is
			# ( refresh it explicitly via the Rebuild button / ` prma reindex ` ).
			self.dash.maybe_reload()
			if self.dash.status == "idle":
				# No index yet : build one from the EXISTING cache ( no network
				# fetch ). Refreshing the cache is the explicit Rebuild / reindex.
				self.dash.ensure_build( refresh=False )
			self._send_json( 200 , self.dash.meta() )
			return

		if path == "/api/search":
			qs = self._qs()
			# hide_skipped defaults ON : skipped rows are filtered out unless the
			# dashboard's "Show skipped" toggle asks for them ( hide_skipped=0 ).
			hide = self._arg( qs , "hide_skipped" , "1" ) not in ( "0" , "false" , "no" )
			# mode=title : fuzzy TITLE lookup instead of the boolean full-text
			# search -- what the /sort and /tiers boards ask for.
			self._send_json( 200 , self.dash.search(
				self._arg( qs , "q" , "" ) ,
				self._arg( qs , "pool" , "external" ) ,
				self._arg( qs , "sort" , "relevance" ) ,
				self._arg_int( qs , "limit" , 100 ) ,
				offset=self._arg_int( qs , "offset" , 0 ) ,
				direction=self._arg( qs , "dir" , None ) ,
				hide_skipped=hide ,
				mode=self._arg( qs , "mode" , "text" ) ,
			) )
			return

		if path == "/api/authors":
			self._send_json( 200 , self.dash.author_table(
				self._arg_int( self._qs() , "limit" , 200 ) ) )
			return

		self._send_json( 404 , { "error": "not found" } )

	def do_POST( self ):
		# Minimal mode ( --exists ) serves exactly two POSTs , both of which work off
		# the SnapshotCache : /exists itself , and /refresh ( force a re-read of the
		# library , handy when a manager gives us no file to watch ). Everything
		# else here is dashboard / figure-report state that minimal mode never built.
		if self.minimal and urlparse( self.path ).path not in ( "/exists" , "/refresh" ):
			self._send_minimal_notice()
			return

		mode = _figure_mode( self.path , "state" )
		if mode:
			# Toggle one figure's selected state or one paper's skipped state on ONE
			# figure report , persisted server-side. Body :
			#   { "kind": "selected"|"skipped" , "id": "<id>" , "on": bool }
			# One id per request ( like /api/skip ) so concurrent browsers don't
			# clobber each other's whole set. Returns the full state back so the
			# page can reconcile ( e.g. pick order ) if it wants to. The two reports'
			# collections are separate stores : a write here never touches the other.
			try:
				st = self.figstate.get( mode )
				if st is None:
					self._send_json( 503 , { "ok": False , "error": "state not loaded" } )
					return
				length = int( self.headers.get( "Content-Length" , "0" ) )
				body   = self.rfile.read( length ) if length > 0 else b"{}"
				data   = json.loads( body.decode( "utf-8" , errors="replace" ) )
				kind   = data.get( "kind" )
				if kind not in ( "selected" , "skipped" ):
					self._send_json( 400 , { "ok": False ,
						"error": "kind must be 'selected' or 'skipped'" } )
					return
				ident = data.get( "id" ) or ""
				on    = bool( data.get( "on" , True ) )
				if kind == "selected":
					st.set_selected( ident , on )
				else:
					st.set_skipped( ident , on )
				self._send_json( 200 , { "ok": True , **st.snapshot() } )
			except Exception as e:
				self._send_json( 500 , { "ok": False , "error": str( e ) } )
			return

		board = self._board( urlparse( self.path ).path , "" )
		if board:
			# Save a board ( /api/tiers or /api/sort ). The page owns the document
			# and posts it back WHOLE ( body : { "doc": { ... } } ) -- see
			# src/db/tiers.py for why that's the right granularity here and what
			# it does to protect the version it replaces.
			try:
				length = int( self.headers.get( "Content-Length" , "0" ) )
				body   = self.rfile.read( length ) if length > 0 else b"{}"
				data   = json.loads( body.decode( "utf-8" , errors="replace" ) )
				doc    = data.get( "doc" )
				if not isinstance( doc , dict ):
					self._send_json( 400 , { "ok": False , "error": "body needs a 'doc' object" } )
					return
				self._send_json( 200 , { "ok": True , **board.replace( doc ) } )
			except Exception as e:
				self._send_json( 500 , { "ok": False , "error": str( e ) } )
			return

		if self.path in ( "/api/paper-meta" , "/api/tiers/meta" ):
			# Per-row lookup for the board pages : { "keys": [ ... ] , "mods": bool }.
			# POST rather than GET because the key list is a few hundred DOIs long.
			# ( /api/tiers/meta is the original spelling , kept working. )
			try:
				length = int( self.headers.get( "Content-Length" , "0" ) )
				body   = self.rfile.read( length ) if length > 0 else b"{}"
				data   = json.loads( body.decode( "utf-8" , errors="replace" ) )
				keys   = data.get( "keys" )
				if not isinstance( keys , list ):
					self._send_json( 400 , { "ok": False , "error": "body needs a 'keys' list" } )
					return
				self._send_json( 200 , { "ok": True , "meta": self.papermeta.meta(
					keys , want_mods=bool( data.get( "mods" ) ) ) } )
			except Exception as e:
				self._send_json( 500 , { "ok": False , "error": str( e ) } )
			return

		if self.path == "/api/skip":
			# Toggle a paper's skipped ( hidden ) state ; persisted server-side so
			# it survives reloads. Body : { "key": "<row key>" , "skipped": bool }.
			try:
				length = int( self.headers.get( "Content-Length" , "0" ) )
				body   = self.rfile.read( length ) if length > 0 else b"{}"
				data   = json.loads( body.decode( "utf-8" , errors="replace" ) )
				key    = data.get( "key" ) or ""
				state  = self.dash.set_skip( key , bool( data.get( "skipped" , True ) ) )
				self._send_json( 200 , { "ok": True , "key": key , "skipped": state } )
			except Exception as e:
				self._send_json( 500 , { "ok": False , "error": str( e ) } )
			return

		if self.path == "/api/refresh":
			# Rebuild button : download fresh OpenAlex data , then re-index.
			self.dash.ensure_build( refresh=True )
			self._send_json( 200 , { "ok": True , "status": self.dash.status } )
			return

		if self.path == "/refresh":
			try:
				self.cache.get( force=True )
				self._send_json( 200 , { "ok": True } )
			except Exception as e:
				self._send_json( 500 , { "error": str( e ) } )
			return

		if self.path != "/exists":
			self._send_json( 404 , { "results": [] , "error": "not found" } )
			return

		try:
			length  = int( self.headers.get( "Content-Length" , "0" ) )
			body    = self.rfile.read( length ) if length > 0 else b"{}"
			data    = json.loads( body.decode( "utf-8" , errors="replace" ) )
			queries = data.get( "queries" , [] )

			if not isinstance( queries , list ):
				self._send_json( 400 , { "results": [] , "error": "queries must be a list" } )
				return

			cleaned = [
				{ "id": q.get( "id" ) , "title": q.get( "title" ) or "" , "doi": q.get( "doi" ) or "" }
				for q in queries if isinstance( q , dict )
			]

			results = lookup( self.cache , cleaned )

			REQUEST_LOG.add( cleaned , results )   # feed the TUI ( no-op cost otherwise )

			hits = sum( 1 for r in results if r[ "exists" ] )
			for r in results:
				if r[ "exists" ]:
					t = ( r.get( "title" ) or "" )[ :70 ]
					d = ( r.get( "doi" )   or "" )[ :70 ]
					print( f"  hit  {r.get('id')} | {t} | {d}" )
			print( f"served {len(results)} queries ({hits} hits)" )

			self._send_json( 200 , { "results": results } )

		except Exception as e:
			print( f"server error: {e!r}" )
			self._send_json( 500 , { "results": [] , "error": str( e ) } )


def _normalize_manager( args ):
	"""Resolve --manager / --zotero / --mendeley into a canonical form."""
	args.output = Path( args.output )
	args.config = Path( args.config )

	if not args.manager:
		if getattr( args , "zotero" , False ):
			args.manager = "zotero"
		elif getattr( args , "mendeley" , False ):
			args.manager = "mendeley"
		else:
			raise SystemExit( "must pass one of: --zotero | --mendeley | --manager <name>" )

	args.manager  = args.manager.lower()
	args.zotero   = args.manager == "zotero"
	args.mendeley = args.manager == "mendeley"
	if not getattr( args , "mendeley_source" , None ):
		args.mendeley_source = "api"
	return args


def run( args ):
	"""Boot the server. `args` may come from the prma CLI or server.py's own
	parser ; either way it needs the manager + server fields."""
	_normalize_manager( args )

	# --exists : minimal mode. Everything below the SnapshotCache -- the dashboard
	# index , the figure reports and their curation state , the live --watch worker
	# -- is skipped , so the only thing running is the userscript surface
	# ( POST /exists + GET /api/version , both served straight off the cache ).
	minimal = bool( getattr( args , "exists_only" , False ) )
	if minimal and getattr( args , "watch" , False ):
		# Explicit --exists --watch : minimal wins ( no background processing is
		# the reason to ask for minimal ) , but don't do it silently.
		print( "--exists :: minimal mode ; ignoring --watch ( no background processing )" )
		args.watch = False

	cache = SnapshotCache( args , debounce=args.debounce , ttl=args.ttl )
	cache.get( force=True )   # fail fast

	Handler.cache   = cache
	Handler.minimal = minimal

	if minimal:
		# Nothing to inject : the dashboard-backed routes are refused up front by
		# the minimal guards in do_GET / do_POST , so they never touch these.
		Handler.dash     = None
		Handler.worker   = None
		Handler.figstate  = {}
		Handler.tiers     = None
		Handler.sort      = None
		Handler.papermeta = None
		httpd   = ThreadingHTTPServer( ( args.host , args.port ) , Handler )
		watched = "mtime-watched" if cache._watch_files else f"ttl={args.ttl}s"
		base    = f"http://{args.host}:{args.port}"
		print( f"exists server  {base}/exists   (manager={args.manager}, {watched})" )
		print( f"version token  {base}/api/version   (userscript change poll)" )
		print(  "minimal mode   ON       (--exists : no dashboard , no figure reports , "
			"no background processing ; drop --exists for the full server)" )
		httpd.serve_forever()
		return

	dash = DashboardData( args )
	loaded = dash.load_from_disk()   # serve last ` prma reindex ` instantly if present
	dash.load_skips()                # restore the user's hidden ( skipped ) rows
	Handler.dash = dash

	# Figure-report curation ( selected figures + skipped papers ) , one store per
	# report : restore the user's picks so each report serves them to whatever
	# browser opens it.
	Handler.figstate = {}
	for _mode in FIGURE_REPORTS:
		_st = FigureSelectionState( args , _mode )
		_st.load()
		Handler.figstate[ _mode ] = _st

	# The hand-curated boards ( /tiers , /sort ) : one document each , loaded once
	# and served to whatever browser opens the page , plus the shared per-row
	# lookup they both draw links and modality stamps from.
	from ..db import tiers as _tiers_db , sortboard as _sort_db
	Handler.papermeta = PaperMeta( args , dash )
	for _attr , _store , _name in ( ( "tiers" , _tiers_db , "tiers" ) ,
	                                ( "sort"  , _sort_db  , "sort"  ) ):
		_board = BoardState( args , _store , _name )
		_board.load()
		setattr( Handler , _attr , _board )

	# Both figure reports are pre-built artifacts served verbatim. If the PAGE
	# template , a report's own renderer , or ( for method-images ) the keyword list
	# changed since one was last built -- e.g. you edited the page and restarted the
	# server -- rebuild it now , in the background so startup isn't blocked on the
	# whole-library sweep. Without this , an already-processed library never rebuilds
	# ( the --watch data path only fires when NEW papers land ) , so edits to the page
	# would never show. mtime-gated , so there's no cost when nothing changed.
	#
	# ONE thread for both , sequentially : each is a full-library pass and both stamp
	# paper[ 'modalities' ] on any straggler they meet , so running them concurrently
	# would just contend for the same CPU and the same records.
	try:
		stale = [ m for m in FIGURE_REPORTS if _report_module( m ).report_is_stale( args ) ]
		if stale:
			def _rebuild_reports():
				for m in stale:
					print( f"{m} :: report inputs changed ( page / renderer / keywords ) ; rebuilding in background" )
					_report_module( m ).rebuild( args )
					print( f"{m} :: report rebuilt" )
			threading.Thread( target=_rebuild_reports , daemon=True ).start()
	except Exception as e:
		print( f"figure reports :: staleness check skipped ( {e} )" )

	# Opt-in live terminal UI ( --tui ). A separate read-only thread that renders
	# task progress bars + the queue by polling worker state. Requires --watch
	# ( nothing to show otherwise ) , a real TTY , and rich. Determined BEFORE the
	# worker starts so the worker knows to suppress its plain status line ; if
	# anything is missing we silently stay on the classic prints.
	tui_on = False
	if ( getattr( args , "tui" , False ) and getattr( args , "watch" , False )
	     and sys.__stdout__ is not None and sys.__stdout__.isatty() ):
		try:
			from .tui import ServerTUI
			tui_on = True
		except Exception as e:
			print( f"--tui unavailable ( {e!r} ) ; using plain logs" )

	# Live processing : only when explicitly opted in via --watch , since the
	# per-paper suite ( YOLO / OCR ) is CPU-heavy. The worker seeds its change
	# signature at construction so coming online is a no-op -- it acts only on
	# papers added AFTER the server starts.
	worker = None
	if getattr( args , "watch" , False ):
		worker = ProcessWorker(
			args , dash , cache ,
			summarize=getattr( args , "watch_summarize" , False ) ,
			backlog=getattr( args , "watch_backlog" , True ) ,
			tui=tui_on ,
		).start()
		Handler.worker = worker

	httpd = ThreadingHTTPServer( ( args.host , args.port ) , Handler )
	watched = "mtime-watched" if cache._watch_files else f"ttl={args.ttl}s"
	base = f"http://{args.host}:{args.port}"
	if loaded:
		dash_state = f"prebuilt index from {dash.built_at} ; run ` prma reindex ` to refresh"
	else:
		dash_state = "no index yet ; builds on first open ( or run ` prma reindex ` )"
	header = [
		f"exists server  {base}/exists   (manager={args.manager}, {watched})" ,
		f"dashboard      {base}/          ({dash_state})" ,
		f"sort board     {base}/sort     (hand-curated , sections ARE tag sets ; CSV in / out)" ,
		f"tier list      {base}/tiers    (hand-curated buckets ; CSV in / out)" ,
		f"errors         {base}/errors   (pipeline problems log)" ,
		f"status         {base}/status   (pipeline completeness ; run ` prma status `)" ,
		f"design figures {base}/method-images  (caption-matched ; run ` prma method-images `)" ,
		f"all figures    {base}/images   (every crop , unfiltered ; run ` prma all-images `)" ,
	]
	if getattr( args , "watch" , False ):
		sm = " +summarize" if getattr( args , "watch_summarize" , False ) else ""
		bl = "backlog+new" if getattr( args , "watch_backlog" , True ) else "new-only"
		header.append( f"watch          ON{sm}  ({bl} ; live progress at {base}/api/jobs)" )
	else:
		header.append( "watch          off       (pass --watch to auto-process newly added papers)" )

	if tui_on:
		tui = ServerTUI( worker , header , REQUEST_LOG )
		tui.start()
		try:
			httpd.serve_forever()
		finally:
			tui.stop()
	else:
		for ln in header:
			print( ln )
		httpd.serve_forever()
