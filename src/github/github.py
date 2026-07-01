"""
Minimal GitHub API client for ` prma code `.

Given a GitHub repo URL harvested from a paper's code links , fetch the repo's
metadata ( description , topics , language , stars , ... ) plus its README and
cache the lot to

  output/cache/github/{owner}__{repo}.json

so a later run ( or the code.xlsx build ) can classify the repo by neuroimaging
method without hitting the network again. Idempotent : an already-cached repo is
returned from disk unless force=True. A 404 / private repo is cached too ( as a
status marker ) so we don't re-hit it every run.

Auth : reads config.yaml -> github.api_key ( a classic PAT or fine-grained
token with public-repo read ). Without a key the client reports configured() ==
False and the caller skips the whole enrichment -- links are still harvested ,
just not tagged by method.
"""

import re
import time
import base64
from datetime import datetime , timezone
from urllib.parse import urlparse

import requests

from ..utils import utils


_API = "https://api.github.com"

# README caps : a keyword scan only needs the text ; don't let a giant README
# bloat the cache.
_README_MAX = 200_000

# github.com path prefixes that are site chrome , not a user's repo ( so a bare
# ` github.com/features ` never gets treated as owner=features ).
_RESERVED_OWNERS = {
	"about" , "features" , "pricing" , "topics" , "search" , "marketplace" ,
	"sponsors" , "settings" , "orgs" , "apps" , "enterprise" , "login" ,
	"join" , "new" , "notifications" , "explore" , "collections" , "events" ,
	"readme" , "site" , "security" , "contact" , "pulls" , "issues" ,
}


def parse_repo( url ):
	"""( owner , repo ) from a GitHub repo / raw URL , or None if it isn't one we
	can turn into a repo ( gists , github.io pages , reserved paths , host-only )."""
	if not url:
		return None
	u = url if re.match( r"(?i)^https?://" , url ) else "https://" + url
	try:
		p = urlparse( u )
	except Exception:
		return None
	host = ( p.netloc or "" ).lower()
	if host.startswith( "www." ):
		host = host[ 4: ]
	segs = [ s for s in ( p.path or "" ).split( "/" ) if s ]
	if host not in ( "github.com" , "raw.githubusercontent.com" ) or len( segs ) < 2:
		return None
	owner , repo = segs[ 0 ] , re.sub( r"\.git$" , "" , segs[ 1 ] )
	if owner.lower() in _RESERVED_OWNERS or not repo:
		return None
	return ( owner , repo )


def _utc_now_iso():
	return datetime.now( timezone.utc ).replace( microsecond=0 ).isoformat()


