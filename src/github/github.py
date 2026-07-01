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
from rapidfuzz import fuzz , process

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


# ---------------------------------------------------------------------------
# OCR-repair : match a mangled repo name against an owner's real repo list
# ---------------------------------------------------------------------------
# OCR damages a repo name in a few predictable ways : it truncates the tail
# ( ` Surface_projection ` -> ` Surfa ` ) , drops the space/hyphen between two
# words ( ` TO-START ` -> ` TOSTART ` ) , or fat-fingers a character. The owner
# ( the profile ) almost always survives intact -- so we list the owner's actual
# repos and pick the one the broken name most plausibly meant.

def _norm_name( s ):
	"""A repo name reduced to lowercase alphanumerics -- drops the '-' / '_' / '.'
	that OCR most often adds or loses , so ` A-SIMPLE-TO-START ` , ` a_simple_to_start `
	and the OCR'd ` ASIMPLE-TOSTART ` all collapse to one comparable key."""
	return re.sub( r"[^a-z0-9]+" , "" , ( s or "" ).lower() )


def _best_repo_match( bad , names ):
	"""Best guess for the real repo `bad` was meant to be , among an owner's
	`names`. Returns ( real_name , score , method ) for a confident match , else
	None. Confidence tiers , most-trustworthy first :
	  'exact'  -- normalized names are identical ( OCR only mangled punctuation /
	              spacing , e.g. TO-START vs TOSTART ) ;
	  'prefix' -- the broken name is a normalized prefix of exactly one repo
	              ( OCR truncated the tail ) -- or of several , in which case the
	              highest fuzzy score among them wins ;
	  'fuzzy'  -- a high WRatio ( >= 90 ) catches typo-level edits the norm
	              missed. Below that we abstain ( better a 404 than a wrong repo )."""
	names = [ n for n in names if n ]
	if not names:
		return None
	nb = _norm_name( bad )
	if len( nb ) < 3:
		return None
	normed = [ ( n , _norm_name( n ) ) for n in names ]
	# 1. exact normalized match
	for n , nn in normed:
		if nn == nb:
			return ( n , 100.0 , "exact" )
	# 2. normalized prefix ( truncation ) -- needs a few chars to be meaningful
	if len( nb ) >= 5:
		prefixes = [ n for n , nn in normed if nn.startswith( nb ) ]
		if len( prefixes ) == 1:
			return ( prefixes[ 0 ] , 97.0 , "prefix" )
		if len( prefixes ) > 1:
			# Several repos extend the truncated name ( ` braindecode ` and
			# ` braindecode.github.io ` both start with ` brainde ` ) -- the SHORTEST
			# completion is the most conservative guess ; WRatio breaks a length tie.
			best = min( prefixes , key=lambda n: ( len( _norm_name( n ) ) , -fuzz.WRatio( bad , n ) ) )
			return ( best , 92.0 , "prefix" )
	# 3. fuzzy fallback
	cand = process.extractOne( bad , names , scorer=fuzz.WRatio )
	if cand and cand[ 1 ] >= 90:
		return ( cand[ 0 ] , float( cand[ 1 ] ) , "fuzzy" )
	return None


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
		# Per-run memo for resolve_repo : owner -> [ repo names ] ( or None ) , so
		# several broken links under the same owner list the profile only once.
		self._user_repos = {}

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
		"""Fetch the repo's default README ( the /readme endpoint auto-detects
		README.md / .rst / .txt on the default branch ). Returns ( text , status )
		where status is 'ok' ( got it ) , 'none' ( repo genuinely has no README --
		a 404 ) , or 'error' ( transient : rate limit / network / decode ) so the
		caller can RETRY an error next run instead of caching an empty README
		forever ( which would leave the repo un-tagged )."""
		data = self._get( f"{_API}/repos/{owner}/{repo}/readme" )
		if isinstance( data , dict ) and data.get( "__status__" ) == "not_found":
			return "" , "none"
		if not isinstance( data , dict ) or data.get( "__status__" ):
			return "" , "error"
		content , enc = data.get( "content" ) or "" , data.get( "encoding" )
		if enc == "base64":
			try:
				text = base64.b64decode( content ).decode( "utf-8" , "replace" )
			except Exception:
				return "" , "error"
		else:
			text = content if isinstance( content , str ) else ""
		return text[ : _README_MAX ] , "ok"

	def _needs_readme_retry( self , rec ):
		"""A cached OK repo whose README fetch failed TRANSIENTLY ( 'error' , not a
		definite 'none' ) is worth re-fetching so a rate-limit hiccup doesn't leave
		it permanently un-tagged."""
		return rec.get( "status" ) == "ok" and rec.get( "readme_status" ) == "error"

	def fetch_repo( self , owner , repo , force=False ):
		"""Fetch + cache a repo's metadata + README. Returns the cached record
		( always a dict with a 'status' field ). Cache-hit fast-path unless
		force=True OR the cached README fetch failed transiently ( retry it )."""
		if not force:
			hit = self.cached( owner , repo )
			if hit is not None and not self._needs_readme_retry( hit ):
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
			rec[ "readme_status" ] = "skip"
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
			rec[ "readme" ] , rec[ "readme_status" ] = self._fetch_readme( owner , repo )
		try:
			utils.write_json( self.cache_path( owner , repo ) , rec )
		except Exception as e:
			print( f"GitHub :: could not cache {owner}/{repo} ( {e} )" )
		return rec

	# -- OCR repair -------------------------------------------------------------

	def list_user_repos( self , owner , cap_pages=3 ):
		"""Public repo NAMES for a user / org , paginated ( 100/page ) and capped
		so a huge account can't run away. Uses the CORE rate limit ( 5000/hr ) ,
		not the stingy Search API. Returns the name list , or None when the owner
		itself doesn't exist / errored ( so the caller can tell 'no such owner'
		-- likely the OCR mangled the profile too -- from 'owner has no match' ).
		Memoized per run."""
		if owner in self._user_repos:
			return self._user_repos[ owner ]
		names  = []
		result = names
		for page in range( 1 , cap_pages + 1 ):
			data = self._get( f"{_API}/users/{owner}/repos?per_page=100&sort=updated&page={page}" )
			if isinstance( data , dict ):        # a { '__status__': ... } error
				result = names if ( page > 1 and names ) else None
				break
			if not isinstance( data , list ):
				break
			names += [ r.get( "name" ) for r in data if r.get( "name" ) ]
			if len( data ) < 100:
				break
		self._user_repos[ owner ] = result
		return result

	def resolve_repo( self , owner , repo ):
		"""Try to repair an OCR-mangled ( owner , repo ) by matching `repo`
		against the owner's real repo list. Returns ( real_repo , score , method )
		for a confident match ( see _best_repo_match ) , or None when the owner is
		gone or nothing matches well enough."""
		names = self.list_user_repos( owner )
		if not names:
			return None
		return _best_repo_match( repo , names )

	def record_resolution( self , owner , repo , resolution ):
		"""Pin a `resolve` marker on the ( not_found ) cache record for
		( owner , repo ) so a later run skips the lookup. `resolution` is
		{ matched: bool , [ repo , url , score , method ] , at }."""
		rec = self.cached( owner , repo ) or {
			"owner": owner , "repo": repo ,
			"url": f"https://github.com/{owner}/{repo}" , "status": "not_found" ,
		}
		rec[ "resolve" ] = resolution
		try:
			utils.write_json( self.cache_path( owner , repo ) , rec )
		except Exception as e:
			print( f"GitHub :: could not record resolution for {owner}/{repo} ( {e} )" )
