"""
The OTHER sort lists : /sort/<slug> , next to the main board at /sort .

The main board ( src/db/sortboard.py , output/cache/sort.json ) is the list --
the one /review screens and /code and /datasets count as "on sort". These are
working sets beside it : a shortlist for one question , a copy to try another
order in , the papers of one year. Each is the same document as the main board
( sortboard's shape , normalize , merge -- everything ) , held in a directory of
its own so its history and its view-only latch travel with it :

  output/cache/sort-lists/<slug>/
    sort.json       the board , exactly sortboard's shape
    history/        sort-*.json , the versions it replaced ( sortboard._snapshot )
    lock.json       its own view-only latch ( src/db/boardlock.py )
    meta.json       { name , created_at , created_by , copied_from , aliases }

The slug IS the URL ( /sort/<slug> ) , which is what makes a list shareable :
send the link. It is made from the name the list was given , and when the list
is renamed it moves with it -- the old slug stays in `aliases` , so a link sent
before the rename still lands ( the server redirects it ).

Deleting a list moves its directory to sort-lists/.trash/ rather than removing
it : a list is somebody's judgement , typed in by hand , and the one thing in
this project that cannot be rebuilt.

`ListStore( slug )` is the store BoardState ( src/server/server.py ) holds for a
list : the sortboard functions , bound to that slug.
"""

import os
import re
import time

from ..utils import utils
from .       import sortboard


SLUG_RE  = re.compile( r"^[a-z0-9]+(?:-[a-z0-9]+)*$" )
MAX_SLUG = 48
MAX_NAME = 80
# `main` is what the page calls the board at /sort ; a list by that name would
# be a second "main" in the switcher.
RESERVED = { "main" }


# ---------------------------------------------------------------------------
# Names and paths
# ---------------------------------------------------------------------------

def valid_slug( slug ):
	"""A slug that can name a list's directory : lowercase words joined by
	single dashes. Nothing else ever reaches a path -- this is the guard."""
	return ( isinstance( slug , str ) and 0 < len( slug ) <= MAX_SLUG
		and bool( SLUG_RE.match( slug ) ) and slug not in RESERVED )


def slugify( name ):
	"""The slug a name makes -- the page's slug() , so what it shows while you
	type is what you get."""
	s = re.sub( r"[^a-z0-9]+" , "-" , str( name or "" ).lower() ).strip( "-" )
	return s[ :MAX_SLUG ].rstrip( "-" )


def clean_name( name ):
	"""A list's display name : one line , trimmed. "" when there is nothing."""
	return re.sub( r"\s+" , " " , str( name or "" ) ).strip()[ :MAX_NAME ]


def list_dir( args , slug ):
	return sortboard.lists_root( args ).joinpath( slug )


def meta_path( args , slug ):
	return list_dir( args , slug ).joinpath( "meta.json" )


def lock_path( args , slug ):
	return list_dir( args , slug ).joinpath( "lock.json" )


def trash_dir( args ):
	return sortboard.lists_root( args ).joinpath( ".trash" )


# ---------------------------------------------------------------------------
# meta.json
# ---------------------------------------------------------------------------

def read_meta( args , slug ):
	"""The list's meta , with every field present. Never raises : a damaged one
	reads as the slug for a name rather than hiding the list."""
	out = { "name": slug , "created_at": "" , "created_by": "" , "copied_from": "" , "aliases": [] }
	try:
		raw = utils.read_json( meta_path( args , slug ) )
	except Exception:
		raw = {}
	if isinstance( raw , dict ):
		out[ "name" ]        = clean_name( raw.get( "name" ) ) or slug
		out[ "created_at" ]  = str( raw.get( "created_at" ) or "" )
		out[ "created_by" ]  = str( raw.get( "created_by" ) or "" )
		out[ "copied_from" ] = str( raw.get( "copied_from" ) or "" )
		out[ "aliases" ]     = [ a for a in ( raw.get( "aliases" ) or [] ) if valid_slug( a ) and a != slug ]
	return out


def write_meta( args , slug , meta ):
	p = meta_path( args , slug )
	p.parent.mkdir( parents=True , exist_ok=True )
	utils.write_json( p , meta )


def exists( args , slug ):
	return valid_slug( slug ) and list_dir( args , slug ).is_dir()


def all_slugs( args ):
	"""Every list on disk , oldest first ( the order they were made in )."""
	root = sortboard.lists_root( args )
	try:
		dirs = [ d for d in root.iterdir() if d.is_dir() and valid_slug( d.name ) ]
	except Exception:
		return []
	return [ d.name for d in sorted( dirs , key=lambda d: ( read_meta( args , d.name )[ "created_at" ] , d.name ) ) ]


