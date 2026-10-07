"""
Accounts , roles and API keys for the dashboard server.

Three levels , and one question per request -- "is this caller at least X" :

  anon   anyone who can reach the server. Reads everything that is ABOUT the
         library ( pages , boards , search , review , status ) and , while an
         admin allows it , the paper content too ( PDFs , md , figures ). Makes
         the server DO nothing : no edits , imports , exports , rebuilds , and
         none of the lazy builds a GET would otherwise start.
  user   a logged-in account. Everything the dashboard can do.
  admin  a user who can also manage the other accounts and the content setting.
         That is the whole difference.

How a person proves who they are ( there are no passwords ) :

  1. An admin -- or the first-run bootstrap , or ` prma auth link ` -- mints a
     one-time LOGIN LINK , /login#<id>.<secret>. The secret sits in the fragment ,
     so it never reaches a server or proxy log , and a chat app's link preview
     can't spend it : only the page's own script POSTs it to /api/login.
  2. Redeeming it creates a SESSION : an HttpOnly , SameSite=Strict cookie
     holding another <id>.<secret>. Revoking one is deleting a row.

Callers that aren't browsers -- a script , a cron job , the userscripts if they
ever need more than /exists -- send an API KEY : ` Authorization: Bearer <key> `.
A key carries a role from the same set , so there is one permission model , not
two. It is bounded twice : at mint time it can't ask for more than its owner
has , and on every request its effective role is recomputed as the LOWER of the
two , so demoting an admin demotes every key they made. A disabled account's
keys stop working at once but aren't destroyed ; re-enabling restores them.

Two containment rules , both from the go-webserver template this mirrors :

  - Nothing that MINTS a credential ( an API key , a login link ) accepts a key.
    A leaked key that could mint another would survive its own revocation.
  - Cookie-authorised POSTs must carry X-CSRF-Token ( the session's token , from
    GET /api/me ). A cookie is ambient ; a header isn't , so key requests skip it.

Every credential is stored only as sha256( secret ) and compared in constant
time. They are 256-bit random , so a slow hash would buy nothing but latency.

The store is one file ( src/db/accounts.py ). The server re-reads it whenever it
moves on disk , so ` prma auth ... ` from another shell reaches a running server
without a restart.
"""

import hmac
import json
import time
import hashlib
import secrets
import threading
from http.cookies import SimpleCookie

from ..db import accounts
from ..utils import utils


ROLES         = { "anon": 0 , "user": 1 , "admin": 2 }
ACCOUNT_ROLES = ( "user" , "admin" )

# Any account can mint keys , so the bucket needs a bound ; revoking frees a slot.
KEY_MAX_PER_USER = 25
# "Is anything still using this key" is the question you ask before revoking it ,
# so it's worth recording -- but not with a file write per request.
KEY_TOUCH_EVERY  = 15 * 60

# config.yaml's auth: section , and what each key means when it's absent.
DEFAULTS = {
	"enabled":          True ,    # False : no accounts at all ; everyone may do everything
	"session_days":     30 ,
	"login_link_hours": 72 ,
	"api_key_days":     90 ,      # default expiry for a new key ; 0 = never
	"secure_cookies":   "auto" ,  # auto = Secure when X-Forwarded-Proto is https
	"public_url":       "" ,      # base for login links printed on the terminal
}

# The POSTs an anonymous caller may make. /exists is the userscripts' lookup ;
# the three row-lookup routes are READS that only use POST for the size of their
# key list ( /api/cite among them : anonymous visitors can read a board , so
# they get its citation line too -- an author list and a journal name are
# bibliographic facts about a published paper , not the paper CONTENT that
# settings.anon_content gates ) ; /api/login is how anyone stops being
# anonymous. Every other POST needs an account -- including any added later ,
# which is the point of listing the exceptions rather than the rules.
ANON_POSTS = { "/exists" , "/api/paper-meta" , "/api/tiers/meta" , "/api/cite" , "/api/login" }

# GETs that only ever feed an edit flow : the /sort board's "recent" list ( it
# reads the reference manager's own sqlite ) , your own key list , the /sort
# board's saved versions ( read back to restore one ) , and the library papers
# not on a sort list yet ( its first call reads every paper record ).
USER_GETS = { "/api/recent" , "/api/keys" , "/api/sort/history" , "/api/sort/remaining" }


def _user_get( path ):
	"""USER_GETS , plus the other sort lists' saved versions
	( /api/sort/lists/<slug>/history ) -- the same thing as /api/sort/history ."""
	return path in USER_GETS or ( path.startswith( "/api/sort/lists/" ) and path.endswith( "/history" ) )

