"""
The sort board : one hand-curated , hand-ordered list of papers ( /sort ).

The sibling of src/db/tiers.py , and the same kind of store -- one hand-typed
document , written whole , snapshotted on every replaced version -- but with no
grouping at all. There is ONE list. A paper's position in it is the sort ( row 1
is row 1 , and the page numbers them ) , and everything else you want to say
about a paper you say with TAGS : fMRI , EEG , both , premier , whatever you
invent. Tags never move a row on their own ; the page has an option for that ,
off by default , because a hand-made order is the point of the page.

Shape ( output/cache/sort.json ) :

  {
    "version": 1 ,
    "updated_at": "..." ,
    "options":  { "auto_move": false } ,       # re-group a row when its tags change
    "columns":  [ { "id": "notes" , "label": "Notes" } , ... ] ,
    "items":    [ { "key": "10.1038/..." , "title": "..." , "doi": "..." ,
                    "wid": "W..." , "pdf": "" , "year": 2023 , "journal": "" ,
                    "tags": [ "fMRI" , "EEG" ] , "fields": { "notes": "..." } ,
                    "added_at": "..." } , ... ] ,
    "staging":  [ ... same rows , not in the list yet ... ] ,
    "vocab":    { "tags": [ { "name": "premier" , "color": "#cfe0fb" } , ... ] }
  }

`staging` is the shelf a reference list imported from a .docx / a paste lands on
( src/db/refparse.py ) : the same rows , OFF the list , so a bibliography can be
read and tagged before any of it is given a position. See src/db/tiers.py for
why it lives in the document rather than in the browser.

`vocab.tags` is the tag list the page's chips and autocomplete offer , each with
the colour its chips are drawn in. It is part of the DOCUMENT , not of any one
browser , so a tag invented mid-session is still there after a restart --
including one you invented and haven't put on a paper yet , and in the same
colour on every machine you open the board on. Its ORDER is the order the chip
bar draws them in and the order you dragged them into , for the same reason :
grouping related tags side by side is a judgement about the tags.

A row's `tags` are stored ALPHABETICAL ( see tiers._clean_list ) , so a paper's
chips read the same way every time you look at it.

Keys , columns , history and the write-whole contract are all exactly as
src/db/tiers.py describes them -- read that docstring first.
"""

import time

from ..utils import utils
from .       import tiers as tiers_db


VERSION      = 1
HISTORY_KEEP = 40


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def sort_path( args ):
	"""Path to the persisted sort board."""
	return args.output.joinpath( "cache" , "sort.json" )


def history_dir( args ):
	"""Directory holding the timestamped snapshots of past versions."""
	return args.output.joinpath( "cache" , "sort-history" )


# ---------------------------------------------------------------------------
# Defaults + normalization
# ---------------------------------------------------------------------------

def default_doc():
	"""A fresh , empty board."""
	return {
		"version":    VERSION ,
		"updated_at": "" ,
		"options":    { "auto_move": False , "add_where": "bottom" } ,
		"columns":    [ { "id": "notes" , "label": "Notes" } ] ,
		"items":      [] ,
		"staging":    [] ,
		"vocab":      { "tags": [] } ,
	}


def _one_namespace( row ):
	"""ONE tag namespace here , so a document that came through the tier page
	( which keeps modalities in their own list ) keeps everything it was tagged
	with."""
	row[ "tags" ] = tiers_db._clean_list( ( row.get( "tags" ) or [] ) + ( row.get( "mods" ) or [] ) )
	row.pop( "mods" , None )
	return row


