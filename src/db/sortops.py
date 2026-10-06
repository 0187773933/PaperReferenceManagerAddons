"""
The /sort board , one named operation at a time -- what an AGENT edits it with.

The page owns the board document and posts it back whole ( src/db/sortboard.py ).
That is the right granularity for a person dragging rows around , and the wrong
one for a script : to move one paper it would have to fetch the board , redo the
page's own rules for where a paper lands , what it is pre-tagged with and which
cells it arrives filled in , and then post the lot back over whatever an open tab
was holding. So this module is those rules , in Python , behind small named ops
( POST /api/sort/ops ) :

  { "ops": [ { "op": "add" , "paper": "10.1038/..." , "at": 3 } ,
             { "op": "set_cell" , "row": "10.1038/..." , "col": "Notes" ,
               "value": "decodes covert speech" , "mode": "append" } ,
             { "op": "tag" , "rows": [ "#1" , "#2" ] , "add": [ "premier" ] } ] }

A batch is ATOMIC : the server runs it on a copy of the board , and only a batch
in which every op succeeded is saved -- once , as one version , with one history
snapshot behind it ( see BoardState.apply in src/server/server.py ). A failing op
names its index in the batch and nothing is written.

Every op mirrors the page's own function of the same job ( addPaper , moveTo ,
toggleTagOnSelection , the column header's rename / delete , stageRefs , ... ) ,
named in each op's docstring , so an agent's edit and a person's land the board
in the same state. The few things only a page can decide stay in the page : a
new tag is stored with color "" , the page's own "auto" colour , because picking
a palette colour is presentation.

Everything here is a pure function of the document plus a small context ( Ctx ,
below ) that the server fills in with the library lookups -- so it runs , and is
tested , without a server.

The op catalog GET /api/sort/schema serves is generated from the OPS registry at
the bottom , so the description an agent reads can't drift from what runs.
"""

import csv
import io
import re
import time
import unicodedata

from ..utils import utils
from .       import tiers as tiers_db


class OpError( Exception ):
	"""One op could not be applied. The message is for whoever sent it."""


class OpFailure( Exception ):
	"""A batch stopped at op `index` -- carries what the HTTP answer reports."""

	def __init__( self , index , op , msg ):
		super().__init__( msg )
		self.index = index
		self.op    = op
		self.msg   = msg


class Ctx:
	"""What an op needs from outside the document. The server fills these in
	( src/server/server.py :: _sort_ctx ) ; a test can pass plain functions.

	  resolve( spec )                 -> a hit dict for `add` , or raise OpError
	  meta( keys , mods , links , titles ) -> { key: /api/paper-meta entry }
	  parse_refs( text )              -> [ ref , ... ] as /api/refs/parse gives
	  tiers_doc()                     -> the tier list document
	  list_doc( slug )                -> another sort list's document ( "" / "main"
	                                     is the main board ) , or raise OpError
	  remaining( doc )                -> the library papers not on `doc` , newest
	                                     first , each with its modality stamp ( mods )
	  modalities                      -> the modality vocabulary ( methods.py )
	"""

	def __init__( self , resolve=None , meta=None , parse_refs=None , tiers_doc=None ,
			list_doc=None , remaining=None , modalities=None ):
		self.resolve    = resolve    or ( lambda spec: _no( "paper lookup" ) )
		self.meta       = meta       or ( lambda keys , mods=False , links=False , titles=None: {} )
		self.parse_refs = parse_refs or ( lambda text: _no( "reference parsing" ) )
		self.tiers_doc  = tiers_doc  or ( lambda: { "tiers": [] , "columns": [] } )
		self.list_doc   = list_doc   or ( lambda slug: _no( "the other sort lists" ) )
		self.remaining  = remaining  or ( lambda doc: _no( "the library" ) )
		self.modalities = list( modalities or [] )


def _no( what ):
	raise OpError( f"{what} is not available on this server" )


# ---------------------------------------------------------------------------
# Small helpers , ported from sort.html so both sides agree
# ---------------------------------------------------------------------------

def lc( s ):
	return str( s or "" ).lower()


def now_iso():
	return time.strftime( "%Y-%m-%dT%H:%M:%S" , time.localtime() )


def fold_text( s ):
	"""foldText ( /static/common.js ) : case , accents and punctuation gone."""
	return re.sub( r"[^a-z0-9]+" , "" ,
		unicodedata.normalize( "NFKD" , str( s or "" ) ).lower() )


def text_has( hay , q ):
	"""textHas ( /static/common.js ) -- the page's filter box."""
	hay = str( hay or "" ).lower()
	q   = str( q or "" ).strip().lower()
	if not q or q in hay:
		return True
	fq = fold_text( q )
	return bool( fq ) and fq in fold_text( hay )


def tag_sig( it ):
	"""tagSig : a row's tag set , as one comparable string."""
	return "|".join( sorted( lc( t ) for t in ( it.get( "tags" ) or [] ) ) )


def has_tag( it , v ):
	return any( lc( t ) == lc( v ) for t in ( it.get( "tags" ) or [] ) )


MATCHES = ( "any" , "all" , "none" )


def tags_match( it , want , match="any" ):
	"""tagMatch : the tag filter , `want` ( lowercased ) against a row's tags.
	any = it carries one of them , all = every one , none = not one of them.
	Nothing wanted lets every row through."""
	if not want:
		return True
	have = { lc( t ) for t in it.get( "tags" ) or [] }
	if match == "all":
		return all( w in have for w in want )
	if match == "none":
		return not any( w in have for w in want )
	return any( w in have for w in want )


def sort_tags( it ):
	it[ "tags" ] = sorted( it.get( "tags" ) or [] , key=lc )


def _items( doc ):
	doc.setdefault( "items" , [] )
	return doc[ "items" ]


def _staged( doc ):
	if not isinstance( doc.get( "staging" ) , list ):
		doc[ "staging" ] = []
	return doc[ "staging" ]


def _vocab( doc ):
	v = doc.setdefault( "vocab" , {} )
	if not isinstance( v.get( "tags" ) , list ):
		v[ "tags" ] = []
	return v[ "tags" ]


def _options( doc ):
	if not isinstance( doc.get( "options" ) , dict ):
		doc[ "options" ] = { "auto_move": False , "add_where": "bottom" }
	return doc[ "options" ]


def vocab_entry( doc , name ):
	return next( ( t for t in _vocab( doc ) if lc( t.get( "name" ) ) == lc( name ) ) , None )


def remember_tag( doc , value ):
	"""rememberTag : every tag used or invented joins the board's tag list. With
	colour "" -- the page's own auto colour ( see the module doc )."""
	if not vocab_entry( doc , value ):
		_vocab( doc ).append( { "name": value , "color": "" } )


def tag_options( doc , ctx ):
	"""tagOptions : the order the page's tag bar draws -- the board's own list ,
	then the modality vocabulary , then anything in use."""
	out , seen = [] , set()

	def push( v ):
		if v and lc( v ) not in seen:
			seen.add( lc( v ) )
			out.append( v )
	for t in _vocab( doc ):
		push( t.get( "name" ) )
	for v in ctx.modalities:
		push( v )
	for it in _items( doc ) + _staged( doc ):
		for t in it.get( "tags" ) or []:
			push( t )
	return out


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------

def find_column( doc , col ):
	"""A column by id , or by label ( case-insensitive ) , or by the id its label
	would slug to. -> the column dict ; OpError naming the real ones otherwise."""
	if not isinstance( col , str ) or not col.strip():
		raise OpError( "`col` must be a column id or label" )
	cols = doc.get( "columns" ) or []
	want = col.strip()
	for c in cols:
		if c[ "id" ] == want:
			return c
	for c in cols:
		if lc( c.get( "label" ) ) == lc( want ):
			return c
	s = tiers_db.slug( want , "col" )
	for c in cols:
		if c[ "id" ] == s:
			return c
	raise OpError( f"no column {want!r} -- the board has " +
		", ".join( f"{c['label']!r} ( id {c['id']} )" for c in cols ) )


def _fill_col( doc , pattern ):
	"""fillCol : the Code / Datasets column , by id first then by label."""
	cols = doc.get( "columns" ) or []
	for c in cols:
		if pattern.search( c[ "id" ] ):
			return c[ "id" ]
	for c in cols:
		if pattern.search( c.get( "label" ) or "" ):
			return c[ "id" ]
	return None


FILL_COLS = { "code": re.compile( r"^code$" , re.I ) ,
              "datasets": re.compile( r"^datasets?$" , re.I ) }


def _cell_text( values ):
	"""cellText : one line each , first spelling wins."""
	seen , out = set() , []
	for v in values:
		v = str( v or "" ).strip()
		if v and lc( v ) not in seen:
			seen.add( lc( v ) )
			out.append( v )
	return "\n".join( out )


def link_line( label , url ):
	"""linkLine : a name with its link , or just the link when the name says
	nothing the URL doesn't."""
	label = str( label or "" ).strip()
	u     = fold_text( url )
	words = [ w for w in re.split( r"[^a-z0-9]+" ,
		re.sub( r"^hf:" , "" , unicodedata.normalize( "NFKD" , label ).lower() ) )
		if w and not re.match( r"^(www|https?)$" , w ) ]
	return url if all( w in u for w in words ) else f"{label} — {url}"