# Paper CONTENT , as opposed to what is known ABOUT a paper : the PDF , its
# full text , and the figures cut from it. Anonymous visitors see these only
# while settings.anon_content is on ( the admin toggle on /account ).
CONTENT_PATHS    = { "/pdf" , "/api/paper" ,
                     "/method-images" , "/method-images.html" , "/images" , "/images.html" }
CONTENT_PREFIXES = ( "/md/" , "/images/" , "/methods/" )

# Routes that mint a credential. A key can't reach them ( see the module doc ).
MINTING = { "/api/keys" , "/api/admin/users" , "/api/admin/users/link" }

# Cap on one anonymous /exists call. The userscripts send one page's worth of
# references ; this only stops someone making the fuzzy matcher chew forever.
ANON_EXISTS_MAX = 5000


def load_config( args ):
	"""config.yaml's auth: section over DEFAULTS. A missing or unreadable
	config means the defaults -- which is accounts ON."""
	try:
		cfg = utils.read_yaml( args.config.joinpath( "config.yaml" ) ) or {}
	except Exception:
		cfg = {}
	sect = cfg.get( "auth" ) or {}
	out  = dict( DEFAULTS )
	if isinstance( sect , dict ):
		out.update( { k: v for k , v in sect.items() if k in DEFAULTS and v is not None } )
	return out


def is_content( path ):
	return path in CONTENT_PATHS or path.startswith( CONTENT_PREFIXES )


def required_role( method , path , anon_content=True ):
	"""The least role that may make this request. Fails closed : a POST nobody
	thought about needs an account."""
	if path == "/api/admin" or path.startswith( "/api/admin/" ):
		return "admin"
	if method == "POST":
		return "anon" if path in ANON_POSTS else "user"
	if _user_get( path ):
		return "user"
	if not anon_content and is_content( path ):
		return "user"
	return "anon"


def narrow( account_role , key_role ):
	"""A key's effective role : the lower of what it asked for and what its
	owner has NOW."""
	return min( ( account_role , key_role ) , key=lambda r: ROLES.get( r , 0 ) )


class AuthError( Exception ):
	def __init__( self , msg , code=400 ):
		super().__init__( msg )
		self.code = code


class Actor:
	"""Who is asking , however they proved it. Route code reads .role / .can_work
	and never needs to know whether that was a cookie or a key."""

	__slots__ = ( "role" , "account_role" , "user_id" , "name" , "via" ,
	              "session_id" , "csrf" , "key_id" )

	def __init__( self , role="anon" , account_role=None , user_id=None , name=None ,
	              via=None , session_id=None , csrf=None , key_id=None ):
		self.role         = role
		self.account_role = account_role
		self.user_id      = user_id
		self.name         = name
		self.via          = via            # "session" | "key" | "local" | None
		self.session_id   = session_id
		self.csrf         = csrf
		self.key_id       = key_id

	@property
	def level( self ):
		return ROLES.get( self.role , 0 )

	@property
	def can_work( self ):
		"""May this caller make the server DO something ( build , rebuild ,
		regenerate ) rather than hand back what already exists."""
		return self.level >= ROLES[ "user" ]

	@property
	def is_admin( self ):
		return self.level >= ROLES[ "admin" ]


ANON = Actor()
# auth.enabled: false -- the old wide-open server. Everyone is this.
LOCAL_ADMIN = Actor( role="admin" , account_role="admin" , name="local" , via="local" )


# -- credentials ---------------------------------------------------------------

def _hash( secret ):
	return hashlib.sha256( secret.encode( "utf-8" ) ).hexdigest()


def _mint():
	"""( id , secret , the '<id>.<secret>' the holder is given ). The id is a
	lookup handle , so a check is one dict probe plus one hash compare."""
	ident  = secrets.token_hex( 8 )
	secret = secrets.token_urlsafe( 32 )
	return ident , secret , f"{ident}.{secret}"


def _split( token ):
	if not isinstance( token , str ) or "." not in token:
		return None , None
	ident , secret = token.strip().split( "." , 1 )
	return ident , secret


def _matches( row , secret ):
	return bool( row and secret and
		hmac.compare_digest( str( row.get( "hash" ) or "" ) , _hash( secret ) ) )


def _bearer( header ):
	"""The key out of an Authorization header ; None when there isn't one."""
	if not header:
		return None
	scheme , _ , rest = header.strip().partition( " " )
	return rest.strip() if scheme.lower() == "bearer" else ""


def _clean_name( name , what="name" ):
	name = " ".join( str( name or "" ).split() )
	if not name or len( name ) > 64:
		raise AuthError( f"{what} must be 1-64 characters" )
	return name


