"""
Dashboard search primitives.

Pure, stateless helpers over the in-memory pools the indexer ( see
indexer.py ) produces : tokenizing a query , AND/phrase matching against a
row's lowercase 'hay' field , sorting , and the author roll-up. The pools
themselves are built + persisted by indexer.py , independently of the
missing.xlsx pipeline.
"""

import re
import unicodedata


# ---------------------------------------------------------------------------
# Text folding
#
# The searchable text and the query BOTH go through normalize_text() , so
# punctuation can never be the reason a paper doesn't come back. Everything
# that isn't a letter or a digit -- colons , slashes , hyphens and en / em
# dashes , brackets , curly quotes , the lot -- flattens to a single space , and
# accents are stripped. So :
#
#   "MindAlign: Decoding Inner Speech"   is found by   mindalign decoding
#   "EEG-fMRI"                           is found by   "eeg fmri" or eeg-fmri
#   "10.1002/(SICI)1099-1492(199908)12:5"  is found by pasting the DOI back in
#   "Müller"                             is found by   muller
#
# Matching itself stays a plain substring test ( fast C ) , just against folded
# text on both sides.
# ---------------------------------------------------------------------------

# Letters NFKD leaves alone because they aren't accent + base ( so stripping
# combining marks doesn't reach them ). Without this they'd fold to a space and
# split the word in two.
_LETTER_MAP = str.maketrans( {
	"ø":"o" , "Ø":"o" , "æ":"ae" , "Æ":"ae" , "œ":"oe" , "Œ":"oe" ,
	"ß":"ss" , "đ":"d" , "Đ":"d" , "ð":"d" , "Ð":"d" , "ł":"l" , "Ł":"l" ,
	"þ":"th" , "Þ":"th" , "ı":"i" ,
} )

_NONALNUM_RE = re.compile( r"[^a-z0-9]+" )

# The combining marks NFKD splits accents into. Stripped with ONE regex pass
# rather than a per-character Python loop -- these haystacks include whole OCR'd
# papers , and a Python loop over every character of those costs minutes across
# a library.
_COMBINING_RE = re.compile(
	"[̀-ͯ҃-҉᪰-᫿᷀-᷿⃐-⃰︠-︯]" )


# Runs of non-ASCII characters. A haystack here can be a whole OCR'd paper ,
# nearly all of it ASCII with a handful of accented words in it , so the accent
# work runs ONLY on those short runs ( via this sub's callback ) instead of
# decomposing megabytes of text that has nothing to decompose.
_NONASCII_RE = re.compile( r"[^\x00-\x7f]+" )


def _fold_run( m ):
	run = unicodedata.normalize( "NFKD" , m.group( 0 ).translate( _LETTER_MAP ) )
	return _COMBINING_RE.sub( "" , run )


def normalize_text( s ):
	"""Fold text to lowercase alphanumeric words joined by single spaces."""
	if not s:
		return ""
	s = str( s )
	# .isascii() is a cheap C scan , and skips the accent pass entirely for text
	# that has no accents to strip.
	if not s.isascii():
		s = _NONASCII_RE.sub( _fold_run , s )
	return _NONALNUM_RE.sub( " " , s.lower() ).strip()


def prepare_rows( rows ):
	"""Fold a pool's haystacks in place , ONCE ( idempotent -- rows already done
	are skipped ) , folding the DOI / key / journal in at the same time so a
	pasted DOI or record key finds its paper.

	It REPLACES the raw haystack rather than keeping a folded copy beside it :
	nothing else reads that field ( to_public() strips it ) , and on a library
	whose haystacks carry the full OCR body a second copy would double the
	index's memory."""
	for r in rows:
		if r.get( "hay_n" ):
			continue
		# The TITLE is kept folded on its own as well : a term in the title means
		# something completely different from the same term on page 9 , and
		# ranking ( see _relevance ) is the whole difference between finding a
		# paper and finding it at position 261.
		r[ "hay_t" ] = normalize_text( r.get( "title" ) )
		# The identifiers on their own , so a pasted DOI / record key finds its
		# paper in the TITLE search too ( where the body haystack isn't looked at ).
		r[ "hay_k" ] = normalize_text( " ".join( str( r.get( k ) or "" ) for k in
			( "doi" , "key" ) ) )
		r[ "hay" ]   = normalize_text( " ".join( str( r.get( k ) or "" ) for k in
			( "hay" , "doi" , "key" , "journal" ) ) )
		r[ "hay_n" ] = True
	return rows


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
			lit , neg = normalize_text( phrase ) , bool( dash )
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
				lit , neg = normalize_text( word[ 1: ] ) , True
			else:
				lit , neg = normalize_text( word ) , False
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


