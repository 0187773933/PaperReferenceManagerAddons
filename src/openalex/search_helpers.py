from rapidfuzz import fuzz
import re

def fuzzy_has( haystack , term , threshold=80 ):
	return fuzz.partial_ratio( term.lower() , haystack ) >= threshold

def any_of( *terms , threshold=80 ):
	return lambda h: any( fuzzy_has( h , t , threshold ) for t in terms )

def all_of( *terms , threshold=80 ):
	return lambda h: all( fuzzy_has( h , t , threshold ) for t in terms )

def none_of( *terms , threshold=80 ):
	return lambda h: not any( fuzzy_has( h , t , threshold ) for t in terms )

def combine_and( *predicates ):
	return lambda h: all( p( h ) for p in predicates )

def combine_or( *predicates ):
	return lambda h: any( p( h ) for p in predicates )

def normalize( text ):
	return re.sub( r'[^a-z0-9 ]+' , ' ' , re.sub( r'\s+' , ' ' , text.lower() ) ).strip()

def weighted_any( *terms , threshold=80 ):
	def _f( h ):
		h = normalize( h )
		return sum( 1 for t in terms if fuzzy_has( h , t.lower() , threshold ) )
	return _f

def at_least( n , *predicates ):
	return lambda h: sum( 1 for p in predicates if p( h ) ) >= n