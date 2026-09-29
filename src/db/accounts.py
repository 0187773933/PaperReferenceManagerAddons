"""
Who may do what on the dashboard : accounts , login links , sessions , API keys ,
and the one admin setting ( whether anonymous visitors may open paper CONTENT ).

The rules live in src/server/auth.py ; this is only the file they are kept in.

Shape ( output/auth/auth.json , chmod 600 ) :

  {
    "version"  : 1 ,
    "settings" : { "anon_content": true } ,
    "users"    : { "<uid>": { "name" , "role": "admin"|"user" , "enabled" ,
                             "created_at" , "last_login_at" } } ,
    "links"    : { "<id>": { "user" , "hash" , "expires_at" , "bootstrap" } } ,
    "sessions" : { "<id>": { "user" , "hash" , "csrf" , "created_at" , "expires_at" } } ,
    "keys"     : { "<id>": { "user" , "name" , "role" , "hash" , "created_at" ,
                             "expires_at" , "last_used_at" } }
  }

Every credential is only a sha256 of its secret half ; the secret itself is shown
once and never written anywhere.

Its own directory under output/ rather than cache/ , because output/ is what gets
rsync-ed from the main machine to a deployed box : this file belongs to the box
it was minted on , and ` rsync --exclude auth/ ` has to be able to leave it alone.

Unlike the board latch ( src/db/boardlock.py ) this fails CLOSED : a file that
can't be read raises , and the caller refuses every login rather than guess.
"""

import os

from ..utils import utils


VERSION = 1


def auth_path( args ):
	"""Path to the one accounts file."""
	return args.output.joinpath( "auth" , "auth.json" )


def empty():
	"""A store with nobody in it ( the first run , before bootstrap )."""
	return {
		"version":  VERSION ,
		"settings": { "anon_content": True } ,
		"users":    {} ,
		"links":    {} ,
		"sessions": {} ,
		"keys":     {} ,
	}


def load( args ):
	"""The whole store. A missing file is an empty store ; an unreadable one
	RAISES -- the caller decides what "closed" looks like."""
	p = auth_path( args )
	if not p.exists():
		return empty()
	doc = utils.read_json( p )
	if not isinstance( doc , dict ) or not isinstance( doc.get( "users" ) , dict ):
		raise ValueError( f"{p} is not an accounts file" )
	base = empty()
	for k , v in base.items():
		if not isinstance( doc.get( k ) , type( v ) ):
			doc[ k ] = v
	return doc


def save( args , doc ):
	"""Persist the store atomically , readable by this user only. The directory
	is 0700 too , so the temp file write_json renames into place is never
	briefly readable by anyone else either."""
	p = auth_path( args )
	p.parent.mkdir( parents=True , exist_ok=True )
	try:
		os.chmod( p.parent , 0o700 )
	except OSError:
		pass
	utils.write_json( p , doc )
	try:
		os.chmod( p , 0o600 )
	except OSError:
		pass
