"""
The tier list : hand-curated , ranked papers ( the /tiers page ).

Everything else in this project is DERIVED -- the pools , the figure reports ,
the modality stamps -- and can be thrown away and rebuilt. This file is the one
store that CANNOT : it is the user's own judgement about which papers are the
premier references , typed in by hand , one paper at a time. So it is written
whole ( the page owns the document and posts it back ) , validated on the way
in , and every accepted write leaves a timestamped snapshot behind in
output/cache/tiers-history/ .

Shape ( output/cache/tiers.json ) :

  {
    "version": 1 ,
    "updated_at": "2026-08-15T12:00:00+00:00" ,
    "columns": [ { "id": "notes" , "label": "Notes" } , ... ] ,
    "vocab":   { "tags": [ { "name": "premier" , "color": "#e5484d" } , ... ] ,
                 "mods": [ { "name": "fMRI"    , "color": "#cfe0fb" } , ... ] } ,
    "tiers":   [
      { "id": "a" , "label": "A" , "desc": "fMRI decodes covert speech" ,
        "color": "#e5484d" , "locked": false ,
        "items": [ {
          "key":   "10.1038/s41593-023-01304-9" ,   # library primary key , or a
                                                    # WID for an external paper
          "title": "..." , "doi": "..." , "year": 2023 , "wid": "W..." ,
          "mods":  [ "fMRI" ] ,                     # modality vocabulary labels
          "tags":  [ "premier" ] ,                  # free-form , user-made
          "fields": { "notes": "..." } ,            # one per `columns` entry
          "added_at": "..."
        } , ... ] } , ... ]
  }

The KEY is the same identity every other prma surface uses ( a normalized DOI ,
a synthetic nodoi-… key , or an OpenAlex WID for a paper that isn't in the
library yet ) , which is what lets the page hang the DOI / PDF / MD / Methods
links off a row without storing any of them.

Items are unique across the WHOLE document : an item lives in exactly one tier ,
so normalize() drops any repeat of a key it has already seen ( first tier wins ).
`columns` is the notes-style free-text columns the page renders , in order ;
"notes" is always present so a fresh document has somewhere to type. Both the
column set and the tag vocabulary are part of the document because a CSV import
can introduce either.
"""

import re
import time

from ..utils import utils


VERSION = 1

# The staging tier -- where search-adds land when no other tier is armed , and
# where the items of a deleted tier are moved. Always present , never deletable
# ( `locked` ) , always last.
STAGING_ID    = "unranked"
STAGING_LABEL = "Unranked"

# Seeded on first open. Colours are the usual tier-list ramp ( red -> blue ) ,
# readable against both themes.
DEFAULT_TIERS = (
	( "s" , "S" , "#ff5c5c" ) ,
	( "a" , "A" , "#ff9f43" ) ,
	( "b" , "B" , "#ffd166" ) ,
	( "c" , "C" , "#7ac74f" ) ,
	( "d" , "D" , "#4aa3df" ) ,
)

# How many past versions to keep in output/cache/tiers-history/ . Each is a few
# KB , and they are the undo of last resort for a hand-typed document.
HISTORY_KEEP = 40


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def tiers_path( args ):
	"""Path to the persisted tier list."""
	return args.output.joinpath( "cache" , "tiers.json" )


def history_dir( args ):
	"""Directory holding the timestamped snapshots of past versions."""
	return args.output.joinpath( "cache" , "tiers-history" )


# ---------------------------------------------------------------------------
# Defaults + normalization
# ---------------------------------------------------------------------------

def default_doc():
	"""A fresh , empty document : the S-D ramp plus the staging tier."""
	tiers = [ { "id": i , "label": l , "desc": "" , "color": c , "items": [] }
		for i , l , c in DEFAULT_TIERS ]
	tiers.append( { "id": STAGING_ID , "label": STAGING_LABEL , "desc":
		"Added but not ranked yet" , "color": "#8b94a3" , "locked": True , "items": [] } )
	return {
		"version":    VERSION ,
		"updated_at": "" ,
		"columns":    [ { "id": "notes" , "label": "Notes" } ] ,
		"vocab":      { "tags": [] , "mods": [] } ,
		"tiers":      tiers ,
	}


_SLUG_RE = re.compile( r"[^a-z0-9]+" )