def prefill( doc , it , m ):
	"""prefill : write the Code / Datasets the scans found into `it`'s EMPTY
	cells. -> the column ids it wrote."""
	if not m or "code" not in m:       # asked for without links -- nothing to write
		return []
	names = []
	for n in ( m.get( "names" ) or [] ):
		n = { "name": n , "url": "" } if isinstance( n , str ) else ( n or {} )
		names.append( link_line( n.get( "name" ) , n[ "url" ] ) if n.get( "url" ) else n.get( "name" ) )
	want = {
		"code":     _cell_text( l.get( "url" ) for l in ( m.get( "code" ) or [] ) ) ,
		"datasets": _cell_text( [ l.get( "url" ) for l in ( m.get( "data" ) or [] ) ] + names ) ,
	}
	f , wrote = it.setdefault( "fields" , {} ) , []
	for k , text in want.items():
		cid = _fill_col( doc , FILL_COLS[ k ] )
		if cid and text and not str( f.get( cid ) or "" ).strip():
			f[ cid ] = text
			wrote.append( cid )
	return wrote


# ---------------------------------------------------------------------------
# Rows : selectors and positions
# ---------------------------------------------------------------------------

def locate( doc , sel ):
	"""A row selector -> ( "items" | "staging" , index , row ).

	  "<key>"   the row's exact key
	  "<doi>"   its key or DOI , case-insensitive , in any spelling a DOI comes in
	  "#N"      position N in YOUR order ( the list only )

	The list is searched before the staging shelf."""
	if not isinstance( sel , str ) or not sel.strip():
		raise OpError( "a row selector must be a key , a DOI or \"#N\"" )
	s     = sel.strip()
	lists = ( ( "items" , _items( doc ) ) , ( "staging" , _staged( doc ) ) )
	for where , rows in lists:
		for i , it in enumerate( rows ):
			if it.get( "key" ) == s:
				return where , i , it
	m = re.match( r"^#\s*(\d+)$" , s )
	if m:
		n , items = int( m.group( 1 ) ) , _items( doc )
		if 1 <= n <= len( items ):
			return "items" , n - 1 , items[ n - 1 ]
		raise OpError( f"{s} is off the list -- it holds {len( items )} paper(s)" )
	doi  = lc( utils.normalize_doi( s ) or "" )
	hits = []
	for where , rows in lists:
		for i , it in enumerate( rows ):
			k = lc( it.get( "key" ) )
			d = lc( utils.normalize_doi( it.get( "doi" ) or "" ) or it.get( "doi" ) or "" )
			if k == lc( s ) or ( doi and ( k == doi or d == doi ) ):
				hits.append( ( where , i , it ) )
	if len( hits ) == 1:
		return hits[ 0 ]
	if len( hits ) > 1:
		raise OpError( f"{s!r} matches {len( hits )} rows ( " +
			", ".join( h[ 2 ][ "key" ] for h in hits ) + " ) -- use the exact key" )
	raise OpError( f"no row {s!r} on the list or the staging shelf" )


def _on_list( doc , sel ):
	where , i , it = locate( doc , sel )
	if where != "items":
		raise OpError( f"{sel!r} is on the staging shelf , not the list -- use stage_add first" )
	return i , it


def _rows_arg( p , name="rows" ):
	v = p.get( name )
	if isinstance( v , str ):
		v = [ v ]
	if not isinstance( v , list ) or not v or not all( isinstance( x , str ) for x in v ):
		raise OpError( f"`{name}` must be a row selector or a list of them" )
	return v


def _row_or_rows( p ):
	if "row" in p and "rows" in p:
		raise OpError( "give `row` or `rows` , not both" )
	return _rows_arg( p , "rows" if "rows" in p else "row" )


def placed_index( doc ):
	"""insertIndex for "placed" : right after the last row placed by hand."""
	last = -1
	for i , it in enumerate( _items( doc ) ):
		if it.get( "placed" ):
			last = i
	return last + 1


def pin( doc , at , moving=() ):
	"""Resolve a {"after": row} / {"before": row} position to the anchor's KEY
	while every row is still where it was -- "#5" means the row that is #5 now ,
	not the one that slides up into #5 once the moving rows are lifted out.
	Anything else passes through for insert_index."""
	if isinstance( at , dict ) and len( at ) == 1 and ( "after" in at or "before" in at ):
		side = "after" if "after" in at else "before"
		i , anchor = _on_list( doc , at[ side ] )
		if anchor[ "key" ] in moving:
			raise OpError( "a row can't be placed relative to itself" )
		return { side + "_key": anchor[ "key" ] }
	return at


def insert_index( doc , at , moving=() ):
	"""Where to insert into the list as it stands NOW ( any rows being moved
	already taken out -- pin() their anchors first ). `at` :

	  N                    the row ends up at position N ( 1-based , clamped )
	  "top" | "bottom"     the ends of the list
	  "placed"             after the last row placed by hand
	  { "after": row } / { "before": row }
	  None                 the board's "add to…" setting ( options.add_where )"""
	items = _items( doc )
	if at is None:
		at = _options( doc ).get( "add_where" ) or "bottom"
	if isinstance( at , bool ):
		raise OpError( "`at` / `to` can't be true / false" )
	if isinstance( at , int ):
		return max( 0 , min( at - 1 , len( items ) ) )
	if isinstance( at , str ):
		if at == "top":
			return 0
		if at == "bottom":
			return len( items )
		if at == "placed":
			return placed_index( doc )
		if re.match( r"^#?\d+$" , at.strip() ):
			return insert_index( doc , int( at.strip().lstrip( "#" ) ) )
		raise OpError( f"unknown position {at!r} -- use N , \"top\" , \"bottom\" , \"placed\" , "
			"{\"after\": row} or {\"before\": row}" )
	if isinstance( at , dict ) and len( at ) == 1 and ( "after_key" in at or "before_key" in at ):
		key = at.get( "after_key" , at.get( "before_key" ) )
		i   = next( ( n for n , x in enumerate( items ) if x.get( "key" ) == key ) , None )
		if i is None:
			raise OpError( "the row to place it next to is gone" )
		return i + 1 if "after_key" in at else i
	if isinstance( at , dict ):
		return insert_index( doc , pin( doc , at , moving ) , moving )
	raise OpError( f"unknown position {at!r}" )


def auto_move( doc , key ):
	"""autoMove : with Auto-move on , slide a row whose tags just changed down
	to sit with the others carrying exactly the same tags."""
	if not _options( doc ).get( "auto_move" ):
		return
	items = _items( doc )
	idx   = next( ( i for i , x in enumerate( items ) if x.get( "key" ) == key ) , -1 )
	if idx < 0:
		return
	sig  = tag_sig( items[ idx ] )
	last = -1
	for i , x in enumerate( items ):
		if i != idx and tag_sig( x ) == sig:
			last = i
	if last < 0:
		return
	it = items.pop( idx )
	items.insert( last + 1 if last < idx else last , it )


def _pos_of( doc , key ):
	for i , it in enumerate( _items( doc ) ):
		if it.get( "key" ) == key:
			return i + 1
	return None


def new_item( doc , hit ):
	"""newItem : a row for a paper , every column present and empty."""
	try:
		year = int( hit.get( "year" ) ) if str( hit.get( "year" ) or "" ).strip() else None
	except ( TypeError , ValueError ):
		year = None
	return {
		"key":      hit[ "key" ] ,
		"title":    hit.get( "title" ) or "" ,
		# A pool row's authors are a list ; only a typed-in paper's are a string.
		"authors":  hit.get( "authors" ) if isinstance( hit.get( "authors" ) , str ) else "" ,
		"doi":      hit.get( "doi" ) or "" ,
		"wid":      hit.get( "wid" ) or "" ,
		# A library paper's pdf is a local path the page asks /pdf?key= for ; only
		# an outside paper's open-access url is stored.
		"pdf":      "" if hit.get( "pdf_local" ) or hit.get( "in_library" ) else ( hit.get( "pdf" ) or "" ) ,
		"year":     year ,
		"journal":  hit.get( "journal" ) or "" ,
		"tags":     [] ,
		"fields":   { c[ "id" ]: "" for c in doc.get( "columns" ) or [] } ,
		"added_at": now_iso() ,
	}


def _str_list( v , name ):
	if v is None:
		return []
	if isinstance( v , str ):
		v = [ v ]
	if not isinstance( v , list ) or not all( isinstance( x , str ) for x in v ):
		raise OpError( f"`{name}` must be a string or a list of strings" )
	return [ x.strip() for x in v if x.strip() ]


def _set_cells( doc , it , cells ):
	if cells is None:
		return
	if not isinstance( cells , dict ):
		raise OpError( "`cells` must be an object of { column: text }" )
	for col , val in cells.items():
		c = find_column( doc , col )
		it.setdefault( "fields" , {} )[ c[ "id" ] ] = "" if val is None else str( val )


def _brief( where , it , doc ):
	out = { "key": it.get( "key" ) , "where": "list" if where == "items" else "shelf" }
	if where == "items":
		out[ "pos" ] = _pos_of( doc , it.get( "key" ) )
	return out


# ---------------------------------------------------------------------------
# The ops
# ---------------------------------------------------------------------------

