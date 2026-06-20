"""
Dashboard search primitives.

Pure, stateless helpers over the in-memory pools the indexer ( see
indexer.py ) produces : tokenizing a query , AND/phrase matching against a
row's lowercase 'hay' field , sorting , and the author roll-up. The pools
themselves are built + persisted by indexer.py , independently of the
missing.xlsx pipeline.
"""

import re


# ---------------------------------------------------------------------------
# Author roll-up ( derived from the references pool )
# ---------------------------------------------------------------------------

def build_authors( references_rows , top_n ):
	"""Top authors across the missing references pool , ranked by total
	library cites then paper count."""
	from collections import Counter
	author_papers = Counter()
	author_cites  = Counter()
	author_meta   = {}
	for r in references_rows:
		n = r.get( "lib_cites" ) or 0
		for aid , name , orcid in r.get( "authors" ) or []:
			if not aid:
				continue
			author_papers[ aid ] += 1
			author_cites[ aid ]  += n
			if aid not in author_meta:
				author_meta[ aid ] = { "name": name or "(unknown)" , "orcid": orcid }
	ranked = sorted(
		author_cites.items() ,
		key=lambda kv: ( kv[ 1 ] , author_papers[ kv[ 0 ] ] ) ,
		reverse=True ,
	)[ : top_n ]
	out = []
	for aid , total in ranked:
		info = author_meta.get( aid , {} )
		out.append( {
			"author":    info.get( "name" ) or "(unknown)" ,
			"orcid":     info.get( "orcid" ) or "" ,
			"openalex":  aid ,
			"papers":    author_papers[ aid ] ,
			"lib_cites": total ,
		} )
	return out


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

# Boolean query : implicit AND between terms , plus OR , NOT / -term , and
# "quoted phrases". Precedence : AND binds tighter than OR , so
#   fMRI NOT EEG              -> has "fmri" AND NOT "eeg"
#   "inner speech" OR aphasia
#   bci -invasive             -> has "bci" AND NOT "invasive"
# A query parses to a list of OR-clauses ; each clause has positive +
# negative substring literals. A row matches if ANY clause matches ( all its
# positives present , none of its negatives present ).

_QUERY_TOKEN_RE = re.compile( r'(-)?"([^"]*)"|(\S+)' )


def _parse_query( query ):
	query = ( query or "" ).strip()
	if not query:
		return []
	clauses     = [ { "pos": [] , "neg": [] } ]
	pending_not = False
	for m in _QUERY_TOKEN_RE.finditer( query ):
		dash , phrase , word = m.group( 1 ) , m.group( 2 ) , m.group( 3 )
		if phrase is not None:
			# Quoted -> always literal ( so you can search the words "or" /
			# "and" / "not" themselves , e.g. "fight or flight" ).
			lit , neg = phrase.lower() , bool( dash )
		else:
			op = word.upper()   # keywords are case-insensitive : OR == or == Or
			if op in ( "AND" , "OR" , "NOT" ):
				if op == "OR":
					clauses.append( { "pos": [] , "neg": [] } )
					pending_not = False
				elif op == "NOT":
					pending_not = True
				# AND is implicit -> ignore
				continue
			if word.startswith( "-" ) and len( word ) > 1:
				lit , neg = word[ 1: ].lower() , True
			else:
				lit , neg = word.lower() , False
		if not lit:
			continue
		neg = neg or pending_not
		pending_not = False
		( clauses[ -1 ][ "neg" ] if neg else clauses[ -1 ][ "pos" ] ).append( lit )
	return [ c for c in clauses if c[ "pos" ] or c[ "neg" ] ]


def _matches( hay , clauses ):
	for c in clauses:
		if all( p in hay for p in c[ "pos" ] ) and not any( n in hay for n in c[ "neg" ] ):
			return True
	return False


_SORT_KEYS = {
	"lib_cites": lambda r: ( r.get( "lib_cites" ) or 0 , r.get( "cited_by" ) or 0 ) ,
	"cited_by":  lambda r: ( r.get( "cited_by" )  or 0 , r.get( "lib_cites" ) or 0 ) ,
	"year":      lambda r: ( r.get( "year" )      or 0 , r.get( "cited_by" ) or 0 ) ,
	"title":     lambda r: ( r.get( "title" )     or "" ).lower() ,
}


def search( rows , query , sort="lib_cites" , limit=100 , offset=0 , direction=None ):
	"""Boolean full-text search over a pool ( see _parse_query ). Sorts the
	WHOLE matched set , then returns the `limit` slice starting at `offset`
	( so paging / infinite-scroll stays globally sorted , not per-page ).
	Returns ( total_hits , page )."""
	clauses = _parse_query( query )
	hits = [ r for r in rows if _matches( r[ "hay" ] , clauses ) ] if clauses else list( rows )

	key = _SORT_KEYS.get( sort , _SORT_KEYS[ "lib_cites" ] )
	reverse = ( sort != "title" ) if direction is None else ( direction == "desc" )
	hits.sort( key=key , reverse=reverse )

	offset = max( 0 , offset )
	return len( hits ) , hits[ offset : offset + max( 0 , limit ) ]


def to_public( row ):
	"""Strip the heavy / internal 'hay' field before serialization."""
	return { k: v for k , v in row.items() if k != "hay" }