def slug( s , fallback="tier" ):
	"""Filesystem / id-safe slug of a label ( 'A - fMRI decodes' -> 'a-fmri-decodes' )."""
	out = _SLUG_RE.sub( "-" , ( s or "" ).strip().lower() ).strip( "-" )
	return out[ :48 ] or fallback


def _clean_str( v , limit=None ):
	s = v if isinstance( v , str ) else ( "" if v is None else str( v ) )
	s = s.replace( "\x00" , "" )
	return s[ :limit ] if limit else s


def _clean_list( v , limit=64 ):
	"""A list of short , non-empty , de-duped strings ( modality / tag lists ) ,
	ALPHABETICAL. A row's chips read the same way every time you look at it --
	the order you happened to add them in isn't information , and a list that
	shuffles itself is one you have to re-read on every row."""
	seen , out = set() , []
	for item in ( v or [] ):
		s = _clean_str( item , 120 ).strip()
		if not s or s.lower() in seen:
			continue
		seen.add( s.lower() )
		out.append( s )
		if len( out ) >= limit:
			break
	return sorted( out , key=str.lower )


def _clean_item( raw , col_ids , seen_keys ):
	"""One row. Returns None when it carries no key ( nothing to hang links or
	de-duplication off ) or when its key already appeared in an earlier tier."""
	if not isinstance( raw , dict ):
		return None
	key = _clean_str( raw.get( "key" ) , 400 ).strip()
	if not key or key in seen_keys:
		return None
	seen_keys.add( key )
	fields = {}
	src = raw.get( "fields" ) if isinstance( raw.get( "fields" ) , dict ) else {}
	for cid in col_ids:
		fields[ cid ] = _clean_str( src.get( cid ) , 20000 )
	year = raw.get( "year" )
	try:
		year = int( year ) if str( year or "" ).strip() else None
	except ( TypeError , ValueError ):
		year = None
	return {
		"key":      key ,
		"title":    _clean_str( raw.get( "title" ) , 1000 ) ,
		"doi":      _clean_str( raw.get( "doi" ) , 400 ) ,
		"wid":      _clean_str( raw.get( "wid" ) , 60 ) ,
		# Only set for a paper that ISN'T in the library : the open-access PDF url
		# the pool row carried. Library papers are served from /pdf?key= instead ,
		# which the page works out from the key alone.
		"pdf":      _clean_str( raw.get( "pdf" ) , 1000 ) ,
		"year":     year ,
		"journal":  _clean_str( raw.get( "journal" ) , 400 ) ,
		"mods":     _clean_list( raw.get( "mods" ) ) ,
		"tags":     _clean_list( raw.get( "tags" ) ) ,
		# "you put this one where it is" -- set when a row is dragged or given a
		# number by hand , so /sort can drop new papers after the last one you've
		# actually placed rather than at the end of everything.
		"placed":   bool( raw.get( "placed" ) ) ,
		"fields":   fields ,
		"added_at": _clean_str( raw.get( "added_at" ) , 40 ) ,
	}


def _clean_columns( raw ):
	"""The free-text columns , 'notes' always first and always present."""
	out , seen = [] , set()
	for c in ( raw or [] ):
		if isinstance( c , str ):
			c = { "id": slug( c , "col" ) , "label": c }
		if not isinstance( c , dict ):
			continue
		label = _clean_str( c.get( "label" ) , 60 ).strip()
		cid   = slug( c.get( "id" ) or label , "col" )
		if not cid or cid in seen:
			continue
		seen.add( cid )
		out.append( { "id": cid , "label": label or cid } )
	if "notes" not in seen:
		out.insert( 0 , { "id": "notes" , "label": "Notes" } )
	return out[ :16 ]


