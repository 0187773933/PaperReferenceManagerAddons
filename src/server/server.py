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
from http.server import BaseHTTPRequestHandler , HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Dict , List , Optional , Set , Tuple

from rapidfuzz import fuzz , process

from ..utils import utils
from ..tasks import snapshot as snap_module


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
		papers = snap_module.get_common( self.args )
		if not papers:
			raise RuntimeError( "snapshot returned nothing — check --manager flag" )
		titles: Set[ str ] = set()
		dois:   Set[ str ] = set()
		for d in papers.values():
			t = d.get( "title" )
			if t:
				nt = utils.normalize_title( t )
				if nt:
					titles.add( nt )
			doi = d.get( "doi" )
			if doi:
				nd = utils.normalize_doi( doi )
				if nd:
					dois.add( nd )
		self._titles = titles
		self._dois   = dois
		self._last_refresh = time.time()
		print( f"snapshot refreshed — {len(titles)} titles, {len(dois)} DOIs" )

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

	def log_message( self , *_ ):
		return

	def _send_json( self , code: int , payload: Dict ):
		raw = json.dumps( payload ).encode( "utf-8" )
		self.send_response( code )
		self.send_header( "Content-Type"                , "application/json; charset=utf-8" )
		self.send_header( "Content-Length"              , str( len( raw ) ) )
		self.send_header( "Access-Control-Allow-Origin" , "*" )
		self.send_header( "Access-Control-Allow-Methods", "POST, OPTIONS" )
		self.send_header( "Access-Control-Allow-Headers", "Content-Type" )
		self.end_headers()
		self.wfile.write( raw )

	def do_OPTIONS( self ):
		self.send_response( 204 )
		self.send_header( "Access-Control-Allow-Origin" , "*" )
		self.send_header( "Access-Control-Allow-Methods", "POST, OPTIONS" )
		self.send_header( "Access-Control-Allow-Headers", "Content-Type" )
		self.end_headers()

	def do_POST( self ):
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

	httpd = ThreadingHTTPServer( ( args.host , args.port ) , Handler )
	watched = "mtime-watched" if cache._watch_files else f"ttl={args.ttl}s"
	print( f"exists server  http://{args.host}:{args.port}  (manager={args.manager}, {watched})" )
	httpd.serve_forever()