class AuthStore:
	"""The accounts file , held in memory and re-read whenever it moves on disk.
	Every public method takes the lock , so the threaded server can call any of
	them from any request."""

	def __init__( self , args , cfg=None ):
		self.args    = args
		self.cfg     = cfg or load_config( args )
		self.enabled = bool( self.cfg.get( "enabled" , True ) )
		self.port    = getattr( args , "port" , 9371 )
		self._lock   = threading.RLock()
		self._doc    = None
		self._sig    = None
		self._broken = None
		if self.enabled:
			with self._lock:
				self._reload( force=True )

	# -- the file --------------------------------------------------------------

	def _stat_sig( self ):
		try:
			st = accounts.auth_path( self.args ).stat()
			return ( st.st_mtime_ns , st.st_size )
		except OSError:
			return None

	def _reload( self , force=False ):
		sig = self._stat_sig()
		if not force and self._doc is not None and sig == self._sig:
			return
		try:
			self._doc = accounts.load( self.args )
			if self._broken:
				print( "auth      :: accounts file readable again" )
			self._broken = None
		except Exception as e:
			if self._broken is None:
				print( f"auth      :: !! {accounts.auth_path( self.args )} can't be read ( {e} ) -- "
					"NOBODY can log in and paper content is hidden from anonymous visitors "
					"until it's fixed or removed" )
			self._broken = str( e )
			self._doc = accounts.empty()
			self._doc[ "settings" ][ "anon_content" ] = False
		self._sig = sig

	def _save( self ):
		if self._broken:
			raise AuthError( "the accounts file can't be read ; fix or remove "
				f"{accounts.auth_path( self.args )} first" , 503 )
		self._purge()
		accounts.save( self.args , self._doc )
		self._sig = self._stat_sig()

	def _purge( self ):
		now = time.time()
		for bucket in ( "links" , "sessions" , "keys" ):
			rows = self._doc[ bucket ]
			for i in [ i for i , r in rows.items()
					if ( r.get( "expires_at" ) or 0 ) and r[ "expires_at" ] <= now ]:
				del rows[ i ]

	# -- lookups ---------------------------------------------------------------

	def _user( self , ref ):
		"""( uid , row ) by id or by name ( case-insensitive ) ; AuthError when
		there's no such account."""
		users = self._doc[ "users" ]
		if ref in users:
			return ref , users[ ref ]
		want = str( ref or "" ).strip().lower()
		for uid , u in users.items():
			if u.get( "name" , "" ).lower() == want:
				return uid , u
		raise AuthError( f"no account {ref!r}" , 404 )

	def _admins( self ):
		return [ uid for uid , u in self._doc[ "users" ].items()
			if u.get( "role" ) == "admin" and u.get( "enabled" ) ]

	def _user_public( self , uid , u ):
		return {
			"id":            uid ,
			"name":          u.get( "name" ) ,
			"role":          u.get( "role" ) ,
			"enabled":       bool( u.get( "enabled" ) ) ,
			"created_at":    u.get( "created_at" ) ,
			"last_login_at": u.get( "last_login_at" ) ,
			"keys":          sum( 1 for k in self._doc[ "keys" ].values() if k.get( "user" ) == uid ) ,
		}

	def key_name( self , kid ):
		"""The name an API key was minted under -- what a board says made a change."""
		with self._lock:
			return ( ( self._doc or {} ).get( "keys" , {} ).get( kid ) or {} ).get( "name" ) or ""

	def _key_public( self , kid , k ):
		owner = self._doc[ "users" ].get( k.get( "user" ) ) or {}
		return {
			"id":           kid ,
			"name":         k.get( "name" ) ,
			"role":         k.get( "role" ) ,
			"effective":    narrow( owner.get( "role" , "user" ) , k.get( "role" , "user" ) )
			                if owner.get( "enabled" ) else "none" ,
			"user":         k.get( "user" ) ,
			"owner":        owner.get( "name" ) ,
			"created_at":   k.get( "created_at" ) ,
			"expires_at":   k.get( "expires_at" ) ,
			"last_used_at": k.get( "last_used_at" ) ,
		}

	# -- who is asking ---------------------------------------------------------

	def resolve( self , cookie_value , bearer ):
		"""The Actor for a request. The cookie is tried first , so a request that
		arrives with ambient browser credentials is always held to the CSRF check
		and can't opt out of it by also carrying a key. Returns None when a
		bearer key was presented and is no good : a script must hear that , not
		be quietly served as anonymous."""
		if not self.enabled:
			return LOCAL_ADMIN
		now = time.time()
		with self._lock:
			self._reload()
			if cookie_value:
				a = self._from_session( cookie_value , now )
				if a:
					return a
			if bearer is not None:
				return self._from_key( bearer , now )
		return ANON

	def _from_session( self , value , now ):
		sid , secret = _split( value )
		row = self._doc[ "sessions" ].get( sid )
		if not _matches( row , secret ) or row.get( "expires_at" , 0 ) <= now:
			return None
		u = self._doc[ "users" ].get( row.get( "user" ) )
		if not u or not u.get( "enabled" ):
			return None
		return Actor( role=u[ "role" ] , account_role=u[ "role" ] , user_id=row[ "user" ] ,
			name=u.get( "name" ) , via="session" , session_id=sid , csrf=row.get( "csrf" ) )

	def _from_key( self , value , now ):
		kid , secret = _split( value )
		row = self._doc[ "keys" ].get( kid )
		if not _matches( row , secret ):
			return None
		if row.get( "expires_at" ) and row[ "expires_at" ] <= now:
			return None
		u = self._doc[ "users" ].get( row.get( "user" ) )
		if not u or not u.get( "enabled" ):
			return None
		if now - ( row.get( "last_used_at" ) or 0 ) >= KEY_TOUCH_EVERY:
			row[ "last_used_at" ] = int( now )
			try:
				self._save()
			except Exception:
				pass
		return Actor( role=narrow( u[ "role" ] , row.get( "role" , "user" ) ) ,
			account_role=u[ "role" ] , user_id=row[ "user" ] , name=u.get( "name" ) ,
			via="key" , key_id=kid )

	def anon_content( self ):
		"""May anonymous visitors open paper content right now."""
		if not self.enabled:
			return True
		with self._lock:
			self._reload()
			return bool( self._doc[ "settings" ].get( "anon_content" , True ) )

	# -- sessions --------------------------------------------------------------

	def redeem_link( self , token ):
		"""Spend a login link. -> ( cookie value , the account ). Single use :
		the row is gone before the session exists."""
		lid , secret = _split( token )
		now = time.time()
		with self._lock:
			self._reload()
			if self._broken:
				raise AuthError( "logins are unavailable : the accounts file can't be read" , 503 )
			row = self._doc[ "links" ].get( lid )
			if not _matches( row , secret ) or row.get( "expires_at" , 0 ) <= now:
				raise AuthError( "that login link is invalid , already used or expired -- "
					"ask an admin for a new one" , 400 )
			uid = row.get( "user" )
			u   = self._doc[ "users" ].get( uid )
			del self._doc[ "links" ][ lid ]
			if not u or not u.get( "enabled" ):
				self._save()
				raise AuthError( "that account is disabled" , 403 )
			sid , ssecret , value = _mint()
			self._doc[ "sessions" ][ sid ] = {
				"user":       uid ,
				"hash":       _hash( ssecret ) ,
				"csrf":       secrets.token_urlsafe( 24 ) ,
				"created_at": int( now ) ,
				"expires_at": int( now + self.session_seconds() ) ,
			}
			u[ "last_login_at" ] = int( now )
			self._save()
			print( f"auth      :: {u.get( 'name' )} logged in" )
			return value , self._user_public( uid , u )

	def logout( self , session_id ):
		with self._lock:
			self._reload()
			if self._doc[ "sessions" ].pop( session_id , None ) is not None:
				self._save()

	def session_seconds( self ):
		return max( 1 , int( float( self.cfg.get( "session_days" ) or 30 ) * 86400 ) )

	# -- accounts --------------------------------------------------------------

	def _issue_link( self , uid , bootstrap=False ):
		lid , secret , token = _mint()
		hours = float( self.cfg.get( "login_link_hours" ) or 72 )
		self._doc[ "links" ][ lid ] = {
			"user":       uid ,
			"hash":       _hash( secret ) ,
			"expires_at": int( time.time() + hours * 3600 ) ,
			"bootstrap":  bool( bootstrap ) ,
		}
		return token

	def list_users( self ):
		with self._lock:
			self._reload()
			rows = [ self._user_public( uid , u ) for uid , u in self._doc[ "users" ].items() ]
		return sorted( rows , key=lambda r: ( r[ "name" ] or "" ).lower() )

	def add_user( self , name , role="user" ):
		"""-> ( the account , a login link token for it )."""
		name = _clean_name( name )
		if role not in ACCOUNT_ROLES:
			raise AuthError( f"role must be one of {', '.join( ACCOUNT_ROLES )}" )
		with self._lock:
			self._reload()
			if any( u.get( "name" , "" ).lower() == name.lower()
					for u in self._doc[ "users" ].values() ):
				raise AuthError( f"there is already an account called {name!r}" , 409 )
			uid = secrets.token_hex( 6 )
			self._doc[ "users" ][ uid ] = {
				"name": name , "role": role , "enabled": True ,
				"created_at": int( time.time() ) , "last_login_at": None ,
			}
			token = self._issue_link( uid )
			self._save()
			return self._user_public( uid , self._doc[ "users" ][ uid ] ) , token

	def issue_link( self , ref ):
		"""A fresh login link for an existing account ( lost cookie , new browser )."""
		with self._lock:
			self._reload()
			uid , u = self._user( ref )
			if not u.get( "enabled" ):
				raise AuthError( f"{u.get( 'name' )} is disabled ; enable the account first" , 409 )
			token = self._issue_link( uid )
			self._save()
			return self._user_public( uid , u ) , token

	def update_user( self , ref , role=None , enabled=None ):
		with self._lock:
			self._reload()
			uid , u = self._user( ref )
			if role is not None and role not in ACCOUNT_ROLES:
				raise AuthError( f"role must be one of {', '.join( ACCOUNT_ROLES )}" )
			losing_admin = ( u.get( "role" ) == "admin" and u.get( "enabled" ) and
				( role == "user" or enabled is False ) )
			if losing_admin and self._admins() == [ uid ]:
				raise AuthError( "that's the last enabled admin -- make someone else an admin first" , 409 )
			if role is not None:
				u[ "role" ] = role
			if enabled is not None:
				u[ "enabled" ] = bool( enabled )
				if not enabled:
					# A session is a browser artifact and costs nothing to recreate ;
					# the keys stay , refused while the account is off.
					for sid in [ s for s , r in self._doc[ "sessions" ].items() if r.get( "user" ) == uid ]:
						del self._doc[ "sessions" ][ sid ]
			self._save()
			return self._user_public( uid , u )

	def delete_user( self , ref ):
		with self._lock:
			self._reload()
			uid , u = self._user( ref )
			if u.get( "role" ) == "admin" and u.get( "enabled" ) and self._admins() == [ uid ]:
				raise AuthError( "that's the last enabled admin -- make someone else an admin first" , 409 )
			del self._doc[ "users" ][ uid ]
			for bucket in ( "links" , "sessions" , "keys" ):
				rows = self._doc[ bucket ]
				for i in [ i for i , r in rows.items() if r.get( "user" ) == uid ]:
					del rows[ i ]
			self._save()
			return { "id": uid , "name": u.get( "name" ) }

	def bootstrap( self ):
		"""First run : while no enabled admin has EVER logged in , mint a fresh
		admin login link ( retiring the previous unused one ) so the terminal
		that started the server can claim it. -> ( name , token ) or None."""
		if not self.enabled:
			return None
		with self._lock:
			self._reload()
			if self._broken:
				return None
			users  = self._doc[ "users" ]
			admins = self._admins()
			if any( users[ uid ].get( "last_login_at" ) for uid in admins ):
				return None
			if admins:
				uid = next( ( a for a in admins if users[ a ].get( "name" ) == "admin" ) , admins[ 0 ] )
			else:
				try:
					uid , u = self._user( "admin" )
					u[ "role" ] , u[ "enabled" ] = "admin" , True
				except AuthError:
					uid = secrets.token_hex( 6 )
					users[ uid ] = {
						"name": "admin" , "role": "admin" , "enabled": True ,
						"created_at": int( time.time() ) , "last_login_at": None ,
					}
			links = self._doc[ "links" ]
			for lid in [ l for l , r in links.items() if r.get( "bootstrap" ) ]:
				del links[ lid ]
			token = self._issue_link( uid , bootstrap=True )
			self._save()
			return users[ uid ][ "name" ] , token

	# -- API keys --------------------------------------------------------------

	def list_keys( self , user_id=None ):
		with self._lock:
			self._reload()
			rows = [ self._key_public( kid , k ) for kid , k in self._doc[ "keys" ].items()
				if user_id is None or k.get( "user" ) == user_id ]
		return sorted( rows , key=lambda r: r[ "created_at" ] or 0 , reverse=True )

	def create_key( self , ref , name , role="user" , days=None ):
		"""-> ( the key's public row , the key itself -- shown once , never stored )."""
		name = _clean_name( name , "key name" )
		if role not in ACCOUNT_ROLES:
			raise AuthError( f"role must be one of {', '.join( ACCOUNT_ROLES )}" )
		if days is None or days == "":
			days = self.cfg.get( "api_key_days" , 90 )
		try:
			days = max( 0 , min( 3650 , int( days ) ) )
		except ( TypeError , ValueError ):
			raise AuthError( "days must be a whole number ( 0 = never expires )" )
		with self._lock:
			self._reload()
			uid , u = self._user( ref )
			if not u.get( "enabled" ):
				raise AuthError( f"{u.get( 'name' )} is disabled" , 409 )
			if ROLES[ role ] > ROLES.get( u.get( "role" ) , 0 ):
				raise AuthError( "a key can't outrank the account that owns it" , 403 )
			if sum( 1 for k in self._doc[ "keys" ].values() if k.get( "user" ) == uid ) >= KEY_MAX_PER_USER:
				raise AuthError( f"{KEY_MAX_PER_USER} keys is the limit ; revoke one first" , 409 )
			kid , secret , token = _mint()
			now = int( time.time() )
			self._doc[ "keys" ][ kid ] = {
				"user":         uid ,
				"name":         name ,
				"role":         role ,
				"hash":         _hash( secret ) ,
				"created_at":   now ,
				"expires_at":   now + days * 86400 if days else None ,
				"last_used_at": None ,
			}
			self._save()
			return self._key_public( kid , self._doc[ "keys" ][ kid ] ) , token

	def revoke_key( self , kid , actor=None ):
		"""Delete a key. Its owner or an admin ; nobody else learns it exists."""
		with self._lock:
			self._reload()
			k = self._doc[ "keys" ].get( kid )
			if not k or ( actor is not None and not actor.is_admin and k.get( "user" ) != actor.user_id ):
				raise AuthError( "no such key" , 404 )
			del self._doc[ "keys" ][ kid ]
			self._save()
			return { "id": kid , "name": k.get( "name" ) }

	# -- settings --------------------------------------------------------------

	def settings( self ):
		with self._lock:
			self._reload()
			return { "anon_content": bool( self._doc[ "settings" ].get( "anon_content" , True ) ) }

	def set_anon_content( self , on ):
		with self._lock:
			self._reload()
			self._doc[ "settings" ][ "anon_content" ] = bool( on )
			self._save()
			print( f"auth      :: paper content is now {'OPEN to' if on else 'hidden from'} anonymous visitors" )
			return self.settings()

	# -- HTTP ------------------------------------------------------------------

	def cookie_name( self ):
		# Port-suffixed : a local ` prma ` and a docker one on the same host would
		# otherwise overwrite each other's session ( cookies ignore the port ).
		return f"prma_session_{self.port}"

	def _read_cookie( self , header ):
		if not header:
			return None
		try:
			c = SimpleCookie()
			c.load( header )
			m = c.get( self.cookie_name() )
			return m.value if m else None
		except Exception:
			return None

	def _cookie_header( self , handler , value , max_age ):
		secure = str( self.cfg.get( "secure_cookies" , "auto" ) ).strip().lower()
		if secure == "auto":
			on = ( handler.headers.get( "X-Forwarded-Proto" ) or "" ).lower() == "https"
		else:
			on = secure in ( "true" , "1" , "yes" , "on" )
		parts = [ f"{self.cookie_name()}={value}" , "Path=/" , "HttpOnly" ,
		          "SameSite=Strict" , f"Max-Age={int( max_age )}" ]
		if on:
			parts.append( "Secure" )
		return "; ".join( parts )

	def gate( self , handler , method , path ):
		"""Resolve the caller onto handler.actor and decide. True to carry on ;
		False when the refusal has already been sent."""
		handler.actor = ANON
		actor = self.resolve( self._read_cookie( handler.headers.get( "Cookie" ) ) ,
			_bearer( handler.headers.get( "Authorization" ) ) )
		if actor is None:
			handler._send_json( 401 , { "ok": False ,
				"error": "that API key is invalid , expired or revoked" } )
			return False
		handler.actor = actor
		need = required_role( method , path ,
			anon_content=actor.can_work or self.anon_content() )
		if actor.level >= ROLES[ need ]:
			if ( need != "anon" and method == "POST" and actor.via == "session" and
					not hmac.compare_digest( handler.headers.get( "X-CSRF-Token" ) or "" ,
						actor.csrf or "" ) ):
				handler._send_json( 403 , { "ok": False , "csrf": True ,
					"error": "missing or stale CSRF token -- reload the page" } )
				return False
			return True
		if actor.level == ROLES[ "anon" ]:
			msg = ( "log in to see paper content on this server" if is_content( path )
				else "log in to do this" )
			if method == "GET" and "text/html" in ( handler.headers.get( "Accept" ) or "" ):
				handler._send_html( 401 ,
					"<!doctype html><meta charset=utf-8><title>Log in</title>"
					"<body style='font:15px system-ui;max-width:36em;margin:3em auto;padding:0 16px'>"
					f"<h1>Log in</h1><p>{msg[ 0 ].upper() + msg[ 1: ]}. "
					"Accounts use login links -- ask an admin for one.</p>"
					"<p><a href='/'>&larr; Dashboard</a></p>" )
			else:
				handler._send_json( 401 , { "ok": False , "login": True , "error": msg } )
		else:
			handler._send_json( 403 , { "ok": False , "error": "admins only" } )
		return False

	def me( self , actor ):
		return {
			"auth_enabled": self.enabled ,
			"role":         actor.role ,
			"account_role": actor.account_role ,
			"via":          actor.via ,
			"user":         { "id": actor.user_id , "name": actor.name } if actor.user_id else None ,
			"csrf":         actor.csrf if actor.via == "session" else None ,
			"anon_content": self.anon_content() ,
		}

	def handle( self , handler , method , path ):
		"""The account routes. True when `path` was one of them ( and has been
		answered ). The gate has already checked the role each one needs."""
		if not ( path in ( "/api/me" , "/api/login" , "/api/logout" ) or
				path == "/api/keys" or path.startswith( "/api/keys/" ) or
				path.startswith( "/api/admin/" ) ):
			return False
		actor = handler.actor
		if path == "/api/me" and method == "GET":
			handler._send_json( 200 , self.me( actor ) )
			return True
		if not self.enabled:
			handler._send_json( 404 , { "ok": False ,
				"error": "accounts are off ( auth.enabled: false in config.yaml )" } )
			return True
		if path in MINTING and method == "POST" and actor.via != "session":
			handler._send_json( 403 , { "ok": False , "error": "minting a credential needs a "
				"browser session -- an API key can't mint a key or a login link" } )
			return True
		try:
			out = self._route( handler , method , path , actor )
		except AuthError as e:
			handler._send_json( e.code , { "ok": False , "error": str( e ) } )
			return True
		if out is None:
			handler._send_json( 404 , { "ok": False , "error": "not found" } )
		else:
			code , payload , headers = out
			handler._send_json( code , payload , headers=headers )
		return True

	def _route( self , handler , method , path , actor ):
		if method == "POST":
			body = _body( handler )
			if path == "/api/login":
				value , user = self.redeem_link( body.get( "token" ) )
				return 200 , { "ok": True , "user": user } , [
					( "Set-Cookie" , self._cookie_header( handler , value , self.session_seconds() ) ) ]
			if path == "/api/logout":
				if actor.session_id:
					self.logout( actor.session_id )
				return 200 , { "ok": True } , [
					( "Set-Cookie" , self._cookie_header( handler , "" , 0 ) ) ]
			if path == "/api/keys":
				key , token = self.create_key( actor.user_id , body.get( "name" ) ,
					body.get( "role" ) or "user" , body.get( "days" ) )
				return 200 , { "ok": True , "key": key , "token": token } , None
			if path in ( "/api/keys/revoke" , "/api/admin/keys/revoke" ):
				return 200 , { "ok": True , **self.revoke_key( body.get( "id" ) ,
					None if path.startswith( "/api/admin/" ) else actor ) } , None
			if path == "/api/admin/users":
				user , token = self.add_user( body.get( "name" ) , body.get( "role" ) or "user" )
				return 200 , { "ok": True , "user": user , "token": token } , None
			if path == "/api/admin/users/link":
				user , token = self.issue_link( body.get( "id" ) )
				return 200 , { "ok": True , "user": user , "token": token } , None
			if path == "/api/admin/users/update":
				en = body.get( "enabled" )
				return 200 , { "ok": True , "user": self.update_user( body.get( "id" ) ,
					role=body.get( "role" ) , enabled=None if en is None else bool( en ) ) } , None
			if path == "/api/admin/users/delete":
				return 200 , { "ok": True , **self.delete_user( body.get( "id" ) ) } , None
			if path == "/api/admin/settings":
				return 200 , { "ok": True , **self.set_anon_content( body.get( "anon_content" ) ) } , None
			return None
		if path == "/api/keys":
			return 200 , { "keys": self.list_keys( actor.user_id ) ,
				"max": KEY_MAX_PER_USER , "default_days": self.cfg.get( "api_key_days" , 90 ) } , None
		if path == "/api/admin/users":
			return 200 , { "users": self.list_users() } , None
		if path == "/api/admin/keys":
			return 200 , { "keys": self.list_keys() } , None
		if path == "/api/admin/settings":
			return 200 , self.settings() , None
		return None