def _clean_tiers( raw , col_ids ):
	"""Every tier , in order , with the staging tier guaranteed present and last."""
	out , seen_ids , seen_keys = [] , set() , set()
	for t in ( raw or [] ):
		if not isinstance( t , dict ):
			continue
		label = _clean_str( t.get( "label" ) , 60 ).strip()
		tid   = slug( t.get( "id" ) or label , "tier" )
		while tid in seen_ids:                      # a rename can collide
			tid = f"{tid}-2"
		seen_ids.add( tid )
		items = []
		for it in ( t.get( "items" ) or [] ):
			row = _clean_item( it , col_ids , seen_keys )
			if row:
				items.append( row )
		out.append( {
			"id":     tid ,
			"label":  label or tid.upper() ,
			"desc":   _clean_str( t.get( "desc" ) , 400 ) ,
			"color":  _clean_str( t.get( "color" ) , 32 ) or "#8b94a3" ,
			"locked": bool( t.get( "locked" ) ) or tid == STAGING_ID ,
			"items":  items ,
		} )
	# The staging tier is structural : re-add it if a bad write dropped it , and
	# keep it pinned to the bottom of the board.
	staging = [ t for t in out if t[ "id" ] == STAGING_ID ]
	out     = [ t for t in out if t[ "id" ] != STAGING_ID ]
	if not staging:
		staging = [ { "id": STAGING_ID , "label": STAGING_LABEL ,
			"desc": "Added but not ranked yet" , "color": "#8b94a3" ,
			"locked": True , "items": [] } ]
	return out + staging


def normalize( doc ):
	"""Coerce anything POSTed at us into the documented shape. Never raises :
	a malformed field is dropped or defaulted , not an error , because the
	alternative is refusing to save somebody's afternoon of curation."""
	if not isinstance( doc , dict ):
		return default_doc()
	columns = _clean_columns( doc.get( "columns" ) )
	col_ids = [ c[ "id" ] for c in columns ]
	vocab   = doc.get( "vocab" ) or {}
	return {
		"version":    VERSION ,
		"updated_at": _clean_str( doc.get( "updated_at" ) , 40 ) ,
		"columns":    columns ,
		# Two named lists , same shape : the tags you invent , and the modality
		# labels ( config/methods.py ) . Only their COLOURS live here -- the
		# modality names themselves come from the config , this just remembers
		# what colour you painted each chip.
		"vocab":      { "tags": _clean_vocab( vocab.get( "tags" ) , 200 ) ,
		                "mods": _clean_vocab( vocab.get( "mods" ) , 200 ) } ,
		"tiers":      _clean_tiers( doc.get( "tiers" ) , col_ids ) ,
	}


def _clean_vocab( raw , limit ):
	"""A { name , color } list : de-duped by name , plain strings accepted."""
	out , seen = [] , set()
	for t in ( raw or [] ):
		if isinstance( t , str ):
			t = { "name": t }
		if not isinstance( t , dict ):
			continue
		name = _clean_str( t.get( "name" ) , 60 ).strip()
		if not name or name.lower() in seen:
			continue
		seen.add( name.lower() )
		out.append( { "name": name , "color": _clean_str( t.get( "color" ) , 32 ) } )
		if len( out ) >= limit:
			break
	return out


def item_count( doc ):
	"""How many papers the document holds , across every tier."""
	return sum( len( t.get( "items" ) or [] ) for t in ( doc or {} ).get( "tiers" ) or [] )


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load( args ):
	"""The stored document , normalized -- or a seeded empty one when there is
	no file yet ( or it is unreadable , in which case the unreadable file is
	left alone rather than overwritten )."""
	p = tiers_path( args )
	if not p.exists():
		return default_doc()
	try:
		return normalize( utils.read_json( p ) )
	except Exception:
		return default_doc()


def save( args , doc ):
	"""Normalize , snapshot the version being replaced , then write. Returns the
	normalized document that actually hit disk."""
	doc = normalize( doc )
	doc[ "updated_at" ] = time.strftime( "%Y-%m-%dT%H:%M:%S" , time.localtime() )
	p = tiers_path( args )
	p.parent.mkdir( parents=True , exist_ok=True )
	_snapshot( args , p )
	utils.write_json( p , doc )
	return doc


def _snapshot( args , current ):
	"""Copy the version we are about to overwrite into tiers-history/ , then
	trim to the newest HISTORY_KEEP. Best-effort : a failure here must never
	block the save itself."""
	if not current.exists():
		return
	try:
		d = history_dir( args )
		d.mkdir( parents=True , exist_ok=True )
		stamp = time.strftime( "%Y%m%d-%H%M%S" )
		dest  = d.joinpath( f"tiers-{stamp}.json" )
		if not dest.exists():
			dest.write_bytes( current.read_bytes() )
		old = sorted( d.glob( "tiers-*.json" ) )[ : -HISTORY_KEEP ] if HISTORY_KEEP else []
		for f in old:
			try:
				f.unlink()
			except Exception:
				pass
	except Exception as e:
		print( f"tiers :: could not snapshot previous version ( {e} )" )