def op_add( doc , ctx , p ):
	"""Put a paper on the list ( addPaper ). `paper` is a key , DOI , OpenAlex
	WID or a title -- looked up in your library , then in the papers it cites and
	is cited by -- or an object { title , doi , year , journal , authors , wid ,
	pdf , key } for a paper found nowhere. Lands at `at` , else where the board's
	"add to…" setting says. Arrives tagged with its modality stamp ( auto_tag ) and
	with its Code / Datasets cells filled ( prefill ) , as from the search box.
	A paper on the staging shelf is moved onto the list , tags and notes kept."""
	paper = p.get( "paper" )
	if isinstance( paper , dict ):
		hit = dict( paper )
		hit[ "key" ] = str( hit.get( "key" ) or utils.normalize_doi( hit.get( "doi" ) or "" )
			or hit.get( "wid" ) or "" ).strip()
		if not hit[ "key" ]:
			if not str( hit.get( "title" ) or "" ).strip():
				raise OpError( "a paper object needs a key , doi , wid or title" )
			from .refparse import synth_key
			hit[ "key" ] = synth_key( hit[ "title" ] )
	elif isinstance( paper , str ) and paper.strip():
		hit = None
		# A paper the board already holds -- on the list , or staged from a
		# reference list the library may not have -- needs no lookup.
		try:
			where , i , it = locate( doc , paper.strip() )
			hit = { "key": it[ "key" ] , "in_library": False }
		except OpError:
			pass
		if hit is None:
			hit = ctx.resolve( paper.strip() )
	else:
		raise OpError( "`paper` must be a key / DOI / WID / title , or a paper object" )

	tags = _str_list( p.get( "tags" ) , "tags" )
	# Already here ? By key , or by the DOI the hit carries.
	try:
		where , i , it = locate( doc , hit[ "key" ] )
	except OpError:
		where = it = None
		d = lc( utils.normalize_doi( hit.get( "doi" ) or "" ) or "" )
		if d:
			try:
				where , i , it = locate( doc , d )
			except OpError:
				pass
	if where == "items":
		if p.get( "if_absent" ):
			return { **_brief( where , it , doc ) , "skipped": "already on the list" }
		raise OpError( f"{it['key']!r} is already on the list at #{i + 1} -- "
			"use move to put it somewhere else , or if_absent to skip it" )
	if where == "staging":
		_staged( doc ).pop( i )
		row , from_shelf = it , True
	else:
		row , from_shelf = new_item( doc , hit ) , False
		if hit.get( "pending" ):
			row[ "pending" ] = True

	for t in tags:
		if not has_tag( row , t ):
			row.setdefault( "tags" , [] ).append( t )
	_set_cells( doc , row , p.get( "cells" ) )
	want_tags = p.get( "auto_tag" , True ) and not row.get( "tags" )
	want_fill = p.get( "prefill" , True )
	m = {}
	if want_tags or want_fill:
		titles = {} if re.match( r"^10\." , row[ "key" ] ) else { row[ "key" ]: row.get( "title" ) or "" }
		m = ctx.meta( [ row[ "key" ] ] , True , True , titles ).get( row[ "key" ] ) or {}
		if want_tags and m.get( "mods" ):
			row[ "tags" ] = list( m[ "mods" ] )
		if want_fill:
			prefill( doc , row , m )
	sort_tags( row )
	for t in row.get( "tags" ) or []:
		remember_tag( doc , t )
	_items( doc ).insert( insert_index( doc , p.get( "at" ) ) , row )
	out = { **_brief( "items" , row , doc ) , "title": row.get( "title" ) ,
		"tags": row.get( "tags" ) , "in_library": bool( hit.get( "in_library" ) or m.get( "in_library" ) ) }
	if from_shelf:
		out[ "from_shelf" ] = True
	if row.get( "pending" ):
		out[ "pending" ] = True
	return out


def op_remove( doc , ctx , p ):
	"""Take rows off the board ( the row's 🗑 , or Drop on the shelf ). Works on
	list and shelf rows alike."""
	gone = []
	for sel in _rows_arg( p ):
		where , i , it = locate( doc , sel )
		( _items( doc ) if where == "items" else _staged( doc ) ).pop( i )
		gone.append( { "key": it[ "key" ] , "from": "list" if where == "items" else "shelf" } )
	return { "removed": gone }


def op_move( doc , ctx , p ):
	"""Send one row somewhere else in YOUR order ( dragging it , or typing a
	number into its box ). Marks it placed by hand , which is what the "placed"
	add position keys off."""
	if "to" not in p:
		raise OpError( "`to` is required" )
	i , it = _on_list( doc , p.get( "row" ) )
	to = pin( doc , p[ "to" ] , moving=( it[ "key" ] , ) )
	_items( doc ).pop( i )
	_items( doc ).insert( insert_index( doc , to ) , it )
	it[ "placed" ] = True
	return _brief( "items" , it , doc )


def op_reorder( doc , ctx , p ):
	"""Take several rows out and put them back as ONE block , in the order given
	-- at `at` , or where the topmost of them was. Each is marked placed."""
	sels  = _rows_arg( p )
	picks = []
	for sel in sels:
		i , it = _on_list( doc , sel )
		if any( x is it for _ , x in picks ):
			raise OpError( f"{sel!r} is listed twice" )
		picks.append( ( i , it ) )
	items  = _items( doc )
	top    = min( i for i , _ in picks )
	keys   = { it[ "key" ] for _ , it in picks }
	before = sum( 1 for x in items[ :top ] if x[ "key" ] not in keys )
	to     = pin( doc , p.get( "at" ) , moving=tuple( keys ) )
	doc[ "items" ] = [ x for x in items if x[ "key" ] not in keys ]
	at = before if to is None else insert_index( doc , to )
	for n , ( _ , it ) in enumerate( picks ):
		it[ "placed" ] = True
		doc[ "items" ].insert( at + n , it )
	return { "rows": [ _brief( "items" , it , doc ) for _ , it in picks ] }


SORTS = ( "year" , "year_asc" , "cites" , "title" , "added" , "tags" )


def op_sort( doc , ctx , p ):
	"""Re-stack the whole list and KEEP it -- the "Order by" lens followed by
	⇩ Keep this order. year ( newest first ) , year_asc , cites ( most cited
	first ) , title , added ( oldest first ) , tags ( papers with the same tags
	together , each group in the order you had it )."""
	by    = p.get( "by" )
	items = _items( doc )
	if by not in SORTS:
		raise OpError( f"`by` must be one of {', '.join( SORTS )}" )
	if by == "tags":
		order , groups = [] , {}
		for it in items:
			sig = tag_sig( it ) or "~"
			if sig not in groups:
				groups[ sig ] = []
				order.append( sig )
			groups[ sig ].append( it )
		order.sort( key=lambda s: s == "~" )
		doc[ "items" ] = [ it for s in order for it in groups[ s ] ]
	elif by == "cites":
		m = ctx.meta( [ it[ "key" ] for it in items ] , False , False , None )
		doc[ "items" ] = sorted( items , key=lambda it: -( ( m.get( it[ "key" ] ) or {} ).get( "cited_by" ) or 0 ) )
	elif by == "year":
		doc[ "items" ] = sorted( items , key=lambda it: -( it.get( "year" ) or 0 ) )
	elif by == "year_asc":
		doc[ "items" ] = sorted( items , key=lambda it: it.get( "year" ) or 9999 )
	elif by == "title":
		doc[ "items" ] = sorted( items , key=lambda it: lc( it.get( "title" ) or it.get( "key" ) ) )
	else:
		doc[ "items" ] = sorted( items , key=lambda it: str( it.get( "added_at" ) or "" ) )
	return { "sorted": len( doc[ "items" ] ) , "by": by }


UPDATABLE = ( "title" , "year" , "journal" , "doi" , "authors" , "pdf" )


def op_update( doc , ctx , p ):
	"""Correct a row's own details : title , year , journal , doi , authors ,
	pdf ( an open-access url , for a paper that isn't in the library )."""
	where , i , it = locate( doc , p.get( "row" ) )
	changed = [ k for k in UPDATABLE if k in p ]
	if not changed:
		raise OpError( "nothing to update -- give one of " + ", ".join( UPDATABLE ) )
	for k in changed:
		v = p[ k ]
		if k == "year":
			if v in ( None , "" ):
				v = None
			else:
				try:
					v = int( v )
				except ( TypeError , ValueError ):
					raise OpError( "`year` must be a number" )
		else:
			v = "" if v is None else str( v )
		it[ k ] = v
	return { **_brief( where , it , doc ) , "updated": changed }


CELL_MODES = ( "replace" , "append" , "prepend" , "fill" )


def op_set_cell( doc , ctx , p ):
	"""Write into one column of one or more rows ( typing into the cell ).
	mode : replace ( default ) , append / prepend ( on its own line ) , fill
	( only where the cell is empty ). An empty value with replace clears it."""
	c    = find_column( doc , p.get( "col" ) )
	val  = p.get( "value" )
	mode = p.get( "mode" ) or "replace"
	if mode not in CELL_MODES:
		raise OpError( f"`mode` must be one of {', '.join( CELL_MODES )}" )
	val = "" if val is None else str( val )
	out = []
	for sel in _row_or_rows( p ):
		where , i , it = locate( doc , sel )
		f   = it.setdefault( "fields" , {} )
		was = str( f.get( c[ "id" ] ) or "" )
		if mode == "append":
			now = ( was.rstrip( "\n" ) + "\n" + val ) if was.strip() else val
		elif mode == "prepend":
			now = ( val + "\n" + was.lstrip( "\n" ) ) if was.strip() else val
		elif mode == "fill":
			now = was if was.strip() else val
		else:
			now = val
		f[ c[ "id" ] ] = now
		out.append( { **_brief( where , it , doc ) , "col": c[ "id" ] , "changed": now != was } )
	return { "cells": out }