def _body( handler , limit=64 * 1024 ):
	"""The JSON body of an account request ( they're all tiny )."""
	try:
		length = int( handler.headers.get( "Content-Length" , "0" ) )
	except ValueError:
		length = 0
	if length > limit:
		raise AuthError( "request body too large" , 413 )
	raw = handler.rfile.read( length ) if length > 0 else b"{}"
	try:
		data = json.loads( raw.decode( "utf-8" , errors="replace" ) or "{}" )
	except ValueError:
		raise AuthError( "body must be JSON" )
	if not isinstance( data , dict ):
		raise AuthError( "body must be a JSON object" )
	return data


# -- prma auth ------------------------------------------------------------------

def link_base( store , host=None ):
	"""Where a printed login link points : auth.public_url when set , else this
	machine's own server."""
	pub = str( store.cfg.get( "public_url" ) or "" ).strip().rstrip( "/" )
	if pub:
		return pub
	h = host or "127.0.0.1"
	if h in ( "0.0.0.0" , "::" , "" ):
		h = "127.0.0.1"
	return f"http://{h}:{store.port}"


def _when( t ):
	return time.strftime( "%Y-%m-%d %H:%M" , time.localtime( t ) ) if t else "-"


def cli( args ):
	"""` prma auth <command> ` -- the shell's way in : the first admin , a lost
	link , or a key for a script , without a browser. Edits the same file the
	server reads , and a running server picks the change up on its next request."""
	store = AuthStore( args )
	if not store.enabled:
		print( "auth.enabled is false in config.yaml -- accounts are off ; "
			"everyone can do everything on the dashboard" )
		return
	if store._broken:
		print( f"can't read {accounts.auth_path( args )} ( {store._broken} ) ; fix or remove it first" )
		return
	cmd  = getattr( args , "auth_command" , None ) or "users"
	base = link_base( store )
	try:
		if cmd == "users":
			rows = store.list_users()
			if not rows:
				print( "no accounts yet -- start the server ( it prints the first admin's "
					"login link ) or run ` prma auth add-user NAME --role admin `" )
			for r in rows:
				state = "" if r[ "enabled" ] else "  DISABLED"
				print( f"{r['name']:<24} {r['role']:<6} last login {_when( r['last_login_at'] )}"
					f"  keys {r['keys']}{state}" )
			print( f"anonymous visitors {'CAN' if store.settings()[ 'anon_content' ] else 'can NOT'} "
				"open paper content ( prma auth anon-content on|off )" )
		elif cmd == "add-user":
			u , token = store.add_user( args.auth_name , args.auth_role )
			print( f"created {u['name']} ( {u['role']} ) -- one-time login link :" )
			print( f"  {base}/login#{token}" )
		elif cmd == "link":
			u , token = store.issue_link( args.auth_name )
			print( f"one-time login link for {u['name']} :" )
			print( f"  {base}/login#{token}" )
		elif cmd == "set-role":
			u = store.update_user( args.auth_name , role=args.auth_role )
			print( f"{u['name']} is now {u['role']}" )
		elif cmd in ( "disable" , "enable" ):
			u = store.update_user( args.auth_name , enabled=( cmd == "enable" ) )
			print( f"{u['name']} {'enabled' if u['enabled'] else 'disabled'}" )
		elif cmd == "delete-user":
			u = store.delete_user( args.auth_name )
			print( f"deleted {u['name']} ( and their sessions , links and keys )" )
		elif cmd == "keys":
			rows = store.list_keys()
			if not rows:
				print( "no API keys" )
			for k in rows:
				print( f"{k['id']}  {k['owner'] or '?':<16} {k['name']:<24} {k['effective']:<6}"
					f" expires {_when( k['expires_at'] ) if k['expires_at'] else 'never'}"
					f"  last used {_when( k['last_used_at'] )}" )
		elif cmd == "create-key":
			k , token = store.create_key( args.auth_name , args.auth_key_name ,
				args.auth_role , args.auth_days )
			print( f"key {k['id']} for {k['owner']} ( {k['role']} , expires "
				f"{_when( k['expires_at'] ) if k['expires_at'] else 'never'} ) -- shown once :" )
			print( f"  {token}" )
			print( f"  curl -H 'Authorization: Bearer {token}' {base}/api/me" )
		elif cmd == "revoke-key":
			k = store.revoke_key( args.auth_key_id )
			print( f"revoked {k['id']} ( {k['name']} )" )
		elif cmd == "anon-content":
			on = args.auth_switch == "on"
			store.set_anon_content( on )
		else:
			print( f"unknown auth command {cmd!r}" )
	except AuthError as e:
		print( f"auth :: {e}" )
		raise SystemExit( 1 )