def normalize( doc ):
	"""Coerce a posted document into the documented shape. Never raises -- see
	tiers.normalize for why that matters here."""
	if not isinstance( doc , dict ):
		return default_doc()
	columns = tiers_db._clean_columns( doc.get( "columns" ) )
	col_ids = [ c[ "id" ] for c in columns ]
	# One key namespace across the list AND the shelf , so a staged row can never
	# shadow a placed one. The list is cleaned first : it wins.
	items , seen_keys = [] , set()
	for raw in ( doc.get( "items" ) or [] ):
		row = tiers_db._clean_item( raw , col_ids , seen_keys )
		if row:
			items.append( _one_namespace( row ) )
	staging = [ _one_namespace( r ) for r in
		tiers_db._clean_staging( doc.get( "staging" ) , col_ids , seen_keys ) ]
	tags = tiers_db._clean_vocab( ( doc.get( "vocab" ) or {} ).get( "tags" ) , 400 )
	seen = { t[ "name" ].lower() for t in tags }
	# Every tag actually in use joins the vocabulary , so one that arrived on an
	# imported row is offered by the chips and survives the next restart too.
	for it in items + staging:
		for t in it[ "tags" ]:
			if t.lower() not in seen:
				seen.add( t.lower() )
				tags.append( { "name": t , "color": "" } )
	opts  = doc.get( "options" ) if isinstance( doc.get( "options" ) , dict ) else {}
	where = opts.get( "add_where" )
	return {
		"version":    VERSION ,
		"updated_at": tiers_db._clean_str( doc.get( "updated_at" ) , 40 ) ,
		"options":    {
			"auto_move": bool( opts.get( "auto_move" ) ) ,
			# Where a paper added from search lands : the end of the list , the
			# top , or straight after the last row you placed by hand.
			"add_where": where if where in ( "bottom" , "top" , "placed" ) else "bottom" ,
		} ,
		"columns":    columns ,
		"items":      items ,
		"staging":    staging ,
		"vocab":      { "tags": tags[ :400 ] } ,
	}


def item_count( doc ):
	"""How many papers the board holds."""
	return len( ( doc or {} ).get( "items" ) or [] )


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load( args ):
	"""The stored board , normalized -- or an empty one when there is no file.

	Same recovery as the tier list ( tiers.load ) : a file that won't parse falls
	back to the newest readable snapshot in sort-history/ rather than reading
	back as an empty board that the next keystroke would then save over."""
	p = sort_path( args )
	if not p.exists():
		return default_doc()
	try:
		return normalize( utils.read_json( p ) )
	except Exception as e:
		print( f"sort :: {p.name} could not be read ( {e} ) -- looking for the newest snapshot" )
		return _recover( args ) or default_doc()


def _recover( args ):
	"""The newest history snapshot that still parses , normalized."""
	try:
		snaps = sorted( history_dir( args ).glob( "sort-*.json" ) , reverse=True )
	except Exception:
		return None
	for snap in snaps:
		try:
			doc = normalize( utils.read_json( snap ) )
		except Exception:
			continue
		print( f"sort :: recovered from {snap.name}" )
		return doc
	return None


def save( args , doc ):
	"""Normalize , snapshot the version being replaced , then write."""
	doc = normalize( doc )
	doc[ "updated_at" ] = time.strftime( "%Y-%m-%dT%H:%M:%S" , time.localtime() )
	p = sort_path( args )
	p.parent.mkdir( parents=True , exist_ok=True )
	_snapshot( args , p )
	utils.write_json( p , doc )
	return doc


def _snapshot( args , current ):
	"""Keep the version we are about to overwrite ( best-effort ; never blocks
	the save ). Same policy as the tier list's history."""
	if not current.exists():
		return
	try:
		d = history_dir( args )
		d.mkdir( parents=True , exist_ok=True )
		dest = d.joinpath( f"sort-{time.strftime( '%Y%m%d-%H%M%S' )}.json" )
		if not dest.exists():
			dest.write_bytes( current.read_bytes() )
		for f in sorted( d.glob( "sort-*.json" ) )[ : -HISTORY_KEEP ] if HISTORY_KEEP else []:
			try:
				f.unlink()
			except Exception:
				pass
	except Exception as e:
		print( f"sort :: could not snapshot previous version ( {e} )" )
