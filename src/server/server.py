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
from http.server import BaseHTTPRequestHandler , HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse , parse_qs , unquote
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
			hide_skipped=True ):
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
			total , page = dash_index.search(
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
			"total":   total ,
			"offset":  offset ,
			"shown":   len( page ) ,
			"results": results ,
		}

	def author_table( self , limit ):
		with self._data_lock:
			if self.status != "ready":
				return { "status": self.status , "total": 0 , "results": [] }
			rows = self.authors[ : max( 0 , limit ) ]
			return { "status": "ready" , "total": len( self.authors ) ,
				"results": list( rows ) }


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
	[text](url) links , and blank-line-separated paragraphs."""
	out = []
	for block in re.split( r"\n\s*\n" , md ):
		block = block.strip( "\n" )
		if not block.strip():
			continue
		m = re.match( r"^#{3,6}\s+(.*)$" , block.strip() )
		if m and "\n" not in block.strip():
			out.append( f"<h4>{_md_inline( m.group( 1 ) )}</h4>" )
		else:
			out.append( f"<p>{_md_inline( block ).replace( chr( 10 ) , '<br>' )}</p>" )
	return "\n".join( out )


def _paper_md_payload( args , key ):
	"""Split a library paper's ` prma md ` render into per-section HTML for the
	'Read' accordion. Relative image links ( ../images/... ) are rewritten to
	the server's /images/ route so figures resolve. Sections split on the
	'## ' headers the md renderer emits ; the '# Title' line becomes the page
	title , anything before the first '## ' becomes an 'Overview' section."""
	from ..db import papers as papers_db
	paper  = papers_db.load( args , key )
	rk     = papers_db.record_key( paper ) if paper else key
	prefix = utils.doi_to_filename( rk ) if rk else None
	md_path = args.output.joinpath( "md" , f"{prefix}.md" ) if prefix else None
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
		elif line.startswith( "# " ) and cur is None and not title:
			title = line[ 2: ].strip()
		else:
			buf.append( line )
	if cur is not None or any( s.strip() for s in buf ):
		raw.append( ( cur , buf ) )

	sections = [
		{ "title": ( t or "Overview" ) , "html": _md_to_html( "\n".join( b ) ) }
		for t , b in raw
	]
	return { "available": True , "key": key , "title": title , "sections": sections }


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
# The per-paper suite runs SIX library tasks ( yolo / ocr / images / methods /
# md ) , each of which prints its own planning + summary lines and spins up its
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
	       methods -> md [ -> summarize ] , scoped to that one paper )
	    -> ONE dashboard reindex for the batch

	On startup it first sweeps the BACKLOG -- every library paper that still has
	undone pipeline work ( no yolo / md / methods ) -- and processes it quietly
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

	def __init__( self , args , dash , cache , summarize=False , backlog=True ):
		self.args      = args
		self.dash      = dash
		self.cache     = cache
		self.summarize = summarize
		self.backlog   = backlog
		self._lock     = threading.Lock()
		self._active   = None     # job dict currently processing , or None
		self._recent   = []       # finished jobs , newest first ( capped )
		self._scanning = False
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
		"""Library papers that still have undone pipeline work : a PDF on disk
		but missing yolo data , the rendered md , or the methods extract. Cheap
		identity + file-existence scan ( one stat per output ). Papers YOLO has
		already failed on are skipped so we don't retry a doomed PDF every boot.
		Newest-updated first , so the freshest additions clear soonest."""
		from ..db    import papers as papers_db
		from ..utils import utils
		md_dir      = self.args.output.joinpath( "md" )
		methods_dir = self.args.output.joinpath( "methods" )
		need = []
		for key , paper in papers_db.iter_all( self.args ):
			if not paper.get( "pdf_path" ):
				continue                               # nothing to process
			if paper.get( papers_db.YOLO_FAILED_KEY ):
				continue                               # don't retry a dead PDF
			prefix      = utils.doi_to_filename( key )
			has_yolo    = bool( ( paper.get( "yolo" ) or {} ).get( "pages" ) )
			has_md      = md_dir.joinpath( f"{prefix}.md" ).exists()
			has_methods = methods_dir.joinpath( f"{prefix}.txt" ).exists()
			if not ( has_yolo and has_md and has_methods ):
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
		still have undone PDF work ( no yolo / md / methods ).

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
		from ..utils import utils
		md_dir      = self.args.output.joinpath( "md" )
		methods_dir = self.args.output.joinpath( "methods" )
		out = []
		for key , paper in papers_db.iter_all( self.args ):
			if key in self._ready_attempted:
				continue
			if paper.get( papers_db.YOLO_FAILED_KEY ):
				continue
			pdf = paper.get( "pdf_path" )
			if not pdf or not Path( pdf ).exists():
				continue                               # no file yet -> wait
			prefix      = utils.doi_to_filename( key )
			has_yolo    = bool( ( paper.get( "yolo" ) or {} ).get( "pages" ) )
			has_md      = md_dir.joinpath( f"{prefix}.md" ).exists()
			has_methods = methods_dir.joinpath( f"{prefix}.txt" ).exists()
			if not ( has_yolo and has_md and has_methods ):
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
		seq = [ "openalex" , "yolo" , "ocr" , "images" , "methods" , "md" ]
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

		with _silence_tasks() as real_stdout:
			prog_ui = _LiveStatus( real_stdout , len( keys ) , stages )
			try:
				for i , key in enumerate( keys , 1 ):
					title = ( papers_db.load( self.args , key ) or {} ).get( "title" ) or key
					job = {
						"key": key , "title": title , "stage": "starting" ,
						"status": "processing" , "started": time.time() ,
					}
					with self._lock:
						self._active = job
					prog_ui.start_paper( i , title )

					def prog( stage , _job=job ):
						with self._lock:
							_job[ "stage" ] = stage
						prog_ui.start_stage( stage )

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
							self._recent.insert( 0 , job )
							del self._recent[ 50: ]
			finally:
				prog_ui.close()

		# One reindex for the whole batch ( cheaper than per-paper ) , applied
		# quietly so anyone browsing isn't bounced to a build screen.
		self.dash.rebuild_after_process()
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
	dash:   DashboardData = None   # injected at startup
	worker: "ProcessWorker" = None # injected at startup IFF --watch ; else None

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

	def do_GET( self ):
		path = urlparse( self.path ).path

		if path in ( "/" , "/index.html" , "/dashboard" ):
			self._send_html( 200 , _load_dashboard_html() )
			return

		if path in ( "/errors" , "/errors.html" ):
			self._send_html( 200 , _load_errors_html() )
			return

		if path == "/api/errors":
			self._send_json( 200 , _errors_payload( self.dash.args ) )
			return

		if path in ( "/status" , "/status.html" ):
			self._send_html( 200 , _load_status_html() )
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

		if path.startswith( "/images/" ):
			self._send_static( self.dash.args.output.joinpath( "images" ) ,
				path[ len( "/images/" ): ] )
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

		if path == "/api/version":
			# A cheap "did the reference library change" token for the DOI-button
			# userscript : it polls this and , when the token moves , re-queries
			# /exists and recolors the page in place ( a paper you just saved
			# flips green without a reload ). cache.get() refreshes only when the
			# Zotero source actually changed , so this is a bare stat() otherwise
			# -- independent of --watch. The token folds the last-refresh time and
			# the title / DOI counts so it bumps on any add / remove.
			titles , dois = self.cache.get()
			n_t , n_d = len( titles or () ) , len( dois or () )
			ver = f"{getattr( self.cache , '_last_refresh' , 0.0 ):.3f}:{n_t}:{n_d}"
			self._send_json( 200 , { "version": ver , "titles": n_t , "dois": n_d } )
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
			self._send_json( 200 , self.dash.search(
				self._arg( qs , "q" , "" ) ,
				self._arg( qs , "pool" , "external" ) ,
				self._arg( qs , "sort" , "lib_cites" ) ,
				self._arg_int( qs , "limit" , 100 ) ,
				offset=self._arg_int( qs , "offset" , 0 ) ,
				direction=self._arg( qs , "dir" , None ) ,
				hide_skipped=hide ,
			) )
			return

		if path == "/api/authors":
			self._send_json( 200 , self.dash.author_table(
				self._arg_int( self._qs() , "limit" , 200 ) ) )
			return

		self._send_json( 404 , { "error": "not found" } )

	def do_POST( self ):
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

	cache = SnapshotCache( args , debounce=args.debounce , ttl=args.ttl )
	cache.get( force=True )   # fail fast

	Handler.cache = cache
	dash = DashboardData( args )
	loaded = dash.load_from_disk()   # serve last ` prma reindex ` instantly if present
	dash.load_skips()                # restore the user's hidden ( skipped ) rows
	Handler.dash = dash

	# Live processing : only when explicitly opted in via --watch , since the
	# per-paper suite ( YOLO / OCR ) is CPU-heavy. The worker seeds its change
	# signature at construction so coming online is a no-op -- it acts only on
	# papers added AFTER the server starts.
	if getattr( args , "watch" , False ):
		worker = ProcessWorker(
			args , dash , cache ,
			summarize=getattr( args , "watch_summarize" , False ) ,
			backlog=getattr( args , "watch_backlog" , True ) ,
		).start()
		Handler.worker = worker

	httpd = ThreadingHTTPServer( ( args.host , args.port ) , Handler )
	watched = "mtime-watched" if cache._watch_files else f"ttl={args.ttl}s"
	base = f"http://{args.host}:{args.port}"
	if loaded:
		dash_state = f"prebuilt index from {dash.built_at} ; run ` prma reindex ` to refresh"
	else:
		dash_state = "no index yet ; builds on first open ( or run ` prma reindex ` )"
	print( f"exists server  {base}/exists   (manager={args.manager}, {watched})" )
	print( f"dashboard      {base}/          ({dash_state})" )
	print( f"errors         {base}/errors   (pipeline problems log)" )
	print( f"status         {base}/status   (pipeline completeness ; run ` prma status `)" )
	if getattr( args , "watch" , False ):
		sm = " +summarize" if getattr( args , "watch_summarize" , False ) else ""
		bl = "backlog+new" if getattr( args , "watch_backlog" , True ) else "new-only"
		print( f"watch          ON{sm}  ({bl} ; live progress at {base}/api/jobs)" )
	else:
		print( f"watch          off       (pass --watch to auto-process newly added papers)" )
	httpd.serve_forever()
