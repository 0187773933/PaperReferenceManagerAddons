"""
Minimal OSF ( Open Science Framework ) API client for ` prma code `.

Given an osf.io URL harvested from a paper's code links , resolve its 5-char
GUID , fetch the referent's metadata ( title , description , category , tags ,
dates , ... ) plus its "Home" wiki ( the closest thing OSF has to a README )
and cache the lot to

  output/cache/osf/{guid}.json

so a later run ( or the code.xlsx build ) can classify the project by
neuroimaging method without hitting the network again. Idempotent : an
already-cached node is returned from disk unless force=True. A 404 / private /
deleted node is cached too ( as a status marker ) so we don't re-hit it.

GUIDs resolve through /v2/guids/{guid}/ , which 302-redirects to the typed
endpoint ( nodes / registrations / preprints / files ) ; requests follows the
redirect and the returned `data.type` tells us what we got. Only project-like
referents ( nodes / registrations ) carry wikis ; preprints / files fall back
to their description alone.

Auth : reads config.yaml -> osfio.api_key ( a Personal Access Token , sent as
` Authorization: Bearer <token> ` ). Without a key the client reports
configured() == False and the caller skips the whole enrichment -- links are
still harvested , just not tagged by method.
"""

import re
import time
import html
from datetime import datetime , timezone
from urllib.parse import urlparse

import requests

from ..utils import utils


_API = "https://api.osf.io/v2"

# Wiki caps : a keyword scan only needs the text ; don't let a giant wiki bloat
# the cache.
_WIKI_MAX = 200_000

# An OSF GUID is a short lowercase-alphanumeric slug ( historically 5 chars ).
_GUID_RE = re.compile( r"^[0-9a-z]{5,}$" )

# osf.io path prefixes / segments that are site chrome or sub-resources , never
# a project GUID ( so a bare ` osf.io/dashboard ` or the ` /overview ` tail of
# ` osf.io/<guid>/overview ` never gets treated as the GUID ).
_RESERVED_GUIDS = {
	"preprints" , "dashboard" , "search" , "login" , "logout" , "register" ,
	"settings" , "support" , "explore" , "myprojects" , "registries" ,
	"institutions" , "quickfiles" , "project" , "wiki" , "files" , "overview" ,
	"home" , "metrics" , "download" , "forks" , "registrations" , "comments" ,
	"contributors" , "osfstorage" , "components" , "analytics" , "about" ,
}

# The DOI form of an OSF record : https://doi.org/10.17605/OSF.IO/<guid> . The
# code-link harvester classifies these as OSF ( via its doi.org path hints ) so
# we resolve the embedded GUID here too.
_DOI_OSF_RE = re.compile( r"(?i)10\.17605/osf\.io/([0-9a-z]+)" )


def parse_node( url ):
	"""The OSF GUID from an osf.io URL ( or a 10.17605/OSF.IO DOI ) , or None if
	it isn't one we can turn into a record ( host-only , reserved paths ). Handles
	the shapes seen in the wild :
	  osf.io/<guid>                     -> <guid>
	  osf.io/<guid>/overview|wiki/home  -> <guid>   ( GUID is the first segment )
	  osf.io/preprints/<prov>/<guid>_v2 -> <guid>   ( GUID is the last segment ,
	                                                  version suffix stripped )
	  doi.org/10.17605/OSF.IO/<guid>    -> <guid>"""
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

	if host == "doi.org" or host.endswith( ".doi.org" ):
		m = _DOI_OSF_RE.search( p.path or "" )
		if not m:
			return None
		cand = m.group( 1 ).lower()
		return cand if _GUID_RE.match( cand ) else None

	if host != "osf.io":
		return None
	segs = [ s for s in ( p.path or "" ).split( "/" ) if s ]
	if not segs:
		return None
	# Preprints nest the GUID under /preprints/<provider>/ ; everything else
	# carries it as the first path segment.
	cand = segs[ -1 ] if segs[ 0 ].lower() == "preprints" and len( segs ) >= 2 else segs[ 0 ]
	cand = re.sub( r"(?i)_v\d+$" , "" , cand.strip().strip( "." ) ).lower()
	if not _GUID_RE.match( cand ) or cand in _RESERVED_GUIDS:
		return None
	return cand