class GitHub:
	def __init__( self , args ):
		self.args = args
		cfg = {}
		try:
			cfg = utils.read_yaml( args.config.joinpath( "config.yaml" ) ) or {}
		except Exception:
			cfg = {}
		gh = cfg.get( "github" ) or {}
		# Accept either 'api_key' ( matches openalex ) or 'token'.
		self.token = ( gh.get( "api_key" ) or gh.get( "token" ) or "" ).strip()
		self.cache_dir = args.output.joinpath( "cache" , "github" )
		self.cache_dir.mkdir( parents=True , exist_ok=True )

	def configured( self ):
		return bool( self.token )

	# -- HTTP ---------------------------------------------------------------

	def _headers( self ):
		h = {
			"Accept":     "application/vnd.github+json" ,
			"User-Agent": "prma" ,
			"X-GitHub-Api-Version": "2022-11-28" ,
		}
		if self.token:
			h[ "Authorization" ] = f"Bearer {self.token}"
		return h

	def _get( self , url ):
		"""GET a GitHub API url. Returns the parsed JSON on 200 , or a dict
		{ '__status__': ... } for a non-200 we want to CACHE ( not_found ,
		http_4xx ) so we don't re-hit it. Honors rate limits."""
		for attempt in range( 4 ):
			try:
				r = requests.get( url , headers=self._headers() , timeout=30 )
			except requests.exceptions.RequestException as e:
				wait = min( 2 ** attempt , 30 )
				print( f"GitHub :: network error ( {e} ) ; retry in {wait}s" )
				time.sleep( wait )
				continue
			if r.status_code == 200:
				return r.json()
			if r.status_code == 404:
				return { "__status__": "not_found" }
			if r.status_code in ( 403 , 429 ):
				# Primary or secondary rate limit. If the core limit is exhausted ,
				# X-RateLimit-Remaining is 0 and -Reset is the epoch to wait for.
				remaining = r.headers.get( "X-RateLimit-Remaining" )
				if remaining == "0":
					reset = int( r.headers.get( "X-RateLimit-Reset" , "0" ) or 0 )
					wait = max( 1 , reset - int( time.time() ) ) if reset else 60
				else:
					wait = int( r.headers.get( "Retry-After" , "30" ) or 30 )
				wait = min( wait , 120 )
				print( f"GitHub :: rate limited ; sleeping {wait}s" )
				time.sleep( wait )
				continue
			if r.status_code == 401:
				print( "GitHub :: 401 unauthorized -- check config.yaml github.api_key" )
				return { "__status__": "unauthorized" }
			return { "__status__": f"http_{r.status_code}" }
		return { "__status__": "error" }

	# -- cache --------------------------------------------------------------

	def cache_path( self , owner , repo ):
		safe = re.sub( r"[^A-Za-z0-9._-]+" , "-" , f"{owner}__{repo}" )
		return self.cache_dir.joinpath( f"{safe}.json" )

	def cached( self , owner , repo ):
		fp = self.cache_path( owner , repo )
		if not fp.exists():
			return None
		try:
			return utils.read_json( fp )
		except Exception:
			return None

	# -- fetch --------------------------------------------------------------

	def _fetch_readme( self , owner , repo ):
		data = self._get( f"{_API}/repos/{owner}/{repo}/readme" )
		if not isinstance( data , dict ) or data.get( "__status__" ):
			return ""
		content , enc = data.get( "content" ) or "" , data.get( "encoding" )
		if enc == "base64":
			try:
				text = base64.b64decode( content ).decode( "utf-8" , "replace" )
			except Exception:
				return ""
		else:
			text = content if isinstance( content , str ) else ""
		return text[ : _README_MAX ]

	def fetch_repo( self , owner , repo , force=False ):
		"""Fetch + cache a repo's metadata + README. Returns the cached record
		( always a dict with a 'status' field ). Cache-hit fast-path unless
		force=True."""
		if not force:
			hit = self.cached( owner , repo )
			if hit is not None:
				return hit

		meta = self._get( f"{_API}/repos/{owner}/{repo}" )
		rec = {
			"owner":      owner ,
			"repo":       repo ,
			"url":        f"https://github.com/{owner}/{repo}" ,
			"fetched_at": _utc_now_iso() ,
		}
		st = meta.get( "__status__" ) if isinstance( meta , dict ) else "error"
		if st:
			rec[ "status" ] = st
			rec[ "readme" ] = ""
		else:
			rec[ "status" ]      = "ok"
			rec[ "full_name" ]   = meta.get( "full_name" ) or f"{owner}/{repo}"
			rec[ "description" ] = meta.get( "description" ) or ""
			rec[ "topics" ]      = meta.get( "topics" ) or []
			rec[ "language" ]    = meta.get( "language" ) or ""
			rec[ "stars" ]       = meta.get( "stargazers_count" )
			rec[ "homepage" ]    = meta.get( "homepage" ) or ""
			rec[ "pushed_at" ]   = meta.get( "pushed_at" ) or ""
			rec[ "archived" ]    = bool( meta.get( "archived" ) )
			rec[ "readme" ]      = self._fetch_readme( owner , repo )
		try:
			utils.write_json( self.cache_path( owner , repo ) , rec )
		except Exception as e:
			print( f"GitHub :: could not cache {owner}/{repo} ( {e} )" )
		return rec