def op_tag( doc , ctx , p ):
	"""Put tags on / take tags off rows ( the chips , or the tag bar over a
	selection ). Case-insensitive ; a tag new to the board joins its tag list.
	With Auto-move on , a list row slides next to the rows with the same tags ,
	as on the page."""
	add    = _str_list( p.get( "add" ) , "add" )
	remove = _str_list( p.get( "remove" ) , "remove" )
	if not add and not remove:
		raise OpError( "give `add` and / or `remove`" )
	out = []
	for sel in _row_or_rows( p ):
		where , i , it = locate( doc , sel )
		before = tag_sig( it )
		tags   = [ t for t in ( it.get( "tags" ) or [] ) if lc( t ) not in { lc( r ) for r in remove } ]
		for t in add:
			if not any( lc( x ) == lc( t ) for x in tags ):
				tags.append( t )
		it[ "tags" ] = tags
		sort_tags( it )
		for t in add:
			remember_tag( doc , t )
		if where == "items" and tag_sig( it ) != before:
			auto_move( doc , it[ "key" ] )
		out.append( { **_brief( where , it , doc ) , "tags": it[ "tags" ] } )
	return { "rows": out }


HEX_RE = re.compile( r"^#[0-9a-fA-F]{6}$" )


def _color( v ):
	if v in ( None , "" ):
		return ""
	if not isinstance( v , str ) or not HEX_RE.match( v ):
		raise OpError( "`color` must be \"#rrggbb\" , or \"\" for the automatic colour" )
	return v.lower()


def _tag_name( p , name="name" ):
	v = p.get( name )
	if not isinstance( v , str ) or not v.strip():
		raise OpError( f"`{name}` must be a tag name" )
	return v.strip()[ :60 ]


def op_tag_create( doc , ctx , p ):
	"""Add a tag to the board's tag list without putting it on anything yet
	( the "+ new tag" box ). An existing tag just takes the colour , if given."""
	name = _tag_name( p )
	e    = vocab_entry( doc , name )
	if e:
		if "color" in p:
			e[ "color" ] = _color( p.get( "color" ) )
		return { "name": e[ "name" ] , "existed": True }
	_vocab( doc ).append( { "name": name , "color": _color( p.get( "color" ) ) } )
	return { "name": name , "existed": False }


def op_tag_delete( doc , ctx , p ):
	"""Delete a tag everywhere : off the tag list , and off every row on the list
	and the staging shelf ( the × on a tag-bar chip )."""
	name = _tag_name( p )
	n    = 0
	for it in _items( doc ) + _staged( doc ):
		if has_tag( it , name ):
			it[ "tags" ] = [ t for t in it[ "tags" ] if lc( t ) != lc( name ) ]
			n += 1
	doc[ "vocab" ][ "tags" ] = [ t for t in _vocab( doc ) if lc( t.get( "name" ) ) != lc( name ) ]
	return { "name": name , "rows": n }


def op_tag_rename( doc , ctx , p ):
	"""Rename a tag on every row and in the tag list. Renaming onto a tag that
	already exists merges the two."""
	old , new = _tag_name( p , "from" ) , _tag_name( p , "to" )
	n = 0
	for it in _items( doc ) + _staged( doc ):
		if has_tag( it , old ):
			tags = [ t for t in it[ "tags" ] if lc( t ) != lc( old ) ]
			if not any( lc( t ) == lc( new ) for t in tags ):
				tags.append( new )
			it[ "tags" ] = tags
			sort_tags( it )
			n += 1
	src , dst = vocab_entry( doc , old ) , vocab_entry( doc , new )
	if lc( old ) == lc( new ) and src:
		src[ "name" ] = new
	elif src and dst:
		doc[ "vocab" ][ "tags" ] = [ t for t in _vocab( doc ) if t is not src ]
	elif src:
		src[ "name" ] = new
	elif not dst:
		_vocab( doc ).append( { "name": new , "color": "" } )
	return { "from": old , "to": new , "rows": n , "merged": bool( src and dst and lc( old ) != lc( new ) ) }


def op_tag_color( doc , ctx , p ):
	"""Set the colour a tag's chips are drawn in ( 🎨 Colors ). "" puts it back
	on the automatic colour."""
	name  = _tag_name( p )
	color = _color( p.get( "color" ) )
	e     = vocab_entry( doc , name )
	if e:
		e[ "color" ] = color
	else:
		_vocab( doc ).append( { "name": name , "color": color } )
	return { "name": name , "color": color }


def op_tag_order( doc , ctx , p ):
	"""Rearrange the tag bar ( dragging chips along it ) : the tags named come
	first , in that order , and every other tag keeps its place after them."""
	names = _str_list( p.get( "names" ) , "names" )
	if not names:
		raise OpError( "`names` must list at least one tag" )
	order = tag_options( doc , ctx )
	known = { lc( v ): v for v in order }
	for n in names:
		if lc( n ) not in known:
			raise OpError( f"no tag {n!r} on this board" )
	first = []
	for n in names:
		v = known[ lc( n ) ]
		if v not in first:
			first.append( v )
	rest = [ v for v in order if v not in first ]
	doc[ "vocab" ][ "tags" ] = [ { "name": v , "color": ( vocab_entry( doc , v ) or {} ).get( "color" ) or "" }
		for v in first + rest ]
	return { "order": first + rest }


MAX_COLUMNS = 16


def op_column_add( doc , ctx , p ):
	"""Add a free-text column ( + Column ). `at` is its 1-based place among the
	columns ; the end by default."""
	label = p.get( "label" )
	if not isinstance( label , str ) or not label.strip():
		raise OpError( "`label` must be the column's name" )
	label = label.strip()[ :60 ]
	cols  = doc.setdefault( "columns" , [] )
	if len( cols ) >= MAX_COLUMNS:
		raise OpError( f"the board holds at most {MAX_COLUMNS} columns" )
	if any( lc( c[ "label" ] ) == lc( label ) for c in cols ):
		raise OpError( f"there is already a {label!r} column" )
	cid = tiers_db.slug( p.get( "id" ) or label , "col" )
	while any( c[ "id" ] == cid for c in cols ):
		cid += "-2"
	at = p.get( "at" )
	if at is not None and ( isinstance( at , bool ) or not isinstance( at , int ) ):
		raise OpError( "`at` must be a column position ( 1-based )" )
	i = len( cols ) if at is None else max( 0 , min( at - 1 , len( cols ) ) )
	cols.insert( i , { "id": cid , "label": label } )
	for it in _items( doc ) + _staged( doc ):
		it.setdefault( "fields" , {} )[ cid ] = ""
	return { "id": cid , "label": label , "pos": i + 1 }


def op_column_rename( doc , ctx , p ):
	"""Rename a column ( double-click its header ). Its id -- and every cell --
	stays as it was."""
	c     = find_column( doc , p.get( "col" ) )
	label = p.get( "label" )
	if not isinstance( label , str ) or not label.strip():
		raise OpError( "`label` must be the new name" )
	c[ "label" ] = label.strip()[ :60 ]
	return { "id": c[ "id" ] , "label": c[ "label" ] }


def op_column_delete( doc , ctx , p ):
	"""Delete a column and everything typed in it ( shift-click its header ).
	The last column can't go."""
	c    = find_column( doc , p.get( "col" ) )
	cols = doc[ "columns" ]
	if len( cols ) == 1:
		raise OpError( "keep at least one column" )
	doc[ "columns" ] = [ x for x in cols if x[ "id" ] != c[ "id" ] ]
	opts = _options( doc )
	opts[ "fold_cols" ] = [ x for x in ( opts.get( "fold_cols" ) or [] ) if x != c[ "id" ] ]
	for it in _items( doc ) + _staged( doc ):
		( it.get( "fields" ) or {} ).pop( c[ "id" ] , None )
	return { "deleted": c[ "id" ] }


def op_column_move( doc , ctx , p ):
	"""Put a column somewhere else among the columns ( `to` is 1-based )."""
	c  = find_column( doc , p.get( "col" ) )
	to = p.get( "to" )
	if isinstance( to , bool ) or not isinstance( to , int ):
		raise OpError( "`to` must be a column position ( 1-based )" )
	cols = [ x for x in doc[ "columns" ] if x is not c ]
	i    = max( 0 , min( to - 1 , len( cols ) ) )
	cols.insert( i , c )
	doc[ "columns" ] = cols
	return { "id": c[ "id" ] , "pos": i + 1 }


def op_column_fold( doc , ctx , p ):
	"""Fold a column away behind the page's ▸ Show Extra Columns button ( or
	bring it back out ). It stays in the document , the export and the filter."""
	c    = find_column( doc , p.get( "col" ) )
	opts = _options( doc )
	fold = [ x for x in ( opts.get( "fold_cols" ) or [] ) if x != c[ "id" ] ]
	if p.get( "folded" , True ):
		fold.append( c[ "id" ] )
	opts[ "fold_cols" ] = fold
	return { "id": c[ "id" ] , "folded": c[ "id" ] in fold }


ADD_WHERE = ( "bottom" , "top" , "placed" )


def op_options( doc , ctx , p ):
	"""The board's settings : auto_move ( tagging slides a row next to the
	others with the same tags ) and add_where ( where a new paper lands :
	bottom , top , or placed = after the last row you placed by hand )."""
	opts = _options( doc )
	if "auto_move" not in p and "add_where" not in p:
		raise OpError( "give `auto_move` and / or `add_where`" )
	if "auto_move" in p:
		if not isinstance( p[ "auto_move" ] , bool ):
			raise OpError( "`auto_move` must be true or false" )
		opts[ "auto_move" ] = p[ "auto_move" ]
	if "add_where" in p:
		if p[ "add_where" ] not in ADD_WHERE:
			raise OpError( f"`add_where` must be one of {', '.join( ADD_WHERE )}" )
		opts[ "add_where" ] = p[ "add_where" ]
	return { "auto_move": bool( opts.get( "auto_move" ) ) , "add_where": opts.get( "add_where" ) }


