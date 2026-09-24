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

# What the two curated surfaces ADD UP TO : every paper on the sort board or with
# a figure picked on /images , screened against fixed inclusion criteria and with
# every architecture / acquisition number its own text states pulled out , each
# behind the verbatim quote it was parsed from ( src/review/build.py ). Served at
# GET /review ; its document comes from GET /api/review .
REVIEW_HTML_PATH = Path( __file__ ).resolve().parent.parent.joinpath(
	"dashboard" , "review.html" )

# The SAME three criteria , run over the papers you do NOT have : the dashboard's
# "All missing" pool ( works your library cites , and works that cite it ) ,
# screened on the only text there is for them -- a title and an OpenAlex abstract
# ( src/review/missing.py ). High precision , low recall by construction , and the
# page says so in those words. Served at GET /review-missing ; its document comes
# from GET /api/review-missing .
REVIEW_MISSING_HTML_PATH = Path( __file__ ).resolve().parent.parent.joinpath(
	"dashboard" , "review-missing.html" )

# The other cross-cutting view of the library : every paper ` prma code ` found a
# SOURCE-CODE / DATA repo for , with a column for each curated surface that holds
# it ( review / sort / tiers / picked figures ) , so "papers that ship code AND
# are in the review" is a sort rather than a search. Served at GET /code ; its
# rows come from GET /api/code and the xlsx from POST /api/code/export.xlsx .
CODE_HTML_PATH = Path( __file__ ).resolve().parent.parent.joinpath(
	"dashboard" , "code.html" )

# Its sibling for the OTHER half of "what does this paper stand on" : every
# PUBLIC DATASET ` prma datasets ` found -- the archive records a paper links
# ( OpenNeuro / NeuroVault / DANDI / OSF / Zenodo / Hugging Face / ... ) and the
# collections it only NAMES ( HCP , NSD , ABIDE , ... ) -- against the same four
# curated surfaces. Served at GET /datasets ; rows from GET /api/datasets , the
# workbook from POST /api/datasets/export.xlsx .
DATASETS_HTML_PATH = Path( __file__ ).resolve().parent.parent.joinpath(
	"dashboard" , "datasets.html" )

# The chrome the pages above SHARE : the palette + header styling
# ( common.css ) , the helpers they all used to redeclare ( common.js ) and the
# pre-paint theme restore ( boot.js ). Served verbatim under GET /static/ so
# each page links one line instead of carrying its own drifting copy.
#
# The two figure reports are deliberately NOT in on this : ` prma images ` writes
# them to output/ as standalone files that have to work opened straight off
# disk , with no server to fetch /static/ from.
STATIC_DIR = Path( __file__ ).resolve().parent.parent.joinpath(
	"dashboard" , "static" )


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


