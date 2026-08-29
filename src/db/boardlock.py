"""
The view-only latch for the two hand-curated boards ( /tiers and /sort ).

A board is the one thing in this project that CANNOT be rebuilt -- it is
somebody's judgement , typed in by hand -- and both pages are open to whoever
can reach the server. This is the switch that says "everyone reads , nobody
writes" : one boolean per board , held on the SERVER , so it is the same answer
for every browser that opens the page rather than a preference each of them
keeps to itself.

Server-side is the whole point. A flag in localStorage would lock the tab that
set it and nothing else , which is the opposite of what a lock is for ; and it
would be advisory only -- a stale tab left open from before the lock would still
POST its document back over the top. So the latch lives in a file next to the
document it protects , the pages read it on load and on their version poll , and
POST /api/<board> REFUSES a write while it is on ( see BoardState.replace in
src/server/server.py ) . The page's own greying-out is the courtesy ; this is the
part that actually holds.

It is deliberately NOT part of the board document. The document is written
WHOLE by the page , which means storing the latch inside it would need a write
of the very thing the latch forbids writing -- and would hand a page holding a
stale copy the chance to restore the whole board just by flipping the lock. One
tiny file , written on its own , has neither problem.

Shape ( output/cache/<board>-lock.json ) :

  { "locked": true , "updated_at": "2026-08-20T12:00:00" }

This is a latch , not a permission system : it stops the accidental edit and the
passer-by , and anyone who can reach the toggle can turn it off again. If the
board ever needs protecting from someone who WANTS in , that is authentication ,
and it belongs in front of the server rather than here.
"""

import time

from ..utils import utils


def lock_path( args , name ):
	"""Path to one board's latch file ( name is 'tiers' / 'sort' -- the same
	name the BoardState carries )."""
	return args.output.joinpath( "cache" , f"{name}-lock.json" )


def load( args , name ):
	"""Whether the board is locked. Never raises , and never fails CLOSED : a
	missing or damaged file reads as unlocked , because a board nobody can edit
	because of a corrupt byte is worse than one briefly left open."""
	p = lock_path( args , name )
	if not p.exists():
		return False
	try:
		return bool( ( utils.read_json( p ) or {} ).get( "locked" ) )
	except Exception as e:
		print( f"{name} :: {p.name} could not be read ( {e} ) -- treating the board as unlocked" )
		return False


def save( args , name , on ):
	"""Set the latch and persist it. Returns what was stored."""
	on = bool( on )
	p  = lock_path( args , name )
	p.parent.mkdir( parents=True , exist_ok=True )
	utils.write_json( p , {
		"locked":     on ,
		"updated_at": time.strftime( "%Y-%m-%dT%H:%M:%S" , time.localtime() ) ,
	} )
	return on