def _have_index( doc ):
	"""haveIndex : every way the board already knows a paper -- key , DOI ,
	title -- and whether that's on the list or the shelf."""
	seen = {}

	def add( it , where ):
		if it.get( "key" ):
			seen[ lc( it[ "key" ] ) ] = where
		if it.get( "doi" ):
			seen[ "doi:" + lc( it[ "doi" ] ) ] = where
		t = utils.normalize_title( it.get( "title" ) or "" )
		if t:
			seen[ "t:" + t ] = where
	for it in _items( doc ):
		add( it , "list" )
	for it in _staged( doc ):
		add( it , "shelf" )
	return seen


def op_stage( doc , ctx , p ):
	"""Read a reference list ( pasted text -- AMA , APA , IEEE , numbered or not )
	onto the staging shelf , exactly as 📋 Import refs does. Each reference is
	matched to your library by DOI , then title ; anything already on the board
	is counted and skipped. Library papers arrive tagged and with Code / Datasets
	filled in."""
	text = p.get( "text" )
	if not isinstance( text , str ) or not text.strip():
		raise OpError( "`text` must be the reference list to read" )
	refs  = ctx.parse_refs( text ) or []
	seen  = _have_index( doc )
	fresh , matched = [] , {}
	on_list = on_shelf = 0
	cols  = doc.get( "columns" ) or []
	for r in refs:
		where = ( seen.get( lc( r.get( "key" ) ) ) or
			( r.get( "doi" ) and seen.get( "doi:" + lc( r[ "doi" ] ) ) ) or
			seen.get( "t:" + utils.normalize_title( r.get( "title" ) or "" ) ) or "" )
		if where == "list":
			on_list += 1
			continue
		if where == "shelf":
			on_shelf += 1
			continue
		it = {
			"key": r.get( "key" ) , "title": r.get( "title" ) or "" , "authors": r.get( "authors" ) or "" ,
			"doi": r.get( "doi" ) or "" , "wid": r.get( "wid" ) or "" , "pdf": r.get( "pdf" ) or "" ,
			"year": r.get( "year" ) , "journal": r.get( "journal" ) or "" , "tags": [] ,
			"fields": { c[ "id" ]: "" for c in cols } , "added_at": now_iso() ,
		}
		if not it[ "key" ]:
			continue
		matched[ it[ "key" ] ] = r.get( "matched" ) or ""
		seen[ lc( it[ "key" ] ) ] = "shelf"
		t = utils.normalize_title( it[ "title" ] )
		if t:
			seen[ "t:" + t ] = "shelf"
		fresh.append( it )
	if fresh:
		titles = { it[ "key" ]: it[ "title" ] for it in fresh
			if it[ "title" ] and not re.match( r"^10\." , it[ "key" ] ) }
		meta = ctx.meta( [ it[ "key" ] for it in fresh ] , True , True , titles )
		for it in fresh:
			m = meta.get( it[ "key" ] ) or {}
			if not it[ "tags" ] and m.get( "mods" ):
				it[ "tags" ] = list( m[ "mods" ] )
				sort_tags( it )
				for t in it[ "tags" ]:
					remember_tag( doc , t )
			prefill( doc , it , m )
		_staged( doc ).extend( fresh )
	return { "read": len( refs ) , "staged": len( fresh ) , "on_list": on_list ,
		"on_shelf": on_shelf , "keys": [ it[ "key" ] for it in fresh ] , "matched": matched }


def _shelf_targets( doc , p ):
	if p.get( "all" ):
		if "rows" in p:
			raise OpError( "give `rows` or `all` , not both" )
		return [ it[ "key" ] for it in _staged( doc ) ]
	return _rows_arg( p )


def op_stage_add( doc , ctx , p ):
	"""Move staged rows onto the list ( Add on the shelf ) -- `rows` , or
	all: true for the whole shelf -- in shelf order , at `at` or where the
	board's "add to…" setting says."""
	moving , skipped = [] , []
	for sel in _shelf_targets( doc , p ):
		where , i , it = locate( doc , sel )
		if where == "items":
			skipped.append( it[ "key" ] )
			continue
		if any( x is it for x in moving ):
			continue
		moving.append( it )
	if not moving:
		return { "added": [] , "skipped": skipped }
	ids = { id( x ) for x in moving }
	doc[ "staging" ] = [ x for x in _staged( doc ) if id( x ) not in ids ]
	at = insert_index( doc , pin( doc , p.get( "at" ) ) )
	for n , it in enumerate( moving ):
		for t in it.get( "tags" ) or []:
			remember_tag( doc , t )
		_items( doc ).insert( at + n , it )
	return { "added": [ _brief( "items" , it , doc ) for it in moving ] , "skipped": skipped }


def op_stage_drop( doc , ctx , p ):
	"""Throw staged rows away ( Drop on the shelf ) -- `rows` , or all: true."""
	gone = []
	for sel in _shelf_targets( doc , p ):
		where , i , it = locate( doc , sel )
		if where != "staging":
			raise OpError( f"{sel!r} is on the list , not the shelf -- use remove" )
		_staged( doc ).pop( i )
		gone.append( it[ "key" ] )
	return { "dropped": gone }


def _list_targets( doc , p , default ):
	if "rows" not in p:
		return [ it for it in _items( doc ) if default( it ) ]
	out = []
	for sel in _rows_arg( p ):
		where , i , it = locate( doc , sel )
		out.append( it )
	return out


def op_auto_tag( doc , ctx , p ):
	"""Tag papers from their ` prma modalities ` stamp ( ⚡ Auto-tag ). Only rows
	carrying no tags at all are touched -- every untagged list row , or the
	`rows` given."""
	todo = [ it for it in _list_targets( doc , p , lambda it: True ) if not it.get( "tags" ) ]
	meta = ctx.meta( [ it[ "key" ] for it in todo ] , True , False , None ) if todo else {}
	done = []
	for it in todo:
		m = meta.get( it[ "key" ] ) or {}
		if m.get( "mods" ):
			it[ "tags" ] = list( m[ "mods" ] )
			sort_tags( it )
			for t in it[ "tags" ]:
				remember_tag( doc , t )
			done.append( it[ "key" ] )
	return { "tagged": done }


def op_refill( doc , ctx , p ):
	"""Fill in what prma has found since a row was added : its modality tags if
	it has none , its Code / Datasets cells where they're empty. Nothing typed is
	overwritten. Default : the rows still marked ⏳ processing ; or the `rows`
	given. A processing row stops being one once its paper has been through the
	pipeline."""
	todo = _list_targets( doc , p , lambda it: it.get( "pending" ) )
	meta = ctx.meta( [ it[ "key" ] for it in todo ] , True , True , None ) if todo else {}
	out  = []
	for it in todo:
		m = meta.get( it[ "key" ] ) or {}
		if not m.get( "in_library" ):
			continue
		got = {}
		if not it.get( "tags" ) and m.get( "mods" ):
			it[ "tags" ] = list( m[ "mods" ] )
			sort_tags( it )
			for t in it[ "tags" ]:
				remember_tag( doc , t )
			got[ "tags" ] = it[ "tags" ]
		wrote = prefill( doc , it , m )
		if wrote:
			got[ "cells" ] = wrote
		if it.get( "pending" ) and ( m.get( "has_md" ) or m.get( "mods" ) or m.get( "code" ) ):
			it[ "pending" ] = False
			got[ "processed" ] = True
		if got:
			out.append( { "key": it[ "key" ] , **got } )
	return { "filled": out }


def op_pull_tiers( doc , ctx , p ):
	"""Bring every paper on the tier list onto this board ( ⇩ From tiers ) : its
	modalities and tags merged into one tag list , its tier in a Tier column , its
	notes columns merged by name. Papers already here are left alone."""
	tdoc = ctx.tiers_doc() or {}
	rows = [ ( it , t ) for t in ( tdoc.get( "tiers" ) or [] ) for it in ( t.get( "items" ) or [] ) ]
	if not rows:
		raise OpError( "the tier list is empty" )
	cols = doc.setdefault( "columns" , [] )
	for c in tdoc.get( "columns" ) or []:
		if not any( x[ "id" ] == c[ "id" ] for x in cols ):
			cols.append( { "id": c[ "id" ] , "label": c[ "label" ] } )
	if not any( c[ "id" ] == "tier" for c in cols ):
		cols.append( { "id": "tier" , "label": "Tier" } )
	have , added = { it[ "key" ] for it in _items( doc ) } , []
	for it , tier in rows:
		if it.get( "key" ) in have:
			continue
		tags = sorted( { lc( t ): t for t in ( it.get( "mods" ) or [] ) + ( it.get( "tags" ) or [] ) }.values() , key=lc )
		for t in tags:
			remember_tag( doc , t )
		fields = dict( it.get( "fields" ) or {} , tier=tier.get( "label" ) or "" )
		for c in cols:
			fields.setdefault( c[ "id" ] , "" )
		_items( doc ).append( { "key": it[ "key" ] , "title": it.get( "title" ) or "" ,
			"doi": it.get( "doi" ) or "" , "wid": it.get( "wid" ) or "" , "pdf": it.get( "pdf" ) or "" ,
			"year": it.get( "year" ) , "journal": it.get( "journal" ) or "" , "tags": tags ,
			"fields": fields , "added_at": it.get( "added_at" ) or now_iso() } )
		have.add( it[ "key" ] )
		added.append( it[ "key" ] )
	return { "pulled": added }