# ---------------------------------------------------------------------------
# Relevance
#
# Matching says whether a paper contains your words ; this says how much it
# LOOKS LIKE the paper you asked for. Without it , a search for
# " silent verb generation " ranks by citation count , which puts the 300-paper
# pile that merely mentions those words in some paragraph above the one paper
# actually CALLED "Silent Verb Generation during fMRI ..." -- it came back 261st.
#
# The weights say , in order : the whole query is the title ; the whole query is
# IN the title ; every word is in the title ; some words are ; the whole query
# appears as a phrase somewhere ; the words show up early ( title + abstract
# region ) rather than deep in the body. Citations only break ties.
# ---------------------------------------------------------------------------

_HEAD_CHARS = 1200      # ~ the title + abstract region of a folded haystack


def _relevance( row , terms , phrase ):
	t    = row.get( "hay_t" ) or ""
	hay  = row.get( "hay" )   or ""
	head = hay[ :_HEAD_CHARS ]
	score = 0
	if phrase:
		if t == phrase:          score += 400
		elif phrase in t:        score += 300
		if phrase in head:       score += 90
		elif phrase in hay:      score += 60
	if terms:
		in_title = sum( 1 for w in terms if w in t )
		score += 45 * in_title
		if in_title == len( terms ):
			score += 200
		score += 12 * sum( 1 for w in terms if w in head )
	return score


_SORT_KEYS = {
	# How well the row answers the QUERY ( see _relevance ) , citations breaking
	# ties. The default whenever there's something to be relevant to.
	"relevance":  lambda r: ( r.get( "rel" ) or 0 , r.get( "lib_cites" ) or 0 ,
	                          r.get( "cited_by" ) or 0 ) ,
	"lib_cites":  lambda r: ( r.get( "lib_cites" ) or 0 , r.get( "cited_by" ) or 0 ) ,
	"cited_by":   lambda r: ( r.get( "cited_by" )  or 0 , r.get( "lib_cites" ) or 0 ) ,
	"year":       lambda r: ( r.get( "year" )      or 0 , r.get( "cited_by" ) or 0 ) ,
	"title":      lambda r: ( r.get( "title" )     or "" ).lower() ,
	# created_at is the per-paper record's ISO-8601 timestamp ( In-Library
	# "Added" column ) ; lexicographic order == chronological. Blank ( older
	# records that predate the field ) sorts last on the default desc.
	"created_at": lambda r: ( r.get( "created_at" ) or "" ) ,
	# "Code" column ( In-Library ) : how many source-code / data links prma code
	# harvested -- so you can float the papers that actually ship code to the top.
	"code":       lambda r: ( len( r.get( "code_links" ) or [] ) , r.get( "cited_by" ) or 0 ) ,
}

# Columns whose NATURAL ( first-click ) direction is ascending ; every other
# column defaults to descending.
_ASC_DEFAULT = { "title" }


def _sort_levels( sort , direction ):
	"""Parse the sort spec into an ordered list of ( keyfn , reverse ) levels ,
	PRIMARY FIRST. Both `sort` and `direction` may be comma-joined to express a
	TIERED sort ( e.g. sort='year,code' dir='desc,desc' -> sort by year , then
	by the code-link count as a tiebreaker ). A missing / blank per-level
	direction falls back to that column's natural default ( see _ASC_DEFAULT )."""
	keys = [ s for s in ( sort or "" ).split( "," ) if s ] or [ "lib_cites" ]
	dirs = ( direction or "" ).split( "," )
	levels = []
	for i , k in enumerate( keys ):
		d = dirs[ i ] if ( i < len( dirs ) and dirs[ i ] ) else None
		reverse = ( k not in _ASC_DEFAULT ) if d is None else ( d == "desc" )
		levels.append( ( _SORT_KEYS.get( k , _SORT_KEYS[ "lib_cites" ] ) , reverse ) )
	return levels


def search( rows , query , sort="relevance" , limit=100 , offset=0 , direction=None ):
	"""Boolean full-text search over a pool ( see _parse_query ). Sorts the
	WHOLE matched set , then returns the `limit` slice starting at `offset`
	( so paging / infinite-scroll stays globally sorted , not per-page ).
	`sort` / `direction` may be comma-joined for a TIERED ( multi-column ) sort
	-- see _sort_levels. Returns ( total_hits , page , fuzzy ) , where fuzzy
	says the exact search found NOTHING and these are near-miss titles."""
	prepare_rows( rows )        # one-time text fold ; free after the first call
	clauses = _parse_query( query )
	hits = [ r for r in rows if _matches( r[ "hay" ] , clauses ) ] if clauses else list( rows )

	# Nothing at all -> it's likelier to be a typo than an empty library , so
	# fall back to near-miss TITLES rather than an empty page.
	fuzzy = False
	if clauses and not hits:
		hits  = _fuzzy_titles( rows , query )
		fuzzy = bool( hits )

	# Score the matched set against the query so the paper the query DESCRIBES
	# outranks the pile that merely mentions the words ( see _relevance ). Only
	# the hits are scored , so this costs nothing on a broad browse.
	if clauses:
		terms  = sorted( { p for c in clauses for p in c[ "pos" ] } )
		phrase = normalize_text( query )
		for r in hits:
			r[ "rel" ] = _relevance( r , terms , phrase )

	# Stable multi-key sort : apply the LEAST-significant level first so the
	# primary level ends up dominant and the earlier levels break its ties.
	# Single-level ( the common case ) is just one pass.
	for keyfn , reverse in reversed( _sort_levels( sort , direction ) ):
		hits.sort( key=keyfn , reverse=reverse )

	offset = max( 0 , offset )
	return len( hits ) , hits[ offset : offset + max( 0 , limit ) ] , fuzzy


