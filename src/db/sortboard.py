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
    "options":  { "auto_move": false ,         # re-group a row when its tags change
                  "add_where": "bottom" ,      # where a paper added from search lands
                  "fold_cols": [ "code" ] ,    # columns the page can fold away
                  "sheet_url": "" } ,          # the Google Sheet last imported from
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

import re
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
	fold  = opts.get( "fold_cols" ) if isinstance( opts.get( "fold_cols" ) , list ) else []
	sheet = tiers_db._clean_str( opts.get( "sheet_url" ) , 500 ).strip()
	return {
		"version":    VERSION ,
		"updated_at": tiers_db._clean_str( doc.get( "updated_at" ) , 40 ) ,
		"options":    {
			"auto_move": bool( opts.get( "auto_move" ) ) ,
			# Where a paper added from search lands : the end of the list , the
			# top , or straight after the last row you placed by hand.
			"add_where": where if where in ( "bottom" , "top" , "placed" ) else "bottom" ,
			# The columns a Google Sheet import brought in ( Methods summary ,
			# Datasets , Code ) : wide enough that the page folds them away behind
			# one show / hide button rather than drawing them all the time.
			"fold_cols": [ c for c in col_ids if c in fold ] ,
			# The sheet the last one came from , so the next import is one click.
			# Only ever a docs.google.com link -- see src/db/gsheet.py .
			"sheet_url": sheet if sheet.startswith( "https://docs.google.com/spreadsheets/" ) else "" ,
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
		# To the millisecond : an agent's batches can land several to a second ,
		# and each one's predecessor is a version it may want to restore. The
		# digits run straight on from the seconds , so these names still sort in
		# time order among the older sort-YYYYmmdd-HHMMSS.json ones.
		t    = time.time()
		dest = d.joinpath( f"sort-{time.strftime( '%Y%m%d-%H%M%S' , time.localtime( t ) )}"
			f"{int( t * 1000 ) % 1000:03d}.json" )
		if not dest.exists():
			dest.write_bytes( current.read_bytes() )
		for f in sorted( d.glob( "sort-*.json" ) )[ : -HISTORY_KEEP ] if HISTORY_KEEP else []:
			try:
				f.unlink()
			except Exception:
				pass
	except Exception as e:
		print( f"sort :: could not snapshot previous version ( {e} )" )


# ---------------------------------------------------------------------------
# History , for an agent to read back and restore from
# ---------------------------------------------------------------------------

_SNAP_RE = re.compile( r"^sort-\d{8}-\d{6}(\d{3})?$" )


def snapshots( args ):
	"""The saved versions in sort-history/ , newest first : { id , at , list ,
	shelf }. An unreadable one is listed with its counts as None."""
	out = []
	try:
		files = sorted( history_dir( args ).glob( "sort-*.json" ) , reverse=True )
	except Exception:
		return out
	for f in files:
		if not _SNAP_RE.match( f.stem ):
			continue
		d = f.stem[ 5: ]
		row = { "id": f.stem , "at": f"{d[ :4 ]}-{d[ 4:6 ]}-{d[ 6:8 ]}T{d[ 9:11 ]}:{d[ 11:13 ]}:{d[ 13:15 ]}"
			+ ( f".{d[ 15: ]}" if len( d ) > 15 else "" ) ,
			"list": None , "shelf": None }
		try:
			doc = utils.read_json( f )
			row[ "list" ]  = len( doc.get( "items" ) or [] )
			row[ "shelf" ] = len( doc.get( "staging" ) or [] )
		except Exception:
			pass
		out.append( row )
	return out


def read_snapshot( args , sid ):
	"""One saved version by its id ( as snapshots() lists it ) , normalized.
	None for an id that isn't one -- never a path outside sort-history/ ."""
	if not isinstance( sid , str ) or not _SNAP_RE.match( sid ):
		return None
	p = history_dir( args ).joinpath( sid + ".json" )
	if not p.exists():
		return None
	return normalize( utils.read_json( p ) )


# ---------------------------------------------------------------------------
# Merge : a page's save over a board that moved underneath it
# ---------------------------------------------------------------------------
#
# The page posts the WHOLE document. When something else wrote in between -- an
# agent's ops , another tab -- that document is missing their change , and
# writing it as-is would quietly undo it. So the page says which version it
# started from ( base_rev ) and the server merges three ways : the BASE it
# started from , MINE ( what it posted ) and THEIRS ( what is stored now ).
#
# The rule everywhere is "mine if I changed it , theirs otherwise" , at the
# finest grain the document has :
#
#   where a row is   list / shelf / gone -- whichever side moved it wins , so a
#                    delete on either side beats an edit on the other
#   a row's fields   title , year , ... each on its own ; each CELL on its own
#   a row's tags     a set : theirs , less what I took off , plus what I put on
#   the list order   mine if I reordered the rows we share , theirs otherwise ;
#                    the rows only the other side has go in after the nearest
#                    row before them that survived
#   columns , tags   the same keyed merge , by column id / tag name
#   options          key by key

_ROW_SCALARS = ( "title" , "authors" , "doi" , "wid" , "pdf" , "year" , "journal" ,
	"placed" , "pending" , "added_at" )


def _pick( b , m , t ):
	return m if m != b else t


def _weave( primary , secondary , keep ):
	"""`primary`'s order , restricted to `keep` , with whatever only `secondary`
	has slotted in after its nearest predecessor there."""
	out  = [ k for k in primary if k in keep ]
	have = set( out )
	for i , k in enumerate( secondary ):
		if k not in keep or k in have:
			continue
		at = 0
		for j in range( i - 1 , -1 , -1 ):
			if secondary[ j ] in have:
				at = out.index( secondary[ j ] ) + 1
				break
		out.insert( at , k )
		have.add( k )
	out.extend( sorted( k for k in keep if k not in have ) )
	return out


def _reordered( base , mine ):
	"""Did MINE change the relative order of the keys it shares with BASE ?"""
	shared = set( base ) & set( mine )
	return [ k for k in mine if k in shared ] != [ k for k in base if k in shared ]


def _order( b , m , t , keep ):
	if _reordered( b , m ):
		return _weave( m , t , keep )
	return _weave( t , m , keep )


def _merge_keyed( b , m , t , key , entry ):
	"""A keyed list ( columns , vocab tags ) : present unless a side that had it
	in BASE dropped it , each entry merged by `entry( b , m , t )` , in order."""
	bm = { key( x ): x for x in b }
	mm = { key( x ): x for x in m }
	tm = { key( x ): x for x in t }
	keep = set()
	for k in set( mm ) | set( tm ):
		if k in mm and k in tm:
			keep.add( k )
		elif k in mm and k not in bm:    # I added it
			keep.add( k )
		elif k in tm and k not in bm:    # they added it
			keep.add( k )
	order = _order( [ key( x ) for x in b ] , [ key( x ) for x in m ] ,
		[ key( x ) for x in t ] , keep )
	out = []
	for k in order:
		if k in mm and k in tm:
			out.append( entry( bm.get( k ) , mm[ k ] , tm[ k ] ) )
		else:
			out.append( mm.get( k ) or tm.get( k ) )
	return out


def _merge_dict( b , m , t ):
	b , m , t = b or {} , m or {} , t or {}
	return { k: _pick( b.get( k ) , m.get( k ) , t.get( k ) ) for k in list( t ) + [ k for k in m if k not in t ] }


def _merge_row( b , m , t ):
	"""One paper both sides still have."""
	if b is None:
		# Both added it : mine where it says something , theirs otherwise.
		row = dict( t )
		for f in _ROW_SCALARS:
			if m.get( f ) not in ( None , "" , False ):
				row[ f ] = m[ f ]
		row[ "fields" ] = { k: ( m.get( "fields" , {} ).get( k ) or t.get( "fields" , {} ).get( k ) or "" )
			for k in set( m.get( "fields" ) or {} ) | set( t.get( "fields" ) or {} ) }
		row[ "tags" ] = list( { s.lower(): s for s in ( t.get( "tags" ) or [] ) + ( m.get( "tags" ) or [] ) }.values() )
		return row
	row = dict( t )
	for f in _ROW_SCALARS:
		row[ f ] = _pick( b.get( f ) , m.get( f ) , t.get( f ) )
	row[ "fields" ] = _merge_dict( b.get( "fields" ) , m.get( "fields" ) , t.get( "fields" ) )
	bt = { s.lower() for s in b.get( "tags" ) or [] }
	mt = { s.lower() for s in m.get( "tags" ) or [] }
	off  = bt - mt
	tags = [ s for s in ( t.get( "tags" ) or [] ) if s.lower() not in off ]
	have = { s.lower() for s in tags }
	for s in m.get( "tags" ) or []:
		if s.lower() not in bt and s.lower() not in have:
			tags.append( s )
			have.add( s.lower() )
	row[ "tags" ] = tags
	return row


def merge( base , mine , theirs ):
	"""Three-way merge of whole board documents -- see the block comment above.
	All three are expected normalized ; the result is normalized."""
	base , mine , theirs = ( normalize( d ) for d in ( base , mine , theirs ) )

	def where( doc ):
		out = {}
		for loc in ( "items" , "staging" ):
			for r in doc.get( loc ) or []:
				out[ r[ "key" ] ] = ( loc , r )
		return out
	B , M , T = where( base ) , where( mine ) , where( theirs )
	result = {}
	for k in set( B ) | set( M ) | set( T ):
		bl = B[ k ][ 0 ] if k in B else None
		ml = M[ k ][ 0 ] if k in M else None
		tl = T[ k ][ 0 ] if k in T else None
		loc = ml if ml != bl else tl
		if loc is None:
			continue
		b , m , t = ( X[ k ][ 1 ] if k in X else None for X in ( B , M , T ) )
		row = _merge_row( b , m , t ) if ( m is not None and t is not None ) else ( m or t )
		result[ k ] = ( loc , row )

	lists = {}
	for loc in ( "items" , "staging" ):
		keep = { k for k , ( l , _ ) in result.items() if l == loc }
		seq  = lambda d: [ r[ "key" ] for r in d.get( loc ) or [] ]
		lists[ loc ] = [ result[ k ][ 1 ] for k in _order( seq( base ) , seq( mine ) , seq( theirs ) , keep ) ]

	columns = _merge_keyed( base[ "columns" ] , mine[ "columns" ] , theirs[ "columns" ] ,
		lambda c: c[ "id" ] ,
		lambda b , m , t: { "id": t[ "id" ] , "label": _pick( ( b or {} ).get( "label" ) , m[ "label" ] , t[ "label" ] ) } )
	tags = _merge_keyed( base[ "vocab" ][ "tags" ] , mine[ "vocab" ][ "tags" ] , theirs[ "vocab" ][ "tags" ] ,
		lambda v: v[ "name" ].lower() ,
		lambda b , m , t: { "name": _pick( ( b or {} ).get( "name" ) , m[ "name" ] , t[ "name" ] ) ,
		                    "color": _pick( ( b or {} ).get( "color" ) , m.get( "color" ) , t.get( "color" ) ) } )
	return normalize( {
		"version":    VERSION ,
		"updated_at": theirs.get( "updated_at" ) ,
		"options":    _merge_dict( base[ "options" ] , mine[ "options" ] , theirs[ "options" ] ) ,
		"columns":    columns ,
		"items":      lists[ "items" ] ,
		"staging":    lists[ "staging" ] ,
		"vocab":      { "tags": tags } ,
	} )