# ---------------------------------------------------------------------------
# Importing from the other lists ( ⇩ Import from lists… on the page )
# ---------------------------------------------------------------------------
#
# Both ops pick rows the way the page's import dialog does -- every filter given
# must hold -- and skip any paper this board already has , on the list or on the
# shelf , by key or by DOI. Positions ( `range` ) are the SOURCE's : #N on the
# list it comes from , or the Nth of Remaining Papers newest-first.

def parse_range( spec ):
	"""'1-40, 55, 60-70' -> the 1-based positions it names. OpError on anything
	that isn't a number or a run of them."""
	out = set()
	for part in re.split( r"[,\s]+" , str( spec or "" ).strip() ):
		if not part:
			continue
		m = re.match( r"^#?(\d+)(?:[-–](\d+))?$" , part )
		if not m:
			raise OpError( f"`range` : {part!r} isn't N or N-M" )
		a = int( m.group( 1 ) )
		b = int( m.group( 2 ) ) if m.group( 2 ) else a
		if b < a:
			a , b = b , a
		if b - a > 100000:
			raise OpError( f"`range` : {part!r} is too long a run" )
		out.update( range( a , b + 1 ) )
	return out


def _year( v ):
	try:
		return int( v ) if str( v or "" ).strip() else None
	except ( TypeError , ValueError ):
		return None


def pick_rows( rows , p ):
	"""The import dialog's filters over `rows` ( [ ( pos , row ) ] , row having
	key / title / doi / year / tags / fields ) -> the ( pos , row ) that pass.

	  keys       only these papers
	  range      only these source positions ( "1-40, 55" )
	  tags       carrying any of them ( match="all" : every one ,
	             match="none" : not one of them )
	  year_from / year_to   published in that span ; a row with no year is in
	             only when neither is given , or with no_year=true
	  q          anything typed in the row , as the page's filter box reads it"""
	keys  = set( _str_list( p.get( "keys" ) , "keys" ) ) if p.get( "keys" ) is not None else None
	span  = parse_range( p.get( "range" ) ) if str( p.get( "range" ) or "" ).strip() else None
	want  = [ lc( t ) for t in _str_list( p.get( "tags" ) , "tags" ) ]
	match = p.get( "match" ) or "any"
	if match not in MATCHES:
		raise OpError( "`match` is \"any\" , \"all\" or \"none\"" )
	y0 , y1 = _year( p.get( "year_from" ) ) , _year( p.get( "year_to" ) )
	no_year = p.get( "no_year" ) if p.get( "no_year" ) is not None else ( y0 is None and y1 is None )
	q = str( p.get( "q" ) or "" ).strip()
	out = []
	for pos , it in rows:
		if keys is not None and it.get( "key" ) not in keys:
			continue
		if span is not None and pos not in span:
			continue
		if not tags_match( it , want , match ):
			continue
		y = _year( it.get( "year" ) )
		if y is None:
			if not no_year:
				continue
		elif ( y0 is not None and y < y0 ) or ( y1 is not None and y > y1 ):
			continue
		if q:
			hay = " ".join( [ it.get( "title" ) or "" , it.get( "key" ) or "" , it.get( "doi" ) or "" ,
				" ".join( it.get( "tags" ) or [] ) ,
				" ".join( str( v ) for v in ( it.get( "fields" ) or {} ).values() ) ] )
			if not text_has( hay , q ):
				continue
		out.append( ( pos , it ) )
	return out


def _here( doc ):
	"""Every key and DOI this board holds , list and shelf , lowercased."""
	out = set()
	for it in _items( doc ) + _staged( doc ):
		out.add( lc( it.get( "key" ) ) )
		d = utils.normalize_doi( it.get( "doi" ) or "" )
		if d:
			out.add( lc( d ) )
	return out


def _is_here( here , it ):
	d = utils.normalize_doi( it.get( "doi" ) or "" )
	return lc( it.get( "key" ) ) in here or bool( d and lc( d ) in here )


def _land( doc , rows , p ):
	"""Put imported rows on the board : on the shelf with shelf=true , else into
	the list at `at` ( the board's "add to…" by default ) , in the order given."""
	if p.get( "shelf" ):
		_staged( doc ).extend( rows )
		return "shelf"
	at = insert_index( doc , p.get( "at" ) )
	_items( doc )[ at:at ] = rows
	return "list"


def op_import_list( doc , ctx , p ):
	"""Copy papers from another sort list onto this one ( ⇩ Import from lists… ,
	or ⇢ Copy to list… on the list they are on ). `from` is that list's slug ,
	"" or "main" for the main board ; the filters pick which of its rows come
	( see pick_rows ) , all of them when none is given. Each arrives with its
	tags in that list's colours and , unless fields=false , its notes -- the
	columns it has that this board hasn't are added. Papers already here , on the
	list or the shelf , are skipped and counted."""
	src = p.get( "from" )
	sdoc = ctx.list_doc( src )
	scol = { c[ "id" ]: c for c in sdoc.get( "columns" ) or [] }
	rows = pick_rows( list( enumerate( sdoc.get( "items" ) or [] , 1 ) ) , p )
	here = _here( doc )
	keep = p.get( "fields" , True )
	cols = doc.setdefault( "columns" , [] )
	got , skipped , used = [] , [] , set()
	for _ , it in rows:
		if _is_here( here , it ):
			skipped.append( it[ "key" ] )
			continue
		fields = {}
		if keep:
			for cid , v in ( it.get( "fields" ) or {} ).items():
				if v and cid in scol:
					fields[ cid ] = v
					used.add( cid )
		row = { "key": it[ "key" ] , "title": it.get( "title" ) or "" , "authors": it.get( "authors" ) or "" ,
			"doi": it.get( "doi" ) or "" , "wid": it.get( "wid" ) or "" , "pdf": it.get( "pdf" ) or "" ,
			"year": it.get( "year" ) , "journal": it.get( "journal" ) or "" ,
			"tags": list( it.get( "tags" ) or [] ) , "fields": fields , "added_at": now_iso() }
		if it.get( "pending" ):
			row[ "pending" ] = True
		for t in row[ "tags" ]:
			if not vocab_entry( doc , t ):
				v = next( ( x for x in ( sdoc.get( "vocab" ) or {} ).get( "tags" ) or [] if lc( x.get( "name" ) ) == lc( t ) ) , None )
				_vocab( doc ).append( { "name": t , "color": ( v or {} ).get( "color" ) or "" } )
		here.add( lc( row[ "key" ] ) )
		got.append( row )
	# The columns that carried something , in that list's order -- folded here
	# when they were folded there ( a sheet's paragraphs stay out of the way ).
	sfold = set( ( sdoc.get( "options" ) or {} ).get( "fold_cols" ) or [] )
	for cid , c in scol.items():
		if cid in used and not any( x[ "id" ] == cid for x in cols ):
			cols.append( { "id": cid , "label": c[ "label" ] } )
			if cid in sfold:
				fold = _options( doc ).setdefault( "fold_cols" , [] )
				if cid not in fold:
					fold.append( cid )
	for row in got:
		for c in cols:
			row[ "fields" ].setdefault( c[ "id" ] , "" )
	where = _land( doc , got , p )
	return { "imported": [ r[ "key" ] for r in got ] , "skipped": skipped , "to": where ,
		"from": src or "main" }


def op_import_remaining( doc , ctx , p ):
	"""Bring library papers that aren't on this board yet onto it -- Remaining
	Papers in ⇩ Import from lists… . Their tags are their modality stamps , so
	`tags` filters on those ; `range` counts them newest-added first. Each
	arrives the way a search hit does ( add ) : tagged from its stamp , with its
	Code / Datasets cells filled."""
	pool = ctx.remaining( doc )
	rows = pick_rows( [ ( i , dict( r , tags=r.get( "mods" ) or [] ) ) for i , r in enumerate( pool , 1 ) ] , p )
	here = _here( doc )
	got  = []
	for _ , hit in rows:
		if _is_here( here , hit ):
			continue
		row = new_item( doc , hit )
		row[ "tags" ] = list( hit.get( "mods" ) or [] )
		sort_tags( row )
		here.add( lc( row[ "key" ] ) )
		got.append( row )
	if got:
		meta = ctx.meta( [ r[ "key" ] for r in got ] , True , True , {} ) or {}
		for row in got:
			m = meta.get( row[ "key" ] ) or {}
			if not row[ "tags" ] and m.get( "mods" ):
				row[ "tags" ] = list( m[ "mods" ] )
				sort_tags( row )
			prefill( doc , row , m )
			for t in row[ "tags" ]:
				remember_tag( doc , t )
	where = _land( doc , got , p )
	return { "imported": [ r[ "key" ] for r in got ] , "to": where , "remaining": len( pool ) - len( got ) }


# ---------------------------------------------------------------------------
# The registry : what runs , and what GET /api/sort/schema says runs
# ---------------------------------------------------------------------------
#
# Each param : ( type , required , description ). Types : "row" ( a selector ) ,
# "rows" ( one or a list ) , "pos" ( a list position ) , "str" , "text" ( a string
# or a number ) , "int" , "bool" ,
# "strs" ( one string or a list ) , "obj" , "paper" ( string or object ).

