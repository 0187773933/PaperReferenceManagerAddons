"""
One paper , the same on every sort list.

A paper can sit on more than one list -- the main one ( src/db/sortboard.py )
and a working set made from it ( src/db/sortlists.py ) , say. What you SAY
about it -- its tags and its cells ( Notes , Methods summary , Datasets , Code ,
any column ) -- is about the paper , not about the list , so an edit to them on
one list is made on every other list that holds the same paper. Where it sits ,
and whether it is on a list at all , stays each list's own business.

Two pure functions ; the server ( SortLists in src/server/server.py ) runs them
after every write a list takes :

  changes( before , after )        what one write changed , paper by paper
  apply( doc , changes , src )     make those changes to another list's document

It is the EDIT that travels , never the whole row : a tag put on adds that tag ,
a tag taken off removes it , a cell written replaces that cell -- so two lists
edited about the same paper at once each keep what the other one didn't touch.

What does NOT count as an edit , and so never travels :
  * a paper arriving on a list ( added , imported , copied ) -- its tags and
    cells are where it came from , not news about it ;
  * a column being added or deleted -- deleting Datasets from a working set
    must not empty Datasets everywhere else ;
  * anything on the staging shelf moving onto the list or off it.
"""

FIELDS = ( "tags" , "fields" )   # what travels ( for the docs / the schema )


def _rows( doc ):
	"""key -> row , over the list AND the shelf ( one key namespace per document ,
	see sortboard.normalize )."""
	out = {}
	for loc in ( "items" , "staging" ):
		for r in ( doc or {} ).get( loc ) or []:
			if r.get( "key" ):
				out[ r[ "key" ] ] = r
	return out


def _col_ids( doc ):
	return [ c.get( "id" ) for c in ( doc or {} ).get( "columns" ) or [] if c.get( "id" ) ]


def changes( before , after ):
	"""What `after` says differently about the papers `before` already had.

	-> { key: { "doi": "..." , "add": [ tags ] , "drop": [ lower-cased tags ] ,
	            "cells": { column id: new text } } } , only the papers with
	something in it. Both documents normalized ( a BoardState's are )."""
	B , A = _rows( before ) , _rows( after )
	# Only a column BOTH sides have can have been edited : one that is new has
	# just arrived , one that is gone was deleted -- neither is news about a paper.
	cols = [ c for c in _col_ids( after ) if c in set( _col_ids( before ) ) ]
	out = {}
	for key , a in A.items():
		b = B.get( key )
		if b is None:
			continue
		bt = { str( t ).lower(): t for t in b.get( "tags" ) or [] }
		at = { str( t ).lower(): t for t in a.get( "tags" ) or [] }
		add  = [ t for lt , t in at.items() if lt not in bt ]
		drop = [ lt for lt in bt if lt not in at ]
		bf , af = b.get( "fields" ) or {} , a.get( "fields" ) or {}
		cells = { c: af.get( c ) or "" for c in cols if ( af.get( c ) or "" ) != ( bf.get( c ) or "" ) }
		if add or drop or cells:
			out[ key ] = { "doi": str( a.get( "doi" ) or "" ).strip().lower() ,
				"add": add , "drop": drop , "cells": cells }
	return out


def touches( doc , changes ):
	"""Does `doc` hold any of the papers in `changes` ( by key or DOI , as apply()
	finds them ) ? A cheap look before a list is copied to have them applied."""
	rows = _rows( doc )
	if any( k in rows for k in changes ):
		return True
	dois = { ch.get( "doi" ) for ch in changes.values() if ch.get( "doi" ) }
	return bool( dois ) and any( str( r.get( "doi" ) or "" ).strip().lower() in dois for r in rows.values() )


def apply( doc , changes , src ):
	"""Make `changes` ( from changes() above ) to `doc` , in place -- another
	list's document. `src` is the document they came out of , for the colour a
	tag new to `doc` is drawn in and the label of a column `doc` hasn't got.

	A paper is found by its key , or failing that by its DOI ( one list may have
	adopted the library's key for a paper the other still holds under its DOI ).
	A cell for a column `doc` lacks brings the column with it -- folded if it is
	folded where it came from -- the way ⇢ Copy to list does , since a cell with
	no column would be dropped on save.

	-> how many of `doc`'s papers changed ( 0 : nothing to save )."""
	if not changes:
		return 0
	rows   = _rows( doc )
	by_doi = {}
	for r in rows.values():
		d = str( r.get( "doi" ) or "" ).strip().lower()
		if d:
			by_doi.setdefault( d , r )
	have_cols = set( _col_ids( doc ) )
	src_cols  = { c.get( "id" ): c for c in ( src or {} ).get( "columns" ) or [] }
	src_fold  = set( ( ( src or {} ).get( "options" ) or {} ).get( "fold_cols" ) or [] )
	src_tags  = { str( t.get( "name" ) ).lower(): t for t in ( ( src or {} ).get( "vocab" ) or {} ).get( "tags" ) or [] }
	vocab     = doc.setdefault( "vocab" , {} ).setdefault( "tags" , [] )
	known     = { str( t.get( "name" ) ).lower() for t in vocab }
	n = 0
	for key , ch in changes.items():
		r = rows.get( key ) or ( by_doi.get( ch.get( "doi" ) ) if ch.get( "doi" ) else None )
		if r is None:
			continue
		hit  = False
		tags = list( r.get( "tags" ) or [] )
		drop = set( ch.get( "drop" ) or [] )
		kept = [ t for t in tags if str( t ).lower() not in drop ]
		hit |= len( kept ) != len( tags )
		have = { str( t ).lower() for t in kept }
		for t in ch.get( "add" ) or []:
			lt = str( t ).lower()
			if lt in have:
				continue
			kept.append( t )
			have.add( lt )
			hit = True
			if lt not in known:
				vocab.append( { "name": t , "color": ( src_tags.get( lt ) or {} ).get( "color" ) or "" } )
				known.add( lt )
		r[ "tags" ] = kept
		fields = r.setdefault( "fields" , {} )
		for cid , text in ( ch.get( "cells" ) or {} ).items():
			if ( fields.get( cid ) or "" ) == text:
				continue
			if cid not in have_cols:
				doc.setdefault( "columns" , [] ).append(
					{ "id": cid , "label": ( src_cols.get( cid ) or {} ).get( "label" ) or cid } )
				have_cols.add( cid )
				if cid in src_fold:
					opts = doc.setdefault( "options" , {} )
					opts[ "fold_cols" ] = list( opts.get( "fold_cols" ) or [] ) + [ cid ]
			fields[ cid ] = text
			hit = True
		n += hit
	return n