def alias_target( args , slug ):
	"""The list a renamed-away slug now lives at , or None."""
	if not valid_slug( slug ):
		return None
	for s in all_slugs( args ):
		if slug in read_meta( args , s )[ "aliases" ]:
			return s
	return None


def _free_slug( args , base ):
	"""`base` , or base-2 , base-3 ... -- the first no list is using."""
	base = base or "list"
	if base in RESERVED:
		base = f"{base}-list"
	n , slug = 1 , base
	while list_dir( args , slug ).exists():
		n   += 1
		tail = f"-{n}"
		slug = base[ :MAX_SLUG - len( tail ) ].rstrip( "-" ) + tail
	return slug


def _drop_alias( args , slug ):
	"""A new list at `slug` wins over an old link that used to redirect there."""
	for s in all_slugs( args ):
		m = read_meta( args , s )
		if slug in m[ "aliases" ]:
			m[ "aliases" ] = [ a for a in m[ "aliases" ] if a != slug ]
			write_meta( args , s , m )


# ---------------------------------------------------------------------------
# Create / rename / delete ( the server runs these under one lock )
# ---------------------------------------------------------------------------

def create( args , name , doc , by="" , copied_from="" ):
	"""Make a new list holding `doc` ( normalized on the way in ). -> its slug.
	ValueError , phrased for a person , when the name makes no slug."""
	name = clean_name( name )
	base = slugify( name )
	if not base:
		raise ValueError( "a list needs a name with at least one letter or digit in it" )
	slug = _free_slug( args , base )
	_drop_alias( args , slug )
	# To the millisecond : the switcher lists them in the order they were made ,
	# and an agent can make several in a second.
	t = time.time()
	write_meta( args , slug , {
		"name":        name ,
		"created_at":  time.strftime( "%Y-%m-%dT%H:%M:%S" , time.localtime( t ) ) + f".{int( t * 1000 ) % 1000:03d}" ,
		"created_by":  by or "" ,
		"copied_from": copied_from or "" ,
		"aliases":     [] ,
	} )
	sortboard.save( args , doc , slug )
	return slug


def rename( args , slug , name ):
	"""Give a list a new name -- and the slug that name makes , moving its
	directory , with the old slug kept as an alias so links to it still land.
	-> the slug it lives at now."""
	name = clean_name( name )
	base = slugify( name )
	if not base:
		raise ValueError( "a list needs a name with at least one letter or digit in it" )
	meta = read_meta( args , slug )
	meta[ "name" ] = name
	if base == slug:
		write_meta( args , slug , meta )
		return slug
	new = _free_slug( args , base )
	os.replace( list_dir( args , slug ) , list_dir( args , new ) )
	_drop_alias( args , new )
	meta[ "aliases" ] = [ a for a in meta[ "aliases" ] + [ slug ] if a != new ]
	write_meta( args , new , meta )
	return new


def delete( args , slug ):
	"""Put a list in the trash ( sort-lists/.trash/<slug>-<stamp>/ ) -- out of
	the switcher and off its URL , but still on disk to be moved back by hand.
	-> where it went."""
	t    = trash_dir( args )
	t.mkdir( parents=True , exist_ok=True )
	dest = t.joinpath( f"{slug}-{time.strftime( '%Y%m%d-%H%M%S' )}" )
	n = 1
	while dest.exists():
		n   += 1
		dest = t.joinpath( f"{slug}-{time.strftime( '%Y%m%d-%H%M%S' )}-{n}" )
	os.replace( list_dir( args , slug ) , dest )
	return dest


# ---------------------------------------------------------------------------
# The store a list's BoardState holds
# ---------------------------------------------------------------------------

class ListStore:
	"""sortboard , bound to one list : the load / save / default_doc /
	normalize / merge interface BoardState already speaks , plus where the
	list's history and view-only latch live."""

	def __init__( self , slug ):
		self.slug = slug

	def load( self , args ):
		return sortboard.load( args , self.slug )

	def save( self , args , doc ):
		return sortboard.save( args , doc , self.slug )

	def default_doc( self ):
		return sortboard.default_doc()

	def normalize( self , doc ):
		return sortboard.normalize( doc )

	def merge( self , base , mine , theirs ):
		return sortboard.merge( base , mine , theirs )

	def snapshots( self , args ):
		return sortboard.snapshots( args , self.slug )

	def read_snapshot( self , args , sid ):
		return sortboard.read_snapshot( args , sid , self.slug )

	def lock_path( self , args ):
		return lock_path( args , self.slug )
