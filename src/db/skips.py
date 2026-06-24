"""
Skipped / hidden dashboard rows.

A flat set of dashboard row KEYS the user has chosen to hide from the lists via
the dashboard's 'Skip' button. A key is whatever a dashboard pool uses as its
primary id : an OpenAlex WID ( e.g. 'W2911234567' ) for the external references
/ cited-by papers , or a normalized DOI / synthetic nodoi-... key for library
papers.

This NEVER deletes any underlying data -- skipping only records the id here so
the dashboard can filter it out of the default view , and unskipping just drops
the id again , so the user can always change their mind. The pools still carry
every paper ; 'skip' is purely a per-user view preference.

Persisted at output/cache/skipped.json :
  { "keys": [ "W123..." , "10.1016/j.cub..." , ... ] }
"""

from ..utils import utils


def skips_path( args ):
	"""Path to the persisted skip set."""
	return args.output.joinpath( "cache" , "skipped.json" )


def load( args ):
	"""Load the skip set ( empty set if none / unreadable )."""
	p = skips_path( args )
	if not p.exists():
		return set()
	try:
		data = utils.read_json( p ) or {}
	except Exception:
		return set()
	return set( data.get( "keys" ) or [] )


def save( args , keys ):
	"""Persist the skip set ( sorted , for a stable diff-able file )."""
	p = skips_path( args )
	p.parent.mkdir( parents=True , exist_ok=True )
	utils.write_json( p , { "keys": sorted( keys ) } )