# How close a title has to be to count as a near miss , and how many to offer.
_FUZZY_CUTOFF , _FUZZY_LIMIT = 76 , 40


def _fuzzy_titles( rows , query ):
	"""Near-miss titles for a query that matched NOTHING -- a typo , a half-
	remembered title. Titles only ( scoring whole OCR bodies fuzzily would cost
	seconds ) , and only ever on the empty-result path , so the normal search
	never pays for it."""
	q = normalize_text( query )
	if len( q ) < 4:
		return []
	try:
		from rapidfuzz import fuzz , process
	except Exception:
		return []
	titles = [ r.get( "hay_t" ) or "" for r in rows ]
	try:
		found = process.extract( q , titles , scorer=fuzz.partial_ratio ,
			score_cutoff=_FUZZY_CUTOFF , limit=_FUZZY_LIMIT )
	except Exception:
		return []
	out = []
	for _ , score , i in found:
		r = rows[ i ]
		r[ "rel" ] = int( score )
		out.append( r )
	return out


# ---------------------------------------------------------------------------
# Title lookup  ( what /sort and /tiers search with )
#
# Those boards aren't browsing the library , they're fetching ONE paper you can
# half-remember : you type a piece of a title and it has to come back. The
# boolean search above is the wrong tool for that. Every word you type is a hard
# AND against the whole OCR'd body , so ONE word you misremember -- a plural , a
# hyphen turned into a word , a subtitle that isn't there -- returns nothing at
# all ; and a word that IS right but also sits on page 9 of three hundred other
# papers buries the paper you meant under them.
#
# So this scores TITLES only , requires nothing to match , and always comes back
# closest-first :
#
#   the title IS what you typed          >  the whole thing you typed is IN the
#   title  >  every word of it is in the title  >  most of them are  >  it just
#   LOOKS like it ( rapidfuzz -- typos and swapped words still land )
#
# and a pasted DOI / record key jumps straight to its paper.
# ---------------------------------------------------------------------------

_TITLE_WIDTH = 400      # how many near-misses the C pre-filter hands on for scoring
_TITLE_FLOOR = 45       # ... and how alike they have to be to be worth scoring
_TITLE_SEED  = 60       # fewer substring matches than this -> sweep the whole pool
_TITLE_KEEP  = 200      # the weakest score still worth showing at all


def _term_credit( term , words , t , ratio ):
	"""What ONE word you typed is worth against a title : the title has that word
	( 1.0 ) , has it inside a longer word ( 0.6 ) , has a word close enough to be
	that one typo'd ( 0.8 ) , or doesn't ( 0 )."""
	if term in words:
		return 1.0
	if term in t:
		return 0.6
	if ratio and len( term ) > 3:
		for w in words:
			if abs( len( w ) - len( term ) ) <= 3 and ratio( term , w ) >= 82:
				return 0.8
	return 0.0


def _title_score( q , terms , t , ratio=None ):
	"""How much a folded title `t` looks like the folded query `q` , as points.
	Everything here is about WHERE your words landed , not how many there are , so
	a short exact title outranks a long one that merely swallows the phrase."""
	if not t:
		return 0.0
	score = 0.0
	if t == q:
		score += 400
	elif q in t:
		# The whole thing you typed is in the title -- the strongest signal there
		# is , and worth more the more of the title it accounts for , so
		# " inner speech " puts the paper CALLED that above the forty whose
		# titles happen to contain the phrase.
		score += 250 + 70 * ( len( q ) / len( t ) )
		if t.startswith( q ):
			score += 40
	if terms:
		# Long words carry the query : " reconstruction " says what you're after
		# and " of " doesn't , so each word counts for its own length. That's also
		# what keeps a typo in the word that MATTERS from handing the top spot to
		# papers that only share your " of " and " from ".
		words = set( t.split() )
		got = weight = 0.0
		for w in terms:
			got    += _term_credit( w , words , t , ratio ) * len( w )
			weight += len( w )
		cover = ( got / weight ) if weight else 0.0
		score += 220 * cover
		if cover >= 0.95:
			score += 90
	return score