ROW  = "a row selector : key , DOI or \"#N\""
ROWS = "one row selector or a list of them"
POS  = "N , \"top\" , \"bottom\" , \"placed\" , {\"after\": row} or {\"before\": row}"

# What the two import ops pick rows with ( pick_rows ) and where they land them.
IMPORT_PICK = {
	"keys":      ( "strs" , False , "only these papers ( keys )" ) ,
	"range":     ( "str" , False , "only these source positions : \"1-40, 55, 60-70\"" ) ,
	"tags":      ( "strs" , False , "only papers carrying one of these ( match=all : every one , "
	                                "match=none : not one of them )" ) ,
	"match":     ( "str" , False , "any | all | none ( default any )" ) ,
	"year_from": ( "int" , False , "published in or after" ) ,
	"year_to":   ( "int" , False , "published in or before" ) ,
	"no_year":   ( "bool" , False , "let in papers with no year ( default : only when no year bound is given )" ) ,
	"q":         ( "str" , False , "text anywhere in the row" ) ,
	"at":        ( "pos" , False , POS + " ; default : the board's add_where" ) ,
	"shelf":     ( "bool" , False , "land them on the staging shelf instead of the list" ) ,
}

OPS = {
	"add": ( op_add , {
		"paper":     ( "paper" , True ,  "key , DOI , OpenAlex WID or title -- or { title , doi , year , journal , authors , wid , pdf , key }" ) ,
		"at":        ( "pos" , False ,   POS + " ; default : the board's add_where" ) ,
		"tags":      ( "strs" , False ,  "tags to put on it ( instead of its modality stamp )" ) ,
		"cells":     ( "obj" , False ,   "{ column id or label: text }" ) ,
		"auto_tag":  ( "bool" , False ,  "tag it from its modality stamp when no tags are given ( default true )" ) ,
		"prefill":   ( "bool" , False ,  "fill its empty Code / Datasets cells ( default true )" ) ,
		"if_absent": ( "bool" , False ,  "already on the list -> skip instead of failing" ) ,
	} ) ,
	"remove":  ( op_remove , { "rows": ( "rows" , True , ROWS ) } ) ,
	"move":    ( op_move , { "row": ( "row" , True , ROW ) , "to": ( "pos" , True , POS ) } ) ,
	"reorder": ( op_reorder , {
		"rows": ( "rows" , True , "the rows , in the order they should end up" ) ,
		"at":   ( "pos" , False , POS + " ; default : where the topmost of them was" ) ,
	} ) ,
	"sort":    ( op_sort , { "by": ( "str" , True , " | ".join( SORTS ) ) } ) ,
	"update":  ( op_update , dict(
		{ "row": ( "row" , True , ROW ) } ,
		**{ k: ( "text" , False , f"the new {k}" ) for k in UPDATABLE } ) ) ,
	"set_cell": ( op_set_cell , {
		"row":   ( "row" , False , ROW + " ( or use rows )" ) ,
		"rows":  ( "rows" , False , ROWS ) ,
		"col":   ( "str" , True , "column id or label" ) ,
		"value": ( "text" , True , "the text" ) ,
		"mode":  ( "str" , False , " | ".join( CELL_MODES ) + " ( default replace )" ) ,
	} ) ,
	"tag": ( op_tag , {
		"row":    ( "row" , False , ROW + " ( or use rows )" ) ,
		"rows":   ( "rows" , False , ROWS ) ,
		"add":    ( "strs" , False , "tags to put on" ) ,
		"remove": ( "strs" , False , "tags to take off" ) ,
	} ) ,
	"tag_create": ( op_tag_create , { "name": ( "str" , True , "the tag" ) ,
		"color": ( "str" , False , "\"#rrggbb\" , or \"\" for automatic" ) } ) ,
	"tag_delete": ( op_tag_delete , { "name": ( "str" , True , "the tag" ) } ) ,
	"tag_rename": ( op_tag_rename , { "from": ( "str" , True , "the tag now" ) ,
		"to": ( "str" , True , "its new name ( an existing tag merges )" ) } ) ,
	"tag_color":  ( op_tag_color , { "name": ( "str" , True , "the tag" ) ,
		"color": ( "str" , True , "\"#rrggbb\" , or \"\" for automatic" ) } ) ,
	"tag_order":  ( op_tag_order , { "names": ( "strs" , True , "tags to put first on the tag bar , in order" ) } ) ,
	"column_add": ( op_column_add , { "label": ( "str" , True , "the column's name" ) ,
		"id": ( "str" , False , "its id ( default : slug of the label )" ) ,
		"at": ( "int" , False , "1-based place among the columns ( default : last )" ) } ) ,
	"column_rename": ( op_column_rename , { "col": ( "str" , True , "column id or label" ) ,
		"label": ( "str" , True , "the new name" ) } ) ,
	"column_delete": ( op_column_delete , { "col": ( "str" , True , "column id or label" ) } ) ,
	"column_move":   ( op_column_move , { "col": ( "str" , True , "column id or label" ) ,
		"to": ( "int" , True , "1-based place among the columns" ) } ) ,
	"column_fold":   ( op_column_fold , { "col": ( "str" , True , "column id or label" ) ,
		"folded": ( "bool" , False , "true folds it away ( default ) , false brings it back" ) } ) ,
	"options": ( op_options , { "auto_move": ( "bool" , False , "tagging slides a row to its tag group" ) ,
		"add_where": ( "str" , False , " | ".join( ADD_WHERE ) ) } ) ,
	"stage":      ( op_stage , { "text": ( "str" , True , "a pasted reference list" ) } ) ,
	"stage_add":  ( op_stage_add , { "rows": ( "rows" , False , ROWS ) ,
		"all": ( "bool" , False , "the whole shelf" ) , "at": ( "pos" , False , POS ) } ) ,
	"stage_drop": ( op_stage_drop , { "rows": ( "rows" , False , ROWS ) ,
		"all": ( "bool" , False , "the whole shelf" ) } ) ,
	"auto_tag":   ( op_auto_tag , { "rows": ( "rows" , False , ROWS + " ( default : every untagged list row )" ) } ) ,
	"refill":     ( op_refill , { "rows": ( "rows" , False , ROWS + " ( default : rows still processing )" ) } ) ,
	"pull_tiers": ( op_pull_tiers , {} ) ,
	"import_list": ( op_import_list , dict( {
		"from":   ( "str" , True , "the list's slug -- \"\" or \"main\" for the main board ( GET /api/sort/lists )" ) ,
		"fields": ( "bool" , False , "bring each row's notes / cells , adding columns this board lacks ( default true )" ) ,
	} , **IMPORT_PICK ) ) ,
	"import_remaining": ( op_import_remaining , IMPORT_PICK ) ,
}


def _check_type( name , kind , v ):
	ok = {
		"row":   lambda: isinstance( v , str ) ,
		"rows":  lambda: isinstance( v , str ) or ( isinstance( v , list ) and all( isinstance( x , str ) for x in v ) ) ,
		"strs":  lambda: isinstance( v , str ) or ( isinstance( v , list ) and all( isinstance( x , str ) for x in v ) ) ,
		"str":   lambda: isinstance( v , str ) ,
		"text":  lambda: v is None or ( isinstance( v , ( str , int , float ) ) and not isinstance( v , bool ) ) ,
		"int":   lambda: v is None or ( isinstance( v , int ) and not isinstance( v , bool ) ) ,
		"bool":  lambda: isinstance( v , bool ) ,
		"obj":   lambda: isinstance( v , dict ) ,
		"paper": lambda: isinstance( v , ( str , dict ) ) ,
		"pos":   lambda: v is None or isinstance( v , ( int , str , dict ) ) and not isinstance( v , bool ) ,
	}[ kind ]()
	if not ok:
		raise OpError( f"`{name}` has the wrong type ( expected {kind} )" )


def apply_op( doc , ctx , op ):
	"""Run one op against `doc` ( in place ). -> its result. OpError on bad
	input -- the caller throws the copy away."""
	if not isinstance( op , dict ):
		raise OpError( "each op must be an object with an \"op\" field" )
	name = op.get( "op" )
	if name not in OPS:
		raise OpError( f"unknown op {name!r} -- see GET /api/sort/schema" )
	fn , spec = OPS[ name ]
	p = { k: v for k , v in op.items() if k != "op" }
	unknown = [ k for k in p if k not in spec ]
	if unknown:
		raise OpError( f"{name} doesn't take {', '.join( map( repr , unknown ) )} -- it takes "
			+ ( ", ".join( spec ) or "nothing" ) )
	for k , ( kind , req , _ ) in spec.items():
		if req and k not in p:
			raise OpError( f"{name} needs `{k}`" )
		if k in p:
			_check_type( k , kind , p[ k ] )
	return fn( doc , ctx , p )


MAX_OPS = 500


def apply_ops( doc , ctx , ops ):
	"""Run a batch in order against `doc` ( in place ). -> the per-op results.
	OpFailure names the first op that failed ; the caller discards `doc`."""
	if not isinstance( ops , list ) or not ops:
		raise OpFailure( 0 , None , "`ops` must be a non-empty list" )
	if len( ops ) > MAX_OPS:
		raise OpFailure( 0 , None , f"at most {MAX_OPS} ops per batch" )
	results = []
	for i , op in enumerate( ops ):
		try:
			res = apply_op( doc , ctx , op )
		except OpError as e:
			raise OpFailure( i , op , str( e ) )
		results.append( { "op": op.get( "op" ) , **( res or {} ) } )
	return results


