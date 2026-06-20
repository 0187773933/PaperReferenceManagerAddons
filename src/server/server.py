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

import json
import time
import threading
from http.server import BaseHTTPRequestHandler , HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import urlparse , parse_qs
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
		self.built_at    = None
		self.lib_count   = 0
		self.missing_count = 0
		self._loaded_mtime = 0.0   # mtime of the on-disk index we last loaded

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
				"built_at":      self.built_at ,
				"manager":       getattr( self.args , "manager" , "" ) ,
			}

	def _pool( self , name ):
		if name == "references":
			return self.references
		if name == "cited_by":
			return self.cited_by
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

	def search( self , query , pool , sort , limit , offset=0 , direction=None ):
		from ..dashboard import index as dash_index
		with self._data_lock:
			if self.status != "ready":
				return { "status": self.status , "pool": pool , "total": 0 ,
					"shown": 0 , "offset": offset , "results": [] }
			rows = self._pool( pool )
			total , page = dash_index.search(
				rows , query , sort=sort , limit=limit ,
				offset=offset , direction=direction )
		return {
			"status":  "ready" ,
			"pool":    pool ,
			"total":   total ,
			"offset":  offset ,
			"shown":   len( page ) ,
			"results": [ dash_index.to_public( r ) for r in page ] ,
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
	cache: SnapshotCache = None   # injected at startup
	dash:  DashboardData = None   # injected at startup

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

	def _send_html( self , code: int , html: str ):
		raw = html.encode( "utf-8" )
		self.send_response( code )
		self.send_header( "Content-Type"   , "text/html; charset=utf-8" )
		self.send_header( "Content-Length" , str( len( raw ) ) )
		self.end_headers()
		self.wfile.write( raw )

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
			self._send_json( 200 , self.dash.search(
				self._arg( qs , "q" , "" ) ,
				self._arg( qs , "pool" , "external" ) ,
				self._arg( qs , "sort" , "lib_cites" ) ,
				self._arg_int( qs , "limit" , 100 ) ,
				offset=self._arg_int( qs , "offset" , 0 ) ,
				direction=self._arg( qs , "dir" , None ) ,
			) )
			return

		if path == "/api/authors":
			self._send_json( 200 , self.dash.author_table(
				self._arg_int( self._qs() , "limit" , 200 ) ) )
			return

		self._send_json( 404 , { "error": "not found" } )

	def do_POST( self ):
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
	Handler.dash = dash

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
	httpd.serve_forever()