def _title_ratios( q , terms , titles ):
	"""( index , 0-100 ) for the titles nearest `q` , or None if rapidfuzz isn't
	installed -- the typo tolerance, in C.

	Two stages, because the external pools are 150 000 rows and the fuzzy scorer
	costs a second and a half across all of them, on every keystroke. So a plain
	substring pass ( milliseconds ) first keeps the titles sharing ANY word with
	the query, and the scorer only sees those. It's only when that pass finds
	almost nothing -- you typo'd every word you typed -- that the scorer has to
	sweep the whole pool, and that's the rare case, not the one you're in while
	typing."""
	try:
		from rapidfuzz import fuzz , process
	except Exception:
		return None
	# Seed on the words that actually narrow anything : " of " and " from " are in
	# half the library, so including them would keep half the pool and cost what
	# the sweep costs. Longest first, since that's the rarest -- `any()` then
	# gives up on the first word for most titles.
	words = sorted( [ w for w in terms if len( w ) > 3 ] or terms ,
		key=len , reverse=True )
	seed  = { i: t for i , t in enumerate( titles ) if any( w in t for w in words ) } \
		if words else None
	try:
		# processor=None : both sides are already folded by normalize_text().
		found = process.extract( q ,
			seed if ( seed is not None and len( seed ) >= _TITLE_SEED ) else titles ,
			scorer=fuzz.WRatio , processor=None ,
			limit=_TITLE_WIDTH , score_cutoff=_TITLE_FLOOR )
	except Exception:
		return None
	return [ ( i , s ) for _ , s , i in found ]


def title_search( rows , query , limit=25 , offset=0 ):
	"""Fuzzy title lookup over a pool -- see the note above. Same shape as
	search() : ( total_hits , page , fuzzy ). Every row that resembles the query
	at all comes back , best first , with its score in `rel`."""
	prepare_rows( rows )
	q = normalize_text( query )
	if not q:
		hits = sorted( rows , key=_SORT_KEYS[ "lib_cites" ] , reverse=True )
		return len( hits ) , hits[ offset : offset + max( 0 , limit ) ] , False

	terms  = q.split()
	titles = [ r.get( "hay_t" ) or "" for r in rows ]
	near   = _title_ratios( q , terms , titles )
	word_ratio = None
	if near is None:
		# No rapidfuzz : fall back to titles sharing a word with the query. Coarser
		# ( a typo stops matching ) but the page still works.
		near = [ ( i , 0 ) for i , t in enumerate( titles )
			if q in t or any( w in t for w in terms ) ]
	else:
		# Word-against-word typo matching , for the few hundred titles that got
		# this far only ( see _term_credit ).
		from rapidfuzz import fuzz
		word_ratio = fuzz.ratio

	scores = {}
	for i , ratio in near:
		scores[ i ] = _title_score( q , terms , titles[ i ] , word_ratio ) + 3.0 * ratio
	# A pasted DOI / record key wins outright , whatever its title looks like.
	if len( q ) > 5:
		for i , r in enumerate( rows ):
			if q in ( r.get( "hay_k" ) or "" ):
				scores[ i ] = scores.get( i , 0.0 ) + 900

	if not scores:
		return 0 , [] , False

	# Cut the tail off. The pre-filter is deliberately generous ( it has to be ,
	# or a typo finds nothing ) , which means the bottom of it is papers sharing
	# one common word with what you typed -- noise , and enough of it to make the
	# real answer look lost. Anything under two fifths of the best hit goes.
	best  = max( scores.values() )
	floor = max( _TITLE_KEEP , 0.4 * best )
	keep  = [ i for i , sc in scores.items() if sc >= floor ]
	# ... unless that leaves nothing to look at , in which case the closest few
	# are still better than an empty panel ( fuzzy=True says so on the page ).
	fuzzy = not keep
	if fuzzy:
		keep = sorted( scores , key=lambda i: scores[ i ] , reverse=True )[ :8 ]

	hits = []
	for i in keep:
		rows[ i ][ "rel" ] = int( scores[ i ] )
		hits.append( rows[ i ] )
	hits.sort( key=lambda r: ( r.get( "rel" ) or 0 , r.get( "lib_cites" ) or 0 ,
		r.get( "cited_by" ) or 0 ) , reverse=True )
	return len( hits ) , hits[ offset : offset + max( 0 , limit ) ] , fuzzy


def to_public( row ):
	"""Strip the heavy / internal haystack fields before serialization."""
	return { k: v for k , v in row.items()
		if k not in ( "hay" , "hay_n" , "hay_t" , "hay_k" ) }