class BoardLocked( Exception ):
	"""Raised by BoardState.replace when the board is in view-only mode."""


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
	more instance rather than a second copy of any of this.

	A board also carries a VIEW-ONLY latch ( src/db/boardlock.py ) : one server-
	side boolean , the same for every browser that opens the page. While it is on
	the page greys its own editing out AND this refuses the write , which is the
	half that matters -- a tab left open from before the lock would otherwise
	still beacon its document back over the top on the way out."""

	def __init__( self , args , store , name ):
		self.args   = args
		self.store  = store
		self.name   = name
		self._lock  = threading.Lock()
		self.doc    = None
		self.rev    = 0
		self.locked = False

	def load( self ):
		"""Populate from disk ( call at startup ). Never raises."""
		from ..db import boardlock
		try:
			with self._lock:
				self.doc = self.store.load( self.args )
		except Exception as e:
			print( f"{self.name} :: could not load the board ( {e} )" )
			self.doc = None
		self.locked = boardlock.load( self.args , self.name )

	def snapshot( self ):
		with self._lock:
			doc = self.doc if self.doc is not None else self.store.default_doc()
			return { "rev": self.rev , "doc": doc , "locked": self.locked }

	def replace( self , doc ):
		"""Write a whole document ( the page's save ). Returns the normalized
		document as stored , with the new rev. Raises BoardLocked when the board
		is in view-only mode -- see the class docstring."""
		with self._lock:
			if self.locked:
				raise BoardLocked( self.name )
			self.doc = self.store.save( self.args , doc )
			self.rev += 1
			return { "rev": self.rev , "doc": self.doc }

	def set_lock( self , on ):
		"""Turn the view-only latch on / off , persisted. `rev` is bumped so the
		OTHER open tabs notice on their next version poll ( that is the only
		signal they have ) and flip with it."""
		from ..db import boardlock
		with self._lock:
			self.locked = boardlock.save( self.args , self.name , on )
			self.rev   += 1
			return { "rev": self.rev , "locked": self.locked }


class ReviewState:
	"""A screened document -- read from disk , rebuilt in the background.

	Unlike everything else the server holds , these are DERIVED and EXPENSIVE :
	a regex pass over every candidate , which is minutes ( see
	src/review/build.py ). So one is never built on the request that noticed it
	was stale. The request gets whatever is on disk , plus a flag saying the
	inputs have moved since ; a rebuild happens on its own thread , and the page
	watches its progress and re-fetches when it lands.

	One rebuild at a time , enforced here : the page polls , and two overlapping
	full-library passes would only fight each other for the same CPU.

	TWO SURFACES , ONE CLASS. `module` is whichever builder this instance drives ,
	and both expose the same five functions ( load / save / build / signature /
	is_stale ) :

	  src/review/build.py    -> /review          the two curated boards , full text
	  src/review/missing.py  -> /review-missing  the OpenAlex pool , abstracts only

	Staleness is the mtimes of whatever that builder reads , stamped into the
	document at build time -- a handful of stat() calls per poll , no parse.
	Editing a board is what makes /review out of date ; a ` prma reindex ` that
	finds new references is what makes /review-missing out of date. Each builder
	watches its own inputs , so each says so at exactly the right moment."""

	def __init__( self , args , module=None , name="review" ):
		from ..review import build as review_build
		self.args     = args
		self.module   = module or review_build
		self.name     = name    # what this surface calls itself in the logs
		self._lock    = threading.Lock()
		self.doc      = None
		self.building = False
		self.stage    = ""      # what the running build is doing , for the page
		self.done     = 0
		self.total    = 0
		self.error    = ""
		self.built_at = ""

	def build_kwargs( self ):
		"""Extra keyword arguments for this surface's build(). Empty for /review ,
		which reads everything it needs off disk ; /review-missing overrides it to
		hand its builder the pools the server is already holding rather than make
		it re-read a 154 MB index ( see MissingReviewState )."""
		return {}

	def load( self ):
		"""Read the persisted document ( call at startup ). Never raises."""
		try:
			doc = self.module.load( self.args )
		except Exception as e:
			print( f"{self.name} :: could not load the built document ( {e} )" )
			doc = None
		with self._lock:
			self.doc      = doc
			self.built_at = ( ( doc or {} ).get( "meta" ) or {} ).get( "generated" , "" )
		return doc is not None

	def stale( self ):
		"""Have the inputs moved since this was built ( or was it never )?"""
		try:
			return self.module.is_stale( self.args , self.doc )
		except Exception:
			return False

	def status( self ):
		"""The small payload the page polls : is there a review , is one being
		built , and how far along. Never carries the document itself."""
		with self._lock:
			return {
				"available" : self.doc is not None ,
				"building"  : self.building ,
				"stage"     : self.stage ,
				"done"      : self.done ,
				"total"     : self.total ,
				"error"     : self.error ,
				"generated" : self.built_at ,
				"stale"     : self.stale() ,
			}

	def snapshot( self ):
		"""The whole document plus the status block the page needs alongside it."""
		with self._lock:
			doc = self.doc
		out = dict( self.status() )
		if doc:
			out.update( doc )
		return out

	def rebuild( self , block=False ):
		"""Kick off a rebuild. Returns the status ; when one is already running
		this is a no-op that says so , so a page that polls can't stack them."""
		with self._lock:
			# status() takes this same lock , and it is not reentrant , so decide
			# here and report AFTER letting go.
			already = self.building
			if not already:
				self.building = True
				self.stage    = "starting"
				self.done , self.total , self.error = 0 , 0 , ""
		if already:
			return self.status()
		if block:
			self._build()
		else:
			threading.Thread( target=self._build , daemon=True ).start()
		return self.status()

	def _build( self ):
		def progress( stage , done , total ):
			with self._lock:
				self.stage , self.done , self.total = stage , done , total

		try:
			doc = self.module.build( self.args , progress=progress , **self.build_kwargs() )
			doc[ "meta" ][ "input_signature" ] = self.module.signature( self.args )
			self.module.save( self.args , doc )
			with self._lock:
				self.doc      = doc
				self.built_at = doc[ "meta" ][ "generated" ]
				self.error    = ""
			c = doc[ "meta" ][ "counts" ]
			print( f"{self.name} :: rebuilt -- {c[ 'included' ]} included / {c[ 'candidates' ]} candidates" )
		except Exception as e:
			print( f"{self.name} :: rebuild failed ( {e} )" )
			with self._lock:
				self.error = str( e )
		finally:
			with self._lock:
				self.building = False
				self.stage    = ""


class MissingReviewState( ReviewState ):
	"""/review-missing : the same screen over the papers you do NOT have.

	Everything about how it is served is identical to /review -- serve the last
	build , say when it is stale , rebuild on a thread while the page watches --
	so all of that is inherited. The one difference is where the candidates come
	from : the dashboard's own index , which this process is already holding in
	memory. Handing those pools to the builder saves it re-reading a 154 MB
	gzipped index off disk to arrive at the same rows.

	When the dashboard index is NOT ready ( nobody has opened the dashboard yet
	this run ) the pools are left out and the builder loads them from disk on its
	own thread , which is exactly what ` prma review-missing ` does."""

	def __init__( self , args , dash ):
		from ..review import missing as review_missing
		super().__init__( args , module=review_missing , name="review-missing" )
		self.dash = dash

	def build_kwargs( self ):
		d = self.dash
		if d is None or getattr( d , "status" , "" ) != "ready":
			return {}
		# _apply_pools swaps these lists wholesale rather than mutating them , so
		# reading the attribute hands back one consistent pool ( the same thing
		# _code_rows does with dash.library ).
		return { "pools": { "references": d.references , "cited_by": d.cited_by } }


class PaperMeta:
	"""The per-row lookup both board pages ask for : which links a paper can
	offer ( PDF / figures / MD / Methods ) and -- on request -- the modality
	stamp ` prma modalities ` already pinned on it , which is what lets a freshly
	added row arrive pre-tagged fMRI / EEG instead of blank ( and , on /sort ,
	drop straight into the section that says so ). Also on request : the code
	and data the scans found for it , which /sort writes into a new row's Code /
	Datasets cells.

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

	def meta( self , keys , want_mods=False , want_links=False ):
		"""What a page needs to draw a row it only knows the KEY of : the library
		identity ( title / DOI / year ) , which links exist for it , and
		optionally the modality stamp and the code / data links. Keys that aren't
		library papers come back with in_library=false and nothing else -- the
		page already holds their title / DOI from the search hit that added them."""
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
			if want_links:
				# Exactly what /code and /datasets show for the paper , off the same
				# index entry they read ( see _code_rows / _dataset_rows ) : the
				# repos ` prma code ` found , and the archive records + named
				# collections ` prma datasets ` found -- a name with the home page
				# /datasets links it to. On request only -- carried on the
				# whole-board lookup every page load makes , they'd nearly double it
				# for rows that never read them.
				entry[ "code" ]  = row.get( "code_links" ) or []
				entry[ "data" ]  = row.get( "dataset_links" ) or []
				entry[ "names" ] = [ { "name": n , "url": ds_vocab.home( n ) }
					for n in ( row.get( "dataset_names" ) or [] ) ]
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


def _review_state( review ):
	"""One line for the startup banner : what /review has to show right now."""
	st = review.status() if review else { "available": False }
	if not st.get( "available" ):
		return "nothing built yet ; open the page or run ` prma review `"
	n = ( ( review.doc.get( "meta" ) or {} ).get( "counts" ) or {} ).get( "included" , 0 )
	return f"{n} included papers" + ( " ; STALE -- boards moved since" if st.get( "stale" ) else "" )


def _load_review_missing_html():
	"""Read the missing-review page fresh on each request. Falls back to a stub."""
	try:
		return REVIEW_MISSING_HTML_PATH.read_text( encoding="utf-8" )
	except Exception as e:
		return f"<h1>review-missing.html not found</h1><pre>{e}</pre>"


def _load_review_html():
	"""Read the review page fresh on each request. Falls back to a stub."""
	try:
		return REVIEW_HTML_PATH.read_text( encoding="utf-8" )
	except Exception as e:
		return f"<h1>review.html not found</h1><pre>{e}</pre>"


def _load_code_html():
	"""Read the code page fresh on each request. Falls back to a stub."""
	try:
		return CODE_HTML_PATH.read_text( encoding="utf-8" )
	except Exception as e:
		return f"<h1>code.html not found</h1><pre>{e}</pre>"


def _load_datasets_html():
	"""Read the datasets page fresh on each request. Falls back to a stub."""
	try:
		return DATASETS_HTML_PATH.read_text( encoding="utf-8" )
	except Exception as e:
		return f"<h1>datasets.html not found</h1><pre>{e}</pre>"


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


# ---------------------------------------------------------------------------
# /code : every paper ` prma code ` found a repo for , against the curation
# ---------------------------------------------------------------------------
# The question this page exists to answer is a JOIN : which papers that ship
# code are also ones I kept. Every side of it is already in memory --
#
#   the links      the dashboard index's library rows ( indexer.py pins
#                  `code_links` on each , read through code.display_links , so
#                  this is the same list the In-Library "Code" column shows )
#   the sort board BoardState.doc     ( src/db/sortboard.py )
#   the tier list  BoardState.doc     ( src/db/tiers.py )
#   the review     ReviewState.doc    ( output/cache/review.json )
#   the figures    FigureSelectionState ( output/cache/images-state.json )
#
# -- so the payload is a dict merge , not a library walk : no paper record is
# re-read and nothing is re-derived. That is deliberate. ` prma code ` is the
# one place a paper is scanned ( see src/tasks/code.py ) , and every surface
# reads what it pinned , so no two pages can disagree about what code a paper
# has.
#
# One row per LIBRARY paper -- the ones with an EMPTY `code` list included too ,
# because "in the review , ships nothing" is a question worth asking and the
# page's Has-code chip is one click either way. Papers on a board that aren't in
# the library have never been scanned , so they have no answer to give and are
# left out ( the page's footer says so ).


def _fig_counts( state ):
	"""paper key -> how many of its figures are picked , off one figure report's
	curation. Figure ids are "<paper-key>#figure-<N>" ( see src/db/figure_state.py )
	and a paper key never contains '#' , so the split is unambiguous."""
	counts = {}
	for fid in ( ( state.snapshot() if state else {} ).get( "selected" ) or [] ):
		key = str( fid ).rsplit( "#" , 1 )[ 0 ]
		if key:
			counts[ key ] = counts.get( key , 0 ) + 1
	return counts


def _sort_where( doc ):
	"""paper key -> ( "list" | "staged" , tags ) off the sort board. The list is
	read first so a key that somehow appears in both reads as placed."""
	out = {}
	for where , rows in ( ( "list" , ( doc or {} ).get( "items" ) ) ,
	                      ( "staged" , ( doc or {} ).get( "staging" ) ) ):
		for row in ( rows or [] ):
			key = ( row or {} ).get( "key" )
			if key and key not in out:
				out[ key ] = ( where , list( row.get( "tags" ) or [] ) )
	return out


def _tier_where( doc ):
	"""paper key -> ( tier label , tags ) off the tier list. The staging shelf
	reads as "" , the same as the sort board's does , so a shelved paper doesn't
	claim a rank it was never given."""
	out = {}
	for tier in ( ( doc or {} ).get( "tiers" ) or [] ):
		label = tier.get( "label" ) or tier.get( "id" ) or ""
		for row in ( tier.get( "items" ) or [] ):
			key = ( row or {} ).get( "key" )
			if key and key not in out:
				out[ key ] = ( label , list( row.get( "tags" ) or [] ) +
					list( row.get( "mods" ) or [] ) )
	for row in ( ( doc or {} ).get( "staging" ) or [] ):
		key = ( row or {} ).get( "key" )
		if key and key not in out:
			out[ key ] = ( "" , list( row.get( "tags" ) or [] ) +
				list( row.get( "mods" ) or [] ) )
	return out


def _review_where( doc ):
	"""paper key -> ( "included" | "excluded" , detail ) off the built review.
	`detail` is the task category for an included paper and the exclusion reason
	for a dropped one -- the one word each side has to say about why it is
	there. Keys the review never saw are absent ( the paper had no extracted
	text , or is on neither surface the review reads )."""
	out = {}
	for p in ( ( doc or {} ).get( "papers" ) or [] ):
		key = ( p or {} ).get( "key" )
		if key:
			out[ key ] = ( "included" , p.get( "task_category" ) or "" )
	for p in ( ( doc or {} ).get( "excluded" ) or [] ):
		key = ( p or {} ).get( "key" )
		if key and key not in out:
			out[ key ] = ( "excluded" , p.get( "exclusion_reason" ) or "" )
	return out


def _code_rows( dash , sort_board , tiers_board , review , figstate ):
	"""One row per library paper : its identity , the code links ` prma code `
	pinned , and where the four curated surfaces have it. The rows the /code
	page draws and the workbook it exports are both built from this , so the
	sheet can never say something the table didn't."""
	srt   = _sort_where(  ( sort_board.snapshot()  if sort_board  else {} ).get( "doc" ) )
	tier  = _tier_where(  ( tiers_board.snapshot() if tiers_board else {} ).get( "doc" ) )
	rev   = _review_where( getattr( review , "doc" , None ) )
	figs  = _fig_counts( ( figstate or {} ).get( "images" ) )
	mfigs = _fig_counts( ( figstate or {} ).get( "method-images" ) )

	rows = []
	for e in ( getattr( dash , "library" , None ) or [] ):
		key = e.get( "key" )
		if not key:
			continue
		links     = e.get( "code_links" ) or []
		s_where , s_tags = srt.get(  key , ( "" , [] ) )
		t_where , t_tags = tier.get( key , ( "" , [] ) )
		r_where , r_why  = rev.get(  key , ( "" , "" ) )
		rows.append( {
			"key":        key ,
			"title":      e.get( "title" ) or "(untitled)" ,
			"doi":        e.get( "doi" ) or "" ,
			"year":       e.get( "year" ) ,
			"cited_by":   e.get( "cited_by" ) ,
			"added":      ( e.get( "created_at" ) or "" )[ :10 ] ,
			"published":  e.get( "pubdate" ) or "" ,
			"pdf":        bool( e.get( "pdf" ) ) ,
			"has_md":     bool( e.get( "has_md" ) ) ,
			"montage":    e.get( "montage" ) or "" ,
			# The links themselves , already compacted for the browser by
			# code.display_links -- { url , source , raw? } , where `raw` is the
			# OCR-mangled original a repair replaced.
			"code":       links ,
			"sources":    sorted( { l.get( "source" ) or "link" for l in links } ) ,
			"review":     r_where ,      # "" | "included" | "excluded"
			"review_why": r_why ,        # task category , or why it was dropped
			"sort":       s_where ,      # "" | "list" | "staged"
			"tier":       t_where ,      # tier label , "" when unranked / absent
			"figures":    figs.get(  key , 0 ) ,
			"mfigures":   mfigs.get( key , 0 ) ,
			"tags":       sorted( { t for t in ( s_tags + t_tags ) if t } ) ,
		} )
	return rows


def _code_payload( dash , sort_board , tiers_board , review , figstate ):
	"""GET /api/code . Carries the index's own state as well as the rows : the
	library pool is what every row comes from , so a page that opened before the
	index finished building needs to know to poll rather than draw an empty
	table."""
	status = getattr( dash , "status" , "idle" )
	rows   = _code_rows( dash , sort_board , tiers_board , review , figstate ) \
		if status == "ready" else []
	with_code = sum( 1 for r in rows if r[ "code" ] )
	return {
		"ok"       : True ,
		"status"   : status ,                       # idle | building | ready | error
		"message"  : getattr( dash , "message" , "" ) ,
		"error"    : getattr( dash , "error" , "" ) ,
		"built_at" : getattr( dash , "built_at" , None ) ,
		"review"   : ( review.status() if review else { "available": False } ) ,
		"counts"   : {
			"papers"    : len( rows ) ,
			"with_code" : with_code ,
			"links"     : sum( len( r[ "code" ] ) for r in rows ) ,
			"in_review" : sum( 1 for r in rows if r[ "code" ] and r[ "review" ] == "included" ) ,
		} ,
		"rows"     : rows ,
	}


# The exported workbook. Two sheets over the same rows the page is showing :
# one line per PAPER , and one line per ( paper , link ) for when the repo is
# the thing you are counting. Column order matches the page's own , left to
# right , so the sheet reads like the table it came from.
_CODE_PAPER_HEADERS = ( "DOI / Key" , "Title" , "Year" , "Added" , "Published" ,
                        "Cited By" , "# Links" , "Sources" , "Code URLs" ,
                        "Review" , "Review Detail" , "Sort" , "Tier" ,
                        "Figures" , "Design Figures" , "Tags" , "PDF" , "Proxy" )
_CODE_LINK_HEADERS  = ( "DOI / Key" , "Title" , "Year" , "Source" , "URL" ,
                        "OCR-repaired from" , "Review" , "Sort" , "Tier" ,
                        "Figures" , "Tags" )


def _code_workbook_bytes( rows ):
	"""Build the /code export in memory and hand back the .xlsx bytes.
	openpyxl saves to any file-like object , so this never touches disk -- the
	one workbook that IS written to disk is ` prma code `'s own
	output/code/code.xlsx , which is a different ( whole-library ) rollup.

	The proxy prefix comes from the task that owns it rather than being spelled
	out again here , the same way the pages take it from one const in
	/static/common.js : a sheet is the one surface with no JS to build its links
	in , so this is where the Python copy is read."""
	import io
	from ..tasks.code import _PROXY_URL_TEMPLATE

	paper_rows , link_rows = [] , []
	for r in rows:
		doi   = r.get( "doi" ) or ""
		ident = utils.Link( doi , f"https://doi.org/{doi}" ) if doi else ( r.get( "key" ) or "" )
		proxy_url = _PROXY_URL_TEMPLATE.format( doi=doi ) if doi else ""
		proxy = utils.Link( proxy_url , proxy_url ) if proxy_url else ""
		links = r.get( "code" ) or []
		tags  = " , ".join( r.get( "tags" ) or [] )
		paper_rows.append( [
			ident , r.get( "title" ) or "" , r.get( "year" ) or "" ,
			r.get( "added" ) or "" , r.get( "published" ) or "" ,
			r.get( "cited_by" ) if r.get( "cited_by" ) is not None else "" ,
			len( links ) , " , ".join( r.get( "sources" ) or [] ) ,
			"\n".join( l.get( "url" ) or "" for l in links ) ,
			r.get( "review" ) or "" , r.get( "review_why" ) or "" ,
			r.get( "sort" ) or "" , r.get( "tier" ) or "" ,
			r.get( "figures" ) or 0 , r.get( "mfigures" ) or 0 , tags ,
			"yes" if r.get( "pdf" ) else "" , proxy ,
		] )
		for l in links:
			url = l.get( "url" ) or ""
			link_rows.append( [
				ident , r.get( "title" ) or "" , r.get( "year" ) or "" ,
				l.get( "source" ) or "" , utils.Link( url , url ) ,
				l.get( "raw" ) or "" ,
				r.get( "review" ) or "" , r.get( "sort" ) or "" ,
				r.get( "tier" ) or "" , r.get( "figures" ) or 0 , tags ,
			] )

	buf = io.BytesIO()
	utils.write_xlsx( buf , [
		( "Papers" , _CODE_PAPER_HEADERS , paper_rows ) ,
		( "Links"  , _CODE_LINK_HEADERS  , link_rows  ) ,
	] )
	return buf.getvalue()


# ---------------------------------------------------------------------------
# /datasets : the PUBLIC DATA every paper stands on , against the curation
# ---------------------------------------------------------------------------
# /code's sibling , and the same join with the other half of the question. A
# deep-learning fMRI paper's data is almost never the authors' own upload : it
# is somebody else's public collection , cited two different ways --
#
#   as a LINK   an archive record ( openneuro.org/datasets/ds000105 , a Zenodo
#               DOI , an OSF node , a Hugging Face dataset ) , harvested out of
#               the abstract + OCR text by ` prma datasets ` and pinned on the
#               record ( see src/tasks/datasets.py )
#   as a NAME   "we use HCP" , with nothing to click -- matched against the same
#               vocabulary /review screens with ( src/review/datasets.py ) , so a
#               paper's datasets read the same on both pages
#
# Both are already on the record , and the four curated surfaces are already in
# memory , so this is the same dict merge _code_rows is : nothing is re-read and
# nothing is re-derived. One row per LIBRARY paper , the empty ones included --
# "in the review , names no public data" is a question worth asking , and the
# page's Uses-data chip is one click either way.
#
# The BY-DATASET pivot -- one row per collection , with the papers that use it --
# is the page's own work , not this payload's : it is a regrouping of these
# rows , and the browser is where prma decides how things are presented.


from ..review import datasets as ds_vocab      # the vocabulary : homes + descriptions

# An ARCHIVE record says what it is out of two caches , neither of them built
# here and neither of them touched by a request :
#
#   output/cache/datasets/  ` prma datasets ` fetches every unique record once --
#                           Zenodo / Figshare / OpenNeuro / OSF through their own
#                           APIs , everything else by the page's <title> .
#   output/cache/osf/       ` prma code ` already fetched every OSF node among the
#                           CODE links , which covers records the dataset scan
#                           might never have reached.
#
# Both are read as a whole directory and memoized on its mtime : /api/datasets is
# a per-request dict merge , and re-reading a few hundred small files on every
# load would be silly. A record in neither shows its URL and no description ,
# which is the honest answer -- nobody has asked yet.
_DS_NOTES  = { "mtime": None , "map": {} }
_OSF_NOTES = { "mtime": None , "map": {} }


def _record_titles( args ):
	"""{ canonical-url : title } off what ` prma datasets ` fetched. {} until it
	has been run , or on any read error."""
	try:
		d = Path( args.output ).joinpath( "cache" , "datasets" )
		mt = d.stat().st_mtime if d.is_dir() else None
	except Exception:
		return {}
	if mt is None:
		return {}
	if _DS_NOTES[ "mtime" ] == mt:
		return _DS_NOTES[ "map" ]
	from ..tasks.code import _dedup_key
	out = {}
	for fp in d.glob( "*.json" ):
		try:
			rec = utils.read_json( fp ) or {}
		except Exception:
			continue
		url , title = rec.get( "url" ) or "" , ( rec.get( "title" ) or "" ).strip()
		if url and title:
			out[ _dedup_key( url ) ] = title[ :300 ]
	_DS_NOTES.update( mtime=mt , map=out )
	return out


def _osf_titles( args ):
	"""{ guid : human title } off whatever ` prma code ` has cached. {} when the
	cache doesn't exist , when OSF was never configured , or on any read error --
	a missing description is a blank line , never a failed request."""
	try:
		d = Path( args.output ).joinpath( "cache" , "osf" )
		mt = d.stat().st_mtime if d.is_dir() else None
	except Exception:
		return {}
	if mt is None:
		return {}
	if _OSF_NOTES[ "mtime" ] == mt:
		return _OSF_NOTES[ "map" ]
	out = {}
	for fp in d.glob( "*.json" ):
		try:
			rec = utils.read_json( fp ) or {}
		except Exception:
			continue
		if rec.get( "status" ) != "ok":
			continue
		title = ( rec.get( "title" ) or "" ).strip()
		if not title:
			# No title , but a description is still an answer -- take its first
			# sentence rather than dumping an abstract into a table row.
			body = " ".join( ( rec.get( "description" ) or "" ).split() )
			title = ( body.split( ". " )[ 0 ] + "." ) if body else ""
		if title:
			out[ rec.get( "guid" ) or fp.stem ] = title[ :300 ]
	_OSF_NOTES.update( mtime=mt , map=out )
	return out


def _link_desc( url , records , osf_titles ):
	"""What an archive record IS , read off the caches above -- never fetched
	here. The dataset scan's own answer wins ; ` prma code `'s OSF cache is the
	fallback , since it may hold a node this scan never saw."""
	if records:
		from ..tasks.code import _dedup_key
		t = records.get( _dedup_key( url ) )
		if t:
			return t
	if not osf_titles:
		return ""
	try:
		from ..osf.osf import parse_node
		guid = parse_node( url )
	except Exception:
		return ""
	return osf_titles.get( guid , "" ) if guid else ""


def _dataset_rows( dash , sort_board , tiers_board , review , figstate ):
	"""One row per library paper : its identity , the public-dataset evidence
	` prma datasets ` pinned , and where the four curated surfaces have it. The
	page's tables and the workbook it exports are both built from this."""
	srt   = _sort_where(  ( sort_board.snapshot()  if sort_board  else {} ).get( "doc" ) )
	tier  = _tier_where(  ( tiers_board.snapshot() if tiers_board else {} ).get( "doc" ) )
	rev   = _review_where( getattr( review , "doc" , None ) )
	figs  = _fig_counts( ( figstate or {} ).get( "images" ) )
	mfigs = _fig_counts( ( figstate or {} ).get( "method-images" ) )
	_a    = getattr( dash , "args" , None )
	recs  = _record_titles( _a ) if _a else {}
	osft  = _osf_titles( _a )    if _a else {}

	rows = []
	for e in ( getattr( dash , "library" , None ) or [] ):
		key = e.get( "key" )
		if not key:
			continue
		# Each archive record travels with what it IS , where we know -- read
		# time again , same as the homes below , so nothing has to be re-scanned
		# when the OSF cache grows.
		links     = [ dict( l , desc=_link_desc( l.get( "url" ) or "" , recs , osft ) )
			for l in ( e.get( "dataset_links" ) or [] ) ]
		# A named collection travels with WHERE IT LIVES , joined at read time
		# off the vocabulary rather than baked into the scan -- so adding a home
		# for a dataset is a one-line table edit that takes effect on the next
		# request , with no re-scan and no reindex.
		names     = [ { "name": n , "url": ds_vocab.home( n ) ,
			"desc": ds_vocab.describe( n ) }
			for n in ( e.get( "dataset_names" ) or [] ) ]
		s_where , s_tags = srt.get(  key , ( "" , [] ) )
		t_where , t_tags = tier.get( key , ( "" , [] ) )
		r_where , r_why  = rev.get(  key , ( "" , "" ) )
		rows.append( {
			"key":        key ,
			"title":      e.get( "title" ) or "(untitled)" ,
			"doi":        e.get( "doi" ) or "" ,
			"year":       e.get( "year" ) ,
			"cited_by":   e.get( "cited_by" ) ,
			"added":      ( e.get( "created_at" ) or "" )[ :10 ] ,
			"published":  e.get( "pubdate" ) or "" ,
			"pdf":        bool( e.get( "pdf" ) ) ,
			"has_md":     bool( e.get( "has_md" ) ) ,
			"montage":    e.get( "montage" ) or "" ,
			# The archive records , compacted for the browser by
			# datasets.display_links -- { url , source , accession? } , where
			# `accession` marks a link minted from a bare id the paper printed
			# without a URL ( ` ds000105 ` ).
			"data":       links ,
			# The collections the paper NAMES but never links --
			# { name , url , desc } , where url / desc are "" for a name we have
			# no checked home page or one-liner for.
			"names":      names ,
			"sources":    sorted( { l.get( "source" ) or "archive" for l in links } ) ,
			# How many repos ` prma code ` found -- a count , not the links : it
			# is here so "uses public data AND ships code" is a sort on this
			# page , while the links themselves stay /code's to render.
			"code":       len( e.get( "code_links" ) or [] ) ,
			"review":     r_where ,      # "" | "included" | "excluded"
			"review_why": r_why ,        # task category , or why it was dropped
			"sort":       s_where ,      # "" | "list" | "staged"
			"tier":       t_where ,      # tier label , "" when unranked / absent
			"figures":    figs.get(  key , 0 ) ,
			"mfigures":   mfigs.get( key , 0 ) ,
			"tags":       sorted( { t for t in ( s_tags + t_tags ) if t } ) ,
		} )
	return rows


def _datasets_payload( dash , sort_board , tiers_board , review , figstate ):
	"""GET /api/datasets . Same contract as /api/code : the index's own state
	travels with the rows , because the library pool they come from builds
	lazily and a page that opened first needs to know to poll."""
	status = getattr( dash , "status" , "idle" )
	rows   = _dataset_rows( dash , sort_board , tiers_board , review , figstate ) \
		if status == "ready" else []
	with_data = sum( 1 for r in rows if r[ "data" ] or r[ "names" ] )
	distinct  = set()
	for r in rows:
		distinct.update( n[ "name" ] for n in r[ "names" ] )
		distinct.update( l.get( "url" ) or "" for l in r[ "data" ] )
	return {
		"ok"       : True ,
		"status"   : status ,                       # idle | building | ready | error
		"message"  : getattr( dash , "message" , "" ) ,
		"error"    : getattr( dash , "error" , "" ) ,
		"built_at" : getattr( dash , "built_at" , None ) ,
		"review"   : ( review.status() if review else { "available": False } ) ,
		"counts"   : {
			"papers"    : len( rows ) ,
			"with_data" : with_data ,
			"links"     : sum( len( r[ "data" ] ) for r in rows ) ,
			"named"     : sum( len( r[ "names" ] ) for r in rows ) ,
			"distinct"  : len( distinct ) ,
			"in_review" : sum( 1 for r in rows
				if ( r[ "data" ] or r[ "names" ] ) and r[ "review" ] == "included" ) ,
		} ,
		"rows"     : rows ,
	}


# The exported workbook. Two sheets over the rows the page is showing : one line
# per PAPER , and one line per ( paper , dataset ) for when the collection is the
# thing you are counting -- archive links and named datasets in the same sheet ,
# told apart by a Kind column , because "which papers used HCP" shouldn't depend
# on whether they happened to print a URL for it.
_DS_PAPER_HEADERS = ( "DOI / Key" , "Title" , "Year" , "Added" , "Published" ,
                      "Cited By" , "# Datasets" , "# Archive Links" , "# Named" ,
                      "Archives" , "Dataset URLs" , "Named Datasets" , "# Code Links" ,
                      "Review" , "Review Detail" , "Sort" , "Tier" ,
                      "Figures" , "Design Figures" , "Tags" , "PDF" , "Proxy" )
_DS_ITEM_HEADERS  = ( "Dataset" , "Kind" , "Description" , "Archive" , "URL" ,
                      "From Accession" , "DOI / Key" , "Title" , "Year" , "Review" ,
                      "Sort" , "Tier" , "Figures" , "Tags" )


def _datasets_workbook_bytes( rows ):
	"""Build the /datasets export in memory and hand back the .xlsx bytes.
	openpyxl saves to any file-like object , so this never touches disk --
	` prma datasets ` writes no workbook of its own ( the scan IS the answer ) ,
	which makes this the only place the rollup exists.

	The proxy prefix comes from the task that owns it rather than being spelled
	out again , the same way the pages take it from one const in
	/static/common.js ."""
	import io
	from ..tasks.code import _PROXY_URL_TEMPLATE

	paper_rows , item_rows = [] , []
	for r in rows:
		doi   = r.get( "doi" ) or ""
		ident = utils.Link( doi , f"https://doi.org/{doi}" ) if doi else ( r.get( "key" ) or "" )
		proxy_url = _PROXY_URL_TEMPLATE.format( doi=doi ) if doi else ""
		proxy = utils.Link( proxy_url , proxy_url ) if proxy_url else ""
		links = r.get( "data" ) or []
		names = r.get( "names" ) or []
		tags  = " , ".join( r.get( "tags" ) or [] )
		paper_rows.append( [
			ident , r.get( "title" ) or "" , r.get( "year" ) or "" ,
			r.get( "added" ) or "" , r.get( "published" ) or "" ,
			r.get( "cited_by" ) if r.get( "cited_by" ) is not None else "" ,
			len( links ) + len( names ) , len( links ) , len( names ) ,
			" , ".join( r.get( "sources" ) or [] ) ,
			"\n".join( l.get( "url" ) or "" for l in links ) ,
			"\n".join( n[ "name" ] for n in names ) , r.get( "code" ) or 0 ,
			r.get( "review" ) or "" , r.get( "review_why" ) or "" ,
			r.get( "sort" ) or "" , r.get( "tier" ) or "" ,
			r.get( "figures" ) or 0 , r.get( "mfigures" ) or 0 , tags ,
			"yes" if r.get( "pdf" ) else "" , proxy ,
		] )
		tail = [ ident , r.get( "title" ) or "" , r.get( "year" ) or "" ,
			r.get( "review" ) or "" , r.get( "sort" ) or "" , r.get( "tier" ) or "" ,
			r.get( "figures" ) or 0 , tags ]
		for l in links:
			url = l.get( "url" ) or ""
			item_rows.append( [ url , "archive link" , l.get( "desc" ) or "" ,
				l.get( "source" ) or "" , utils.Link( url , url ) ,
				l.get( "accession" ) or "" , *tail ] )
		for n in names:
			item_rows.append( [ n[ "name" ] , "named" , n.get( "desc" ) or "" , "" ,
				utils.Link( n[ "url" ] , n[ "url" ] ) if n[ "url" ] else "" ,
				"" , *tail ] )

	buf = io.BytesIO()
	utils.write_xlsx( buf , [
		( "Papers"   , _DS_PAPER_HEADERS , paper_rows ) ,
		( "Datasets" , _DS_ITEM_HEADERS  , item_rows  ) ,
	] )
	return buf.getvalue()

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
		# Recently-added ( see recent() ) : its own little cache , because it is
		# a SECOND read of the same source and a ~0.4s SQLite copy each time.
		self._recent: Optional[ List[ Dict ] ] = None
		self._recent_sig: Optional[ Tuple[ float , ... ] ] = None
		self._recent_n  = 0      # how many rows the cached read asked for
		self._recent_at = 0.0    # when it ran , for the no-watch-files TTL path
		self._recent_note = ""

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



	def recent( self , limit=10 ):
		"""The newest additions to the library , straight off the manager --
		what /sort's " Recently added " offers. Returns ( rows , note ) ; see
		tasks.snapshot.recent for the row shape.

		Cached on the SAME source signature the title / DOI sets above use , so
		clicking the button repeatedly is free while Zotero sits still and picks
		up a paper the moment one is saved. A bigger `limit` than the cached
		read covers re-reads ; a smaller one is served by slicing it."""
		# Clamped here rather than only in the reader , so a silly ?limit= can't
		# poison the cache's idea of how much it holds.
		try:    limit = max( 1 , min( int( limit ) , 200 ) )
		except ( TypeError , ValueError ): limit = 10
		now = time.time()
		sig = self._source_sig()
		fresh = (
			self._recent is not None
			and self._recent_n >= limit
			and ( sig == self._recent_sig if sig is not None
				else ( now - self._recent_at ) < self.ttl )
		)
		if not fresh:
			# Over-read a little so nudging the count up ( 10 -> 25 ) usually
			# answers out of the cache instead of re-copying the SQLite.
			want = max( limit , 25 )
			rows , note = snap_module.recent( self.args , want )
			self._recent      = rows
			self._recent_note = note
			self._recent_n    = want
			self._recent_sig  = sig
			self._recent_at   = now
		return self._recent[ :limit ] , self._recent_note


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
		seq = [ "openalex" , "yolo" , "ocr" , "images" , "methods" , "code" , "md" ,
		        "datasets" , "modalities" ]
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


def _refs_payload( dash , text ):
	"""Somebody else's bibliography , resolved against this library.

	src/db/refparse.py does the READING -- a numbered AMA list , an APA one , a
	.docx paragraph per reference -- and this is the half that needs the index :
	every parsed reference is looked up in the library , then in the
	missing-paper pools , so the row that reaches a board carries the same KEY
	the rest of prma uses for that paper. That key is what makes its DOI / PDF /
	MD / Methods links work , and what lets the page tell "already on the board"
	from "new" by identity instead of by spelling.

	Matching follows the same policy as /exists , for the same reason : a DOI
	settles it outright , and a title is only fuzzy-matched when there is no DOI
	to go on -- a reference whose DOI we don't have is a paper we don't have ,
	and letting a near-enough title overrule that would staple it to the wrong
	paper silently.

	Never a partial answer : a reference that matches nothing still comes back ,
	flagged in_library=false with whatever the parse got , because the staging
	shelf is exactly where a row you have to fix by hand belongs."""
	from ..db import refparse

	refs = refparse.parse( text )
	if not refs:
		return { "ok": True , "refs": [] }

	def index( rows ):
		by_doi , by_title , titles = {} , {} , []
		for r in ( rows or [] ):
			d = utils.normalize_doi( r.get( "doi" ) or "" )
			if d:
				by_doi.setdefault( d , r )
			t = utils.normalize_title( r.get( "title" ) or "" )
			if t and t not in by_title:
				by_title[ t ] = r
				titles.append( t )
		return by_doi , by_title , titles

	def match( doi , title , idx ):
		by_doi , by_title , titles = idx
		if doi:
			return ( by_doi[ doi ] , "doi" ) if doi in by_doi else ( None , "" )
		if not title:
			return None , ""
		if title in by_title:
			return by_title[ title ] , "title"
		hit = process.extractOne( title , titles , scorer=fuzz.token_sort_ratio ,
			score_cutoff=TITLE_THRESHOLD )
		return ( by_title[ hit[ 0 ] ] , "fuzzy" ) if hit else ( None , "" )

	lib_idx = index( getattr( dash , "library" , None ) )
	# Everything you DON'T have but know about : a cited work carries a WID and ,
	# often , an open-access pdf url -- so even an unmatched reference can arrive
	# with working links.
	ext_idx = index( ( getattr( dash , "references" , None ) or [] ) +
	                 ( getattr( dash , "cited_by"   , None ) or [] ) )

	out = []
	for r in refs:
		doi   = utils.normalize_doi( r.get( "doi" ) or "" ) or ""
		ntit  = utils.normalize_title( r.get( "title" ) or "" )
		row , how = match( doi , ntit , lib_idx )
		in_lib    = bool( row )
		if not row:
			row , how = match( doi , ntit , ext_idx )
		row = row or {}
		entry = {
			"n":       r.get( "n" ) ,
			"raw":     r.get( "raw" ) ,
			"authors": r.get( "authors" ) or "" ,
			# The library's title when we matched one : that's the canonical
			# spelling , and seeing it is how you check a fuzzy match was right.
			"title":   row.get( "title" ) or r.get( "title" ) or "" ,
			"doi":     doi or ( row.get( "doi" ) or "" ) ,
			"year":    r.get( "year" ) or row.get( "year" ) ,
			"journal": r.get( "journal" ) or row.get( "journal" ) or "" ,
			"url":     r.get( "url" ) or "" ,
			"wid":     row.get( "wid" ) or "" ,
			# A library row's `pdf` is a LOCAL path the page must ask /pdf?key=
			# for , so it never travels ; an external row's is a url , so it does.
			"pdf":     "" if in_lib else ( row.get( "pdf" ) or "" ) ,
			"in_library": in_lib ,
			"matched":    how ,
		}
		entry[ "key" ] = ( row.get( "key" ) or entry[ "doi" ] or entry[ "wid" ]
			or refparse.synth_key( entry[ "title" ] or entry[ "raw" ] ) )
		out.append( entry )
	return { "ok": True , "refs": out }


def _disposition( kind , filename ):
	"""A Content-Disposition value that can't break the response. http.server
	writes headers as latin-1 , and a Zotero PDF is named after its title --
	curly quotes , en dashes , accents and all -- so a raw name raised half-way
	through the headers and the browser got an empty reply. The plain `filename`
	is an ASCII stand-in ; `filename*` ( RFC 6266 ) carries the real name , and
	it is the one every current browser uses."""
	name  = str( filename or "download" )
	plain = re.sub( r'[^\x20-\x7e]|["\\]' , "_" , name )
	return f"{kind}; filename=\"{plain}\"; filename*=UTF-8''{quote( name , safe='' )}"


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
	# What those two boards add up to , screened and field-extracted ( /review ) ,
	# and the same screen run over the papers you do NOT have ( /review-missing ).
	# Both injected at startup ; None in minimal mode.
	review: "ReviewState" = None
	review_missing: "MissingReviewState" = None
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

	def _send_download( self , data , filename , ctype="application/octet-stream" ):
		"""Hand back bytes as a file to SAVE rather than to render -- the /code
		page's xlsx export. It arrives on a POST ( the key list of what you are
		looking at is far too long for a query string ) , so the page reads the
		body as a blob and clicks its own link ; the disposition is what names
		the file when it does."""
		self.send_response( 200 )
		self.send_header( "Content-Type"        , ctype )
		self.send_header( "Content-Length"      , str( len( data ) ) )
		self.send_header( "Content-Disposition" , _disposition( "attachment" , filename ) )
		self.send_header( "Cache-Control"       , "no-store" )
		self.end_headers()
		self.wfile.write( data )

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

	def _send_static( self , base , rel , no_cache=False ):
		"""Serve a file under `base` ( e.g. output/images/ ) by relative path ,
		with a path-traversal guard so only files actually inside `base` are
		reachable. Used for the figure crops / montages that ` prma md ` links
		and the 'Figures' column points at.

		`no_cache` is for the shared page chrome under /static/ : those are files
		you EDIT , and the pages themselves are re-read from disk on every load
		( _load_dashboard_html and friends ) , so the stylesheet has to refresh
		on a reload too or half the page would still be the old one. The figure
		crops , which are content , keep the browser's normal caching."""
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
		if no_cache:
			self.send_header( "Cache-Control" , "no-cache, must-revalidate" )
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
		self.send_header( "Content-Disposition" , _disposition( "inline" , Path( pdf ).name ) )
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

		if path.startswith( "/static/" ):
			# The chrome every page links : common.css , common.js , boot.js .
			# no-cache because they're hand-edited alongside the pages that load
			# them , and those are re-read from disk on every request.
			self._send_static( STATIC_DIR , path[ len( "/static/" ): ] , no_cache=True )
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

		if path in ( "/review" , "/review.html" ):
			self._send_html( 200 , _load_review_html() )
			return

		if path in ( "/review-missing" , "/review-missing.html" ):
			self._send_html( 200 , _load_review_missing_html() )
			return

		if path in ( "/code" , "/code.html" ):
			self._send_html( 200 , _load_code_html() )
			return

		if path in ( "/datasets" , "/datasets.html" ):
			self._send_html( 200 , _load_datasets_html() )
			return

		if path == "/api/datasets":
			# Every library paper's public-dataset evidence joined against the
			# four curated surfaces. Same contract as /api/code below : cheap
			# ( a dict merge over what is already in memory ) , but the library
			# pool it reads builds lazily , so pick up a fresher ` prma reindex ` ,
			# start a build if there has never been one , and answer with the
			# status so the page polls instead of drawing an empty table.
			self.dash.maybe_reload()
			if self.dash.status == "idle":
				self.dash.ensure_build( refresh=False )
			self._send_json( 200 , _datasets_payload( self.dash , self.sort ,
				self.tiers , self.review , self.figstate ) )
			return

		if path == "/api/code":
			# Every library paper's code links joined against the four curated
			# surfaces. Built from what is already in memory ( see _code_rows ) ,
			# so it is cheap enough to serve on every load -- EXCEPT that the
			# library pool it reads is the dashboard index , which builds lazily.
			# Same contract as /api/meta : pick up a fresher ` prma reindex ` , and
			# start a build if there has never been one , then answer with the
			# status so the page polls instead of drawing an empty table.
			self.dash.maybe_reload()
			if self.dash.status == "idle":
				self.dash.ensure_build( refresh=False )
			self._send_json( 200 , _code_payload( self.dash , self.sort ,
				self.tiers , self.review , self.figstate ) )
			return

		if path == "/api/review":
			# The whole review document. Big ( a few MB ) and derived , so it is
			# served from what was last BUILT -- never built inline , which would
			# hold the request open for minutes. When the boards have moved since ,
			# the payload still comes back , flagged stale ; kicking off the
			# rebuild is the page's call ( POST /api/review/rebuild ) , because it
			# is the one that knows whether anybody is looking.
			self._send_json( 200 , self.review.snapshot() )
			return

		if path == "/api/review/version":
			# The cheap poll : status only , no document. Carries `building` +
			# progress while a rebuild runs and `generated` when it lands , which
			# is how the page knows to re-fetch.
			self._send_json( 200 , self.review.status() )
			return

		if path == "/api/review-missing":
			# The /review-missing document. Same contract as /api/review above ,
			# and capped at build time ( see missing.MAX_PAPERS ) precisely so this
			# stays a payload a browser can hold : the pool behind it is ~140,000
			# papers , and serving all of them would be the pool , not a page.
			self._send_json( 200 , self.review_missing.snapshot() )
			return

		if path == "/api/review-missing/version":
			self._send_json( 200 , self.review_missing.status() )
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
			# as the figure reports' /api/<mode>/version ). The view-only latch
			# rides along on it : this poll is the ONLY thing an idle tab does , so
			# it is also the only way a board locked from another browser reaches
			# the tabs that were already open when it happened.
			snap = board.snapshot()
			self._send_json( 200 , { "rev": snap[ "rev" ] , "locked": snap[ "locked" ] } )
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

		if path == "/api/recent":
			# The newest papers in the reference manager , read off the manager
			# ITSELF rather than off the dashboard index ( see
			# SnapshotCache.recent ). That is the whole point of the route : the
			# index only learns about a paper after a snapshot has pushed it into
			# output/cache/papers/ and a reindex has run , so a paper saved into
			# Zotero a minute ago is not searchable yet -- but /sort can still
			# offer it here , with the key it WILL have once the pipeline catches
			# up. `note` explains an empty list when there is something to say.
			rows , note = self.cache.recent( self._arg_int( self._qs() , "limit" , 10 ) )
			self._send_json( 200 , { "results": rows , "total": len( rows ) ,
				"note": note , "manager": getattr( self.dash.args , "manager" , "" ) } )
			return

		if path == "/api/search":
			qs = self._qs()
			# Pick up a ` prma reindex ` that ran in another process since the
			# last query ( a bare mtime stat when nothing moved ) , so a board
			# left open overnight searches the current index instead of the one
			# the server started with.
			self.dash.maybe_reload()
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

		if urlparse( self.path ).path == "/api/datasets/export.xlsx":
			# The /datasets table as a workbook , same contract as /code's below :
			# the body is { "keys": [ ... ] } -- the rows the page is SHOWING , in
			# the order it is showing them -- so the sheet is the view you built.
			# No keys , or no body at all , exports every paper that stands on
			# some public data.
			try:
				length = int( self.headers.get( "Content-Length" , "0" ) )
				body   = self.rfile.read( length ) if length > 0 else b"{}"
				data   = json.loads( body.decode( "utf-8" , errors="replace" ) )
				rows   = _dataset_rows( self.dash , self.sort , self.tiers ,
					self.review , self.figstate )
				keys   = data.get( "keys" )
				if isinstance( keys , list ) and keys:
					by_key = { r[ "key" ]: r for r in rows }
					rows   = [ by_key[ k ] for k in keys if k in by_key ]
				else:
					rows = [ r for r in rows if r[ "data" ] or r[ "names" ] ]
				stamp = time.strftime( "%Y%m%d" )
				self._send_download( _datasets_workbook_bytes( rows ) ,
					f"prma-datasets-{stamp}.xlsx" ,
					"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" )
			except Exception as e:
				self._send_json( 500 , { "ok": False , "error": str( e ) } )
			return

		if urlparse( self.path ).path == "/api/code/export.xlsx":
			# The /code table as a workbook. The body is { "keys": [ ... ] } -- the
			# rows the page is SHOWING , in the order it is showing them , so the
			# sheet is the view you built rather than the whole library again
			# ( that one is ` prma code `'s output/code/code.xlsx ). No keys , or
			# no body at all , exports every paper that has a link.
			try:
				length = int( self.headers.get( "Content-Length" , "0" ) )
				body   = self.rfile.read( length ) if length > 0 else b"{}"
				data   = json.loads( body.decode( "utf-8" , errors="replace" ) )
				rows   = _code_rows( self.dash , self.sort , self.tiers ,
					self.review , self.figstate )
				keys   = data.get( "keys" )
				if isinstance( keys , list ) and keys:
					by_key = { r[ "key" ]: r for r in rows }
					rows   = [ by_key[ k ] for k in keys if k in by_key ]
				else:
					rows = [ r for r in rows if r[ "code" ] ]
				stamp = time.strftime( "%Y%m%d" )
				self._send_download( _code_workbook_bytes( rows ) ,
					f"prma-code-{stamp}.xlsx" ,
					"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" )
			except Exception as e:
				self._send_json( 500 , { "ok": False , "error": str( e ) } )
			return

		if urlparse( self.path ).path == "/api/review/rebuild":
			# Re-screen and re-extract from the boards as they stand now. Minutes
			# of work , so it runs on its own thread and this returns immediately
			# with the status the page then polls ; a second press while one is
			# running is a no-op that reports the build already under way.
			self._send_json( 200 , { "ok": True , **self.review.rebuild() } )
			return

		if urlparse( self.path ).path == "/api/review-missing/rebuild":
			# The same , over the OpenAlex pool. It reads the dashboard index , so
			# nudge that into building first when nobody has opened the dashboard
			# yet this run -- otherwise the builder falls back to reading the index
			# off disk , which works but pays for it twice.
			if self.dash is not None and self.dash.status == "idle":
				self.dash.ensure_build( refresh=False )
			self._send_json( 200 , { "ok": True , **self.review_missing.rebuild() } )
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
			except BoardLocked:
				# View-only ( src/db/boardlock.py ). The page already knows and has
				# greyed itself out ; what lands here is a tab that was open BEFORE
				# the lock -- including the beacon it fires on the way out -- so the
				# answer carries the latch and the page flips instead of retrying.
				self._send_json( 403 , { "ok": False , "locked": True ,
					"error": "this board is in view-only mode" } )
			except Exception as e:
				self._send_json( 500 , { "ok": False , "error": str( e ) } )
			return

		board = self._board( urlparse( self.path ).path , "/lock" )
		if board:
			# Flip a board into / out of view-only mode , for EVERYONE ( body :
			# { "locked": bool } ). Its own endpoint rather than a field in the
			# document because the document is written whole : storing the latch
			# inside it would need a write of the very thing the latch forbids.
			try:
				length = int( self.headers.get( "Content-Length" , "0" ) )
				body   = self.rfile.read( length ) if length > 0 else b"{}"
				data   = json.loads( body.decode( "utf-8" , errors="replace" ) )
				self._send_json( 200 , { "ok": True , **board.set_lock( data.get( "locked" ) ) } )
			except Exception as e:
				self._send_json( 500 , { "ok": False , "error": str( e ) } )
			return

		if self.path in ( "/api/paper-meta" , "/api/tiers/meta" ):
			# Per-row lookup for the board pages :
			# { "keys": [ ... ] , "mods": bool , "links": bool }.
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
					keys , want_mods=bool( data.get( "mods" ) ) ,
					want_links=bool( data.get( "links" ) ) ) } )
			except Exception as e:
				self._send_json( 500 , { "ok": False , "error": str( e ) } )
			return

		if self.path == "/api/refs/parse":
			# Somebody else's reference list -> rows the boards can stage. The
			# body is either the FILE the page picked ( a .docx , which is a zip ,
			# or a .txt ) posted as its own bytes , or { "text": "<pasted>" } --
			# one route for both because they arrive as the same block of text
			# ( refparse.text_from_upload sorts out which it got ) and neither is
			# worth a multipart parser.
			try:
				length = int( self.headers.get( "Content-Length" , "0" ) )
				body   = self.rfile.read( length ) if length > 0 else b""
				from ..db import refparse
				self._send_json( 200 , _refs_payload(
					self.dash , refparse.text_from_upload( body ) ) )
			except ValueError as e:
				# The user dropped the wrong kind of file. refparse raises these
				# already phrased for a person , so pass it straight through.
				self._send_json( 400 , { "ok": False , "error": str( e ) } )
			except Exception as e:
				self._send_json( 500 , { "ok": False , "error": str( e ) } )
			return

		if self.path == "/api/sheet/csv":
			# A Google Sheet link -> its rows , for /sort's ⇩ Import to take in as
			# if the file had been dropped on it , and the links behind its cells
			# ( body : { "url": "..." } ). The page can't fetch docs.google.com
			# itself -- no CORS -- and every request is rebuilt from the sheet ID
			# alone , see src/db/gsheet.py . The links are the best-effort half :
			# when they can't be read the rows still come back , and links_error
			# says why.
			try:
				length = int( self.headers.get( "Content-Length" , "0" ) )
				body   = self.rfile.read( length ) if length > 0 else b"{}"
				data   = json.loads( body.decode( "utf-8" , errors="replace" ) )
				from ..db import gsheet
				rows  = gsheet.rows( gsheet.fetch_csv( data.get( "url" ) ) )
				links , why = None , ""
				try:
					links = gsheet.fetch_links( data.get( "url" ) , rows )
				except Exception as e:
					why = str( e ) or e.__class__.__name__
				self._send_json( 200 , { "ok": True , "rows": rows , "links": links ,
					"links_error": why } )
			except ValueError as e:
				self._send_json( 400 , { "ok": False , "error": str( e ) } )
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
		Handler.review    = None
		Handler.review_missing = None
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

	# What those two boards ADD UP TO ( /review ) : serve whatever was last built
	# so the page opens instantly. Rebuilding is minutes of regex over every
	# candidate's full text , so it is NEVER done here -- the page is told the
	# review is stale and offers the button.
	Handler.review = ReviewState( args )
	if Handler.review.load():
		_c = ( Handler.review.doc.get( "meta" ) or {} ).get( "counts" ) or {}
		print( f"review :: loaded {_c.get( 'included' , 0 )} included / "
			f"{_c.get( 'candidates' , 0 )} candidates"
			+ ( "  ( the boards have moved since -- rebuild from /review )"
				if Handler.review.stale() else "" ) )

	# The same screen over the papers you do NOT have ( /review-missing ). Read
	# off disk like the review above ; never built here , for the same reason --
	# it is a pass over every abstract in the missing pool.
	Handler.review_missing = MissingReviewState( args , dash )
	if Handler.review_missing.load():
		_c = ( Handler.review_missing.doc.get( "meta" ) or {} ).get( "counts" ) or {}
		print( f"review-missing :: loaded {_c.get( 'included' , 0 )} candidates / "
			f"{_c.get( 'candidates' , 0 )} in the pool"
			+ ( "  ( the index has moved since -- rebuild from /review-missing )"
				if Handler.review_missing.stale() else "" ) )

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
		f"sort board     {base}/sort     (hand-curated , sections ARE tag sets ; CSV / Google Sheet in , CSV out)" ,
		f"review         {base}/review   ({_review_state( Handler.review )})" ,
		f"tier list      {base}/tiers    (hand-curated buckets ; CSV in / out)" ,
		f"code           {base}/code     (papers with a repo , vs. review / sort / tiers / figures ; xlsx out)" ,
		f"datasets       {base}/datasets (the public data papers stand on , by paper or by dataset ; xlsx out)" ,
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