# ---------------------------------------------------------------------------
# Reading the board the way an agent wants it
# ---------------------------------------------------------------------------

def _cell_keys( doc ):
	"""Column id -> the key its cells go under in rows_view : the label , or the
	id where two columns share a label."""
	labels = [ c[ "label" ] for c in doc.get( "columns" ) or [] ]
	return { c[ "id" ]: ( c[ "label" ] if labels.count( c[ "label" ] ) == 1 else c[ "id" ] )
		for c in doc.get( "columns" ) or [] }


def rows_view( doc , meta=None , q="" , tags=None , match="any" , shelf=False ,
		limit=500 , offset=0 ):
	"""GET /api/sort/rows : the list ( or the shelf ) as flat rows , each with its
	position in YOUR order and its cells under their column labels. `q` filters
	like the page's filter box ; `tags` like the tag bar's 🔎 Filter."""
	meta   = meta or {}
	ckeys  = _cell_keys( doc )
	src    = _staged( doc ) if shelf else _items( doc )
	want   = [ lc( t ) for t in ( tags or [] ) if t ]
	rows   = []
	for i , it in enumerate( src ):
		if not tags_match( it , want , match ):
			continue
		if q:
			hay = " ".join( [ it.get( "title" ) or "" , it.get( "key" ) or "" , it.get( "doi" ) or "" ,
				" ".join( it.get( "tags" ) or [] ) ,
				" ".join( str( v ) for v in ( it.get( "fields" ) or {} ).values() ) ] )
			if not text_has( hay , q ):
				continue
		m = meta.get( it[ "key" ] ) or {}
		row = {
			"pos":     None if shelf else i + 1 ,
			"key":     it[ "key" ] ,
			"title":   it.get( "title" ) or m.get( "title" ) or "" ,
			"year":    it.get( "year" ) or m.get( "year" ) ,
			"journal": it.get( "journal" ) or "" ,
			"doi":     it.get( "doi" ) or m.get( "doi" ) or "" ,
			"tags":    list( it.get( "tags" ) or [] ) ,
			"cells":   { ckeys[ c ]: v for c , v in ( it.get( "fields" ) or {} ).items() if c in ckeys } ,
			"placed":  bool( it.get( "placed" ) ) ,
			"pending": bool( it.get( "pending" ) ) ,
		}
		if meta:
			row[ "in_library" ] = bool( m.get( "in_library" ) )
		if it.get( "authors" ):
			row[ "authors" ] = it[ "authors" ]
		rows.append( row )
	counts = {}
	for it in _items( doc ):
		for t in it.get( "tags" ) or []:
			counts[ lc( t ) ] = counts.get( lc( t ) , 0 ) + 1
	return {
		"total":   len( rows ) ,
		"offset":  offset ,
		"rows":    rows[ offset : offset + max( 0 , limit ) ] ,
		"columns": [ { "id": c[ "id" ] , "label": c[ "label" ] ,
			"folded": c[ "id" ] in ( _options( doc ).get( "fold_cols" ) or [] ) }
			for c in doc.get( "columns" ) or [] ] ,
		"tags":    [ { "name": t[ "name" ] , "color": t.get( "color" ) or "" ,
			"count": counts.get( lc( t[ "name" ] ) , 0 ) } for t in _vocab( doc ) ] ,
		"options": { "auto_move": bool( _options( doc ).get( "auto_move" ) ) ,
			"add_where": _options( doc ).get( "add_where" ) or "bottom" } ,
		"list":    len( _items( doc ) ) ,
		"shelf":   len( _staged( doc ) ) ,
	}


def _csv_cell( v ):
	"""csvCell ( /static/common.js ) : one line per record."""
	s = "; ".join( v ) if isinstance( v , list ) else ( "" if v is None else str( v ) )
	return "; ".join( t.strip() for t in s.replace( "\r\n" , "\n" ).replace( "\r" , "\n" ).split( "\n" ) if t.strip() )


def to_csv( doc , meta=None ):
	"""toCSV : # , DOI_or_Key , Title , Year , Tags , <columns> , MD_File -- in
	YOUR order. With the BOM saveCSV puts on , so Excel reads it as UTF-8."""
	meta = meta or {}
	buf  = io.StringIO()
	w    = csv.writer( buf , lineterminator="\n" )
	cols = doc.get( "columns" ) or []
	w.writerow( [ "#" , "DOI_or_Key" , "Title" , "Year" , "Tags" ] + [ c[ "label" ] for c in cols ] + [ "MD_File" ] )
	for i , it in enumerate( _items( doc ) ):
		m  = meta.get( it[ "key" ] ) or {}
		lk = ( m.get( "in_library" ) and m.get( "key" ) ) or it[ "key" ]
		w.writerow( [ _csv_cell( x ) for x in
			[ i + 1 , it[ "key" ] , it.get( "title" ) or m.get( "title" ) or "" ,
			  it.get( "year" ) or m.get( "year" ) or "" , it.get( "tags" ) or [] ]
			+ [ ( it.get( "fields" ) or {} ).get( c[ "id" ] ) or "" for c in cols ]
			+ [ re.sub( r"[\/\\]" , "_" , lk ) + ".md" if m.get( "has_md" ) else "" ] ] )
	return "\ufeff" + buf.getvalue().rstrip( "\n" )


def schema():
	"""GET /api/sort/schema."""
	return {
		"about": "Edit the /sort board one named operation at a time. POST /api/sort/ops runs a "
			"batch atomically : every op succeeds and the board is saved once , or nothing is saved. "
			"The other lists ( /sort/<slug> ) take every route below under /api/sort/lists/<slug> "
			"instead of /api/sort -- the same ops , rows , history and restore.",
		"auth": "Authorization: Bearer <API key> ( mint one on /account ). Reads need none.",
		"endpoints": {
			"GET /api/sort/schema":     "this document" ,
			"GET /api/sort/rows":       "the list as flat rows. ?q= text filter , ?tags=a,b&match=any|all|none , "
			                            "?shelf=1 for the staging shelf , ?limit= ( default 500 ) &offset= , "
			                            "?meta=1 adds in_library and fills blank titles from the library" ,
			"GET /api/sort":            "the whole board document , plus rev and locked" ,
			"GET /api/sort/version":    "{ rev , locked , by } -- cheap change check" ,
			"POST /api/sort/ops":       "{ ops: [ { op , ... } ] , if_rev?: rev , dry_run?: bool }" ,
			"GET /api/sort/export.csv": "the list as CSV , in your order" ,
			"GET /api/sort/history":    "saved versions , newest first ( login needed )" ,
			"POST /api/sort/restore":   "{ id } -- put a saved version back ( itself saved first , so it undoes too )" ,
			"GET /api/sort/lists":      "every list : { slug , name , url , api , list , shelf , ... } , the main board first" ,
			"POST /api/sort/lists":     "{ name , from?: slug , keys?: [ ... ] } -- a new list : empty , a copy of "
			                            "list `from` ( \"\" = the main board ) , or only its `keys`" ,
			"POST /api/sort/lists/<slug>/rename": "{ name } -- the slug follows the name ; the old one redirects" ,
			"POST /api/sort/lists/<slug>/delete": "{} -- moved to output/cache/sort-lists/.trash/" ,
			"GET /api/sort/remaining":  "?list=<slug>&mods=1 -- library papers not on that list , newest first "
			                            "( login needed )" ,
		},
		"responses": {
			"200": "{ ok , rev , results: [ per op ] , summary: { list , shelf } } ( dry_run : also doc )" ,
			"400": "{ ok: false , index , op , error } -- op `index` failed ; nothing was saved" ,
			"401": "no or expired API key" ,
			"403": "{ locked: true } -- the board is view-only right now" ,
			"409": "{ rev } -- if_rev was given and the board has moved on since" ,
		},
		"row_selector": "exact key ; else a key or DOI case-insensitively ( https://doi.org/... works ) ; "
			"else \"#N\" for position N in your order. The list is searched before the staging shelf." ,
		"position": "N ( 1-based : the row ends up at #N , clamped ) , \"top\" , \"bottom\" , "
			"\"placed\" ( after the last row placed by hand ) , {\"after\": row} , {\"before\": row}" ,
		"ops": { name: {
			"does":   " ".join( ( fn.__doc__ or "" ).split() ) ,
			"params": { k: { "type": kind , "required": req , "about": about }
				for k , ( kind , req , about ) in spec.items() } ,
		} for name , ( fn , spec ) in OPS.items() } ,
		"examples": [
			{ "ops": [ { "op": "add" , "paper": "10.1038/s41593-023-01304-9" , "at": 1 } ] } ,
			{ "ops": [ { "op": "move" , "row": "#12" , "to": { "after": "#3" } } ] } ,
			{ "ops": [ { "op": "set_cell" , "row": "10.1038/s41593-023-01304-9" , "col": "Notes" ,
				"value": "decodes covert speech" , "mode": "append" } ] } ,
			{ "ops": [ { "op": "tag" , "rows": [ "#1" , "#2" ] , "add": [ "premier" ] , "remove": [ "maybe" ] } ] } ,
			{ "ops": [ { "op": "column_add" , "label": "Decoding method" } ,
				{ "op": "set_cell" , "rows": [ "#1" , "#2" ] , "col": "Decoding method" , "value": "CNN" } ] ,
			  "dry_run": True } ,
		],
	}