def _utc_now_iso():
	return datetime.now( timezone.utc ).replace( microsecond=0 ).isoformat()


class OSF:
	def __init__( self , args ):
		self.args = args
		cfg = {}
		try:
			cfg = utils.read_yaml( args.config.joinpath( "config.yaml" ) ) or {}
		except Exception:
			cfg = {}
		osf = cfg.get( "osfio" ) or cfg.get( "osf" ) or {}
		# Accept either 'api_key' ( matches openalex / github ) or 'token'.
		self.token = ( osf.get( "api_key" ) or osf.get( "token" ) or "" ).strip()
		self.cache_dir = args.output.joinpath( "cache" , "osf" )
		self.cache_dir.mkdir( parents=True , exist_ok=True )

	def configured( self ):
		return bool( self.token )

	# -- HTTP ---------------------------------------------------------------

	def _headers( self , accept ):
		h = { "User-Agent": "prma" , "Accept": accept }
		if self.token:
			h[ "Authorization" ] = f"Bearer {self.token}"
		return h

	def _http_get( self , url , accept ):
		"""GET `url` ( following OSF's guid 302 redirect ) , retrying on network
		errors and rate limits ( 429 ). Returns the final requests.Response , or
		None once we exhaust retries on transient failures."""
		for attempt in range( 4 ):
			try:
				r = requests.get( url , headers=self._headers( accept ) , timeout=30 )
			except requests.exceptions.RequestException as e:
				wait = min( 2 ** attempt , 30 )
				print( f"OSF :: network error ( {e} ) ; retry in {wait}s" )
				time.sleep( wait )
				continue
			if r.status_code == 429:
				wait = min( max( int( r.headers.get( "Retry-After" , "30" ) or 30 ) , 1 ) , 120 )
				print( f"OSF :: rate limited ; sleeping {wait}s" )
				time.sleep( wait )
				continue
			return r
		return None

	def _get( self , url ):
		"""GET a JSON:API url. Returns the parsed JSON on 200 , or a dict
		{ '__status__': ... } for a non-200 we want to CACHE ( not_found ,
		forbidden , unauthorized , http_4xx ) so we don't re-hit it."""
		r = self._http_get( url , "application/vnd.api+json" )
		if r is None:
			return { "__status__": "error" }
		if r.status_code == 200:
			try:
				return r.json()
			except Exception:
				return { "__status__": "error" }
		if r.status_code in ( 404 , 410 ):
			return { "__status__": "not_found" }
		if r.status_code == 403:
			# A private / embargoed node -- the token is fine ( other nodes
			# resolve ) , this one just isn't ours to read. Cache & move on ,
			# like GitHub's 404-for-private-repos.
			return { "__status__": "forbidden" }
		if r.status_code == 401:
			print( "OSF :: 401 unauthorized -- check config.yaml osfio.api_key" )
			return { "__status__": "unauthorized" }
		return { "__status__": f"http_{r.status_code}" }

	# -- cache --------------------------------------------------------------

	def cache_path( self , guid ):
		safe = re.sub( r"[^A-Za-z0-9._-]+" , "-" , guid )
		return self.cache_dir.joinpath( f"{safe}.json" )

	def cached( self , guid ):
		fp = self.cache_path( guid )
		if not fp.exists():
			return None
		try:
			return utils.read_json( fp )
		except Exception:
			return None

	# -- fetch --------------------------------------------------------------

	def _fetch_wiki( self , guid , base ):
		"""Fetch the node's 'Home' wiki text ( OSF's nearest equivalent to a
		README ). Returns ( text , status ) where status is 'ok' ( got it ) ,
		'none' ( no wiki page exists ) , or 'error' ( transient : rate limit /
		network / decode ) so the caller can RETRY an error next run instead of
		caching an empty wiki forever."""
		data = self._get( f"{_API}/{base}/{guid}/wikis/" )
		if isinstance( data , dict ) and data.get( "__status__" ) in ( "not_found" , "forbidden" ):
			return "" , "none"
		if not isinstance( data , dict ) or data.get( "__status__" ):
			return "" , "error"
		wikis = data.get( "data" ) or []
		if not wikis:
			return "" , "none"
		# Prefer the page named 'home' ; fall back to the first wiki.
		chosen = next(
			( w for w in wikis
			  if ( ( w.get( "attributes" ) or {} ).get( "name" ) or "" ).lower() == "home" ) ,
			wikis[ 0 ] ,
		)
		content_url = ( ( chosen.get( "links" ) or {} ).get( "download" )
		                or f"{_API}/wikis/{chosen.get( 'id' )}/content/" )
		r = self._http_get( content_url , "text/markdown" )
		if r is None:
			return "" , "error"
		if r.status_code in ( 404 , 410 ):
			return "" , "none"
		if r.status_code != 200:
			return "" , "error"
		return ( r.text or "" )[ : _WIKI_MAX ] , "ok"

	def _needs_wiki_retry( self , rec ):
		"""A cached OK node whose wiki fetch failed TRANSIENTLY ( 'error' , not a
		definite 'none' / 'na' ) is worth re-fetching so a rate-limit hiccup
		doesn't leave it permanently un-tagged."""
		return rec.get( "status" ) == "ok" and rec.get( "readme_status" ) == "error"

	def fetch_node( self , guid , force=False ):
		"""Fetch + cache an OSF referent's metadata + wiki. Returns the cached
		record ( always a dict with a 'status' field ). Cache-hit fast-path
		unless force=True OR the cached wiki fetch failed transiently."""
		if not force:
			hit = self.cached( guid )
			if hit is not None and not self._needs_wiki_retry( hit ):
				return hit

		data = self._get( f"{_API}/guids/{guid}/" )
		rec = {
			"guid":       guid ,
			"url":        f"https://osf.io/{guid}/" ,
			"fetched_at": _utc_now_iso() ,
		}
		st   = data.get( "__status__" ) if isinstance( data , dict ) else "error"
		node = data.get( "data" ) if isinstance( data , dict ) else None
		if st or not isinstance( node , dict ):
			rec[ "status" ]        = st or "error"
			rec[ "readme" ]        = ""
			rec[ "readme_status" ] = "skip"
		else:
			attrs  = node.get( "attributes" ) or {}
			rtype  = node.get( "type" ) or ""
			rec[ "status" ]        = "ok"
			rec[ "type" ]          = rtype
			rec[ "title" ]         = html.unescape( attrs.get( "title" ) or "" )
			rec[ "description" ]   = html.unescape( attrs.get( "description" ) or "" )
			rec[ "category" ]      = attrs.get( "category" ) or ""
			rec[ "tags" ]          = attrs.get( "tags" ) or []
			rec[ "date_created" ]  = attrs.get( "date_created" ) or attrs.get( "date_published" ) or ""
			rec[ "date_modified" ] = attrs.get( "date_modified" ) or ""
			rec[ "public" ]        = bool( attrs.get( "public" , True ) )
			# Only nodes / registrations carry wikis ; preprints / files don't ,
			# so their description is the whole haystack.
			if rtype in ( "nodes" , "registrations" ):
				base = "registrations" if rtype == "registrations" else "nodes"
				rec[ "readme" ] , rec[ "readme_status" ] = self._fetch_wiki( guid , base )
			else:
				rec[ "readme" ] , rec[ "readme_status" ] = "" , "na"
		try:
			utils.write_json( self.cache_path( guid ) , rec )
		except Exception as e:
			print( f"OSF :: could not cache {guid} ( {e} )" )
		return rec
