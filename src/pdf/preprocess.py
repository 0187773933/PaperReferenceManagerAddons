"""
src/pdf/preprocess.py

Per-paper READING ORDER + SECTION CLASSIFICATION. Take the YOLO bboxes
( already pinned with OCR text by src/pdf/ocr.py ) and produce two
things :

  1. A reading-order permutation of each page's detection indexes
     ( so consumers can iterate paper[ 'yolo' ] in the order a human
     would read it , without us duplicating any bbox / text data ) ;
  2. A coarse semantic grouping of every detection into one of a
     fixed set of section buckets ( title , abstract , introduction ,
     methods , results , figures , tables , ... ) -- each bucket is
     a list of [ page_idx , det_idx ] pairs that index back into
     paper[ 'yolo' ][ 'pages' ][ page_idx ].

This module does NOT re-render the PDF , re-run YOLO , or re-OCR
anything -- it only sorts and labels what's already pinned.

Reading-order algorithm : recursive XY-cut.

  for each page :
    1. drop 'abandon' page-furniture detections ( running headers ,
       page numbers , footers ) -- a human doesn't read those in flow ;
    2. recursively cut the remaining boxes : alternate between the
       horizontal axis ( find a vertical whitespace band tall enough
       to separate top from bottom ) and the vertical axis ( find a
       horizontal whitespace band wide enough to separate left column
       from right column ). Each level picks the largest qualifying
       gap and splits there.
    3. when no qualifying gap exists on either axis , fall back to a
       deterministic top-down , left-right sort of the leaf group.

  This handles the layouts academic papers actually use : single
  column , two column , and the mixed "full-width header ( title /
  authors / abstract ) then two columns of body" pattern.

Section-classification algorithm : two-pass over the reading order.

  Pass 1 : walk reading order ; route figures and tables to their own
  buckets ; build a flat "prose flow" of every other det ; mark
  positions of section anchors ( `title` dets whose text matches a
  known section keyword ) and the paper title ( first `title` det
  that isn't an anchor ).

  Pass 2 : assign every flow position to a bucket --
    - anchor positions get their matched bucket name ;
    - the paper title position gets 'title' ;
    - positions between two anchors inherit the earlier anchor's
      bucket ;
    - positions after the last anchor inherit it ;
    - positions BEFORE the first anchor use position to decide :
        * if the first anchor IS 'abstract' , front matter is 'misc'
          ( authors / affiliations live before the abstract header ) ;
        * else if the position is before the paper title , 'misc'
          ( journal banner / running head ) ;
        * else 'abstract' ( the structured-abstract case : papers
          like JAMA have IMPORTANCE / OBJECTIVE / ... blocks but no
          "Abstract" header , so we infer from position ) ;
    - references positional fallback : if no 'references' anchor was
      found , scan the tail of the prose flow for reference-pattern
      text ( DOIs , "et al." , "(2024)" , "[12]" ) and reclassify the
      contiguous trailing run.

  Figures and tables get a text sanity-check : YOLO sometimes labels
  body text as a caption , so a caption-classed det whose first line
  ISN'T "Figure N" / "Table N" falls through to the prose flow
  instead.

Top-level entry point :

  preprocess_paper( paper )

  Returns ( yolo_sorted_page_indexes , sections ) :
    yolo_sorted_page_indexes : list-of-lists of det indexes in reading
      order , one inner list per page. 'abandon' / bad-bbox dets
      omitted ; each value indexes paper[ 'yolo' ][ 'pages' ][ page_idx ].
    sections : dict keyed by SECTION_KEYS , each value a list of
      [ page_idx , det_idx ] pairs.
  Does NOT mutate `paper` -- the caller assigns the results onto
  paper[ 'yolo_sorted_page_indexes' ] and paper[ 'sections' ] and
  persists.
"""

import re


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# YOLO classes for page furniture we exclude from reading flow ; these
# are headers , page numbers , and footers that a human ignores when
# reading the paper.
ABANDON_CLASSES = { "abandon" }

# Safety cap on XY-cut recursion depth. A real layout should resolve
# in well under this ; we bail to a plain sort if we somehow blow past.
MAX_CUT_DEPTH = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page_extent( dets ):
	"""Approximate page width / height from the union of detection
	bboxes ( we don't have the raw page dimensions in the yolo dict ,
	but the union is close enough for picking gap thresholds )."""
	if not dets:
		return 0.0 , 0.0
	x2s = [ d[ "bbox" ][ 2 ] for d in dets if d.get( "bbox" ) ]
	y2s = [ d[ "bbox" ][ 3 ] for d in dets if d.get( "bbox" ) ]
	return ( float( max( x2s ) ) if x2s else 0.0 ,
	         float( max( y2s ) ) if y2s else 0.0 )


# ---------------------------------------------------------------------------
# XY-cut
# ---------------------------------------------------------------------------

def _find_split( dets , axis , min_gap ):
	"""Find the largest whitespace gap along `axis` ( 'x' or 'y' ) that
	exceeds min_gap , and split dets into two groups across it.

	Returns [ group_low , group_high ] ( each non-empty ) or None if no
	qualifying gap exists. The 'gap' is a band on the axis where no det
	's bbox extends -- we sweep sorted by axis-start , tracking the
	running max of axis-end , and any time the next det starts beyond
	that max + min_gap we've found a candidate gap of that size."""
	if len( dets ) < 2:
		return None
	i_lo , i_hi = ( 0 , 2 ) if axis == "x" else ( 1 , 3 )

	sorted_dets = sorted( dets , key=lambda d: d[ "bbox" ][ i_lo ] )
	running_max = sorted_dets[ 0 ][ "bbox" ][ i_hi ]
	best_gap , best_split = 0.0 , None

	for d in sorted_dets[ 1: ]:
		lo = d[ "bbox" ][ i_lo ]
		if lo > running_max:
			gap = lo - running_max
			if gap >= min_gap and gap > best_gap:
				best_gap = gap
				best_split = ( running_max + lo ) / 2.0
		hi = d[ "bbox" ][ i_hi ]
		if hi > running_max:
			running_max = hi

	if best_split is None:
		return None

	low , high = [] , []
	for d in dets:
		mid = ( d[ "bbox" ][ i_lo ] + d[ "bbox" ][ i_hi ] ) / 2.0
		if mid < best_split:
			low.append( d )
		else:
			high.append( d )
	if not low or not high:
		return None
	return [ low , high ]


def _xy_cut( dets , primary , min_x_gap , min_y_gap , depth=0 ):
	"""Recursive XY-cut. `primary` in { 'x' , 'y' } picks which axis to
	try first at this level ; classic XY-cut alternates. Returns the
	dets flattened in reading order."""
	n = len( dets )
	if n <= 1:
		return list( dets )
	if depth > MAX_CUT_DEPTH:
		return sorted( dets , key=lambda d: ( d[ "bbox" ][ 1 ] , d[ "bbox" ][ 0 ] ) )

	primary_min = min_y_gap if primary == "y" else min_x_gap
	other       = "x" if primary == "y" else "y"
	other_min   = min_x_gap if other   == "x" else min_y_gap

	groups = _find_split( dets , primary , primary_min )
	used_axis = primary
	if groups is None:
		groups = _find_split( dets , other , other_min )
		used_axis = other
		if groups is None:
			# Leaf : neither axis has a qualifying gap. Sort by top-down
			# then left-right ( column-friendly within a tight cluster ).
			return sorted( dets , key=lambda d: ( d[ "bbox" ][ 1 ] , d[ "bbox" ][ 0 ] ) )

	i_lo = 0 if used_axis == "x" else 1
	groups.sort( key=lambda g: min( d[ "bbox" ][ i_lo ] for d in g ) )

	next_axis = "x" if used_axis == "y" else "y"
	out = []
	for g in groups:
		out.extend( _xy_cut( g , next_axis , min_x_gap , min_y_gap , depth + 1 ) )
	return out


# ---------------------------------------------------------------------------
# Per-page / per-paper entry points
# ---------------------------------------------------------------------------

def preprocess_page( page_dets ):
	"""Return one page's reading-order permutation of detection indexes.

	Indexes are positions in the original `page_dets` list. 'abandon'
	and any det without a valid bbox are omitted ( so the returned
	list is usually shorter than `page_dets` )."""
	# Build (orig_idx , det) pairs , filter , and wrap each as a thin
	# dict the xy-cut code can read like a real det. We carry the
	# original index through so the final permutation maps back to
	# paper[ 'yolo' ][ 'pages' ][ page_idx ].
	keep = []
	for idx , d in enumerate( page_dets or [] ):
		if not isinstance( d , dict ):
			continue
		if d.get( "type" ) in ABANDON_CLASSES:
			continue
		bbox = d.get( "bbox" )
		if not bbox or len( bbox ) != 4:
			continue
		keep.append( { "idx": idx , "bbox": bbox } )
	if not keep:
		return []

	page_w , page_h = _page_extent( keep )
	# Gap thresholds scale with the page so we work at any DPI , with
	# a floor so a near-empty page doesn't degenerate. We tuned these
	# small : YOLO bboxes are already tight per-block , so any gap we
	# see between bboxes is real whitespace -- the only thing we want
	# to AVOID is splitting within-block jitter. Two-column journal
	# gutters are routinely only ~25-30 px at 200 DPI ( e.g. JAMA
	# Neurology ) , so we keep the x-threshold well under that.
	min_x_gap = max( 10.0 , page_w * 0.012 )
	min_y_gap = max(  8.0 , page_h * 0.008 )

	# Start by looking for horizontal cuts ( top / middle / bottom
	# bands ) , then vertical cuts ( columns ) within each band.
	ordered = _xy_cut(
		keep , primary="y" ,
		min_x_gap=min_x_gap , min_y_gap=min_y_gap ,
	)
	return [ w[ "idx" ] for w in ordered ]


# ---------------------------------------------------------------------------
# Section classification
# ---------------------------------------------------------------------------

# Fixed list of section buckets we emit. Order here is the order of
# keys in the returned dict ( Python 3.7+ preserves insertion order ).
SECTION_KEYS = (
	"title"        ,
	"abstract"     ,
	"introduction" ,
	"background"   ,
	"methods"      ,
	"results"      ,
	"figures"      ,
	"tables"       ,
	"conclusions"  ,
	"future"       ,
	"misc"         ,
	"references"   ,
)

# YOLO detection classes routed to the figures / tables buckets
# regardless of where they appear in reading order.
FIGURE_CLASSES = { "figure" , "figure_caption" }
TABLE_CLASSES  = { "table" , "table_caption" , "table_footnote" }

# OCR engines stored under det[ 'ocr' ][ engine ] ; we prefer the
# fastest / most accurate first when a det has results from more
# than one engine ( e.g. rapid + paddle ).
OCR_ENGINE_PRIORITY = ( "rapid" , "paddle" , "surya" , "tesseract" )

# Section-header keyword patterns. Tested in order ; the first match
# wins. Ordered most-specific first so "results and discussion" maps
# to results rather than landing in some other bucket , and so
# "discussion" alone also lands in results ( the user's bucket list
# has no separate 'discussion' ; results is the closest fit ).
SECTION_HEADER_PATTERNS = (
	( "references"   , r"^(references?|bibliography|literature\s+cited|works?\s+cited|reference\s+list)\b" ) ,
	( "future"       , r"^(future\s+(work|directions?|research|prospects?)|limitations?(\s+and\s+future)?)\b" ) ,
	( "conclusions"  , r"^(conclusions?|concluding\s+remarks|summary\s+and\s+conclusions?)\b" ) ,
	( "results"      , r"^(results?(\s+and\s+discussion)?|findings?|discussion)\b" ) ,
	( "methods"      , r"^(methods?|methodology|materials?\s+(and|&)\s+methods?|patients?\s+(and|&)\s+methods?|experimental(\s+(methods?|design))?|study\s+design|participants?|procedures?|data\s+collection|data\s+analysis|statistical\s+analysis)\b" ) ,
	( "background"   , r"^(background|related\s+work|prior\s+work|literature\s+review|prior\s+art)\b" ) ,
	( "introduction" , r"^(introduction)\b" ) ,
	( "abstract"     , r"^(abstract|summary)\b" ) ,
)

# Strip a leading "1." / "1.2" / "III." / "I." style numbering prefix
# from a header line before matching keywords.
_HEADER_PREFIX_RE = re.compile( r"^[\divxlcDIVXLC]{1,5}(?:\.\d+)*\.?[\s\.]*" )

_COMPILED_HEADERS = tuple(
	( name , re.compile( pat , re.IGNORECASE ) )
	for name , pat in SECTION_HEADER_PATTERNS
)

# A real figure / table caption first line starts with the word
# "Figure" / "Fig" / "Table" followed by a number. Used to filter
# out YOLO mislabels where body text is class-labeled as a caption.
_FIGURE_CAPTION_RE = re.compile( r"^\s*(figure|figures|fig|figs)\.?\s*\d" , re.IGNORECASE )
_TABLE_CAPTION_RE  = re.compile( r"^\s*table\.?\s*\d" , re.IGNORECASE )

# Patterns we use to recognize reference / bibliography entries when
# no "References" header was detected. We don't require all of these
# to fire ; ANY of them on a block is enough evidence , and we walk
# from the doc tail backwards while the run stays reference-ish.
_REFERENCE_PATTERNS = (
	re.compile( r"\bet\s+al\.?\b"     , re.IGNORECASE ) ,   # author lists
	re.compile( r"\(\s*(?:19|20)\d{2}[a-z]?\s*\)" )       , # (2024) , (1999a)
	re.compile( r"\bdoi[\.:]?\s*10\." , re.IGNORECASE ) ,   # 'doi:10.xxxx'
	re.compile( r"10\.\d{4,}/\S+" )                        , # bare DOI
	re.compile( r"^\s*\[\s*\d+\s*\]" )                     , # [1] -style
	re.compile( r"^\s*\d{1,3}\.\s+[A-Z]" )                 , # 1. Author , ...
)


def _text_for_det( det ):
	"""Pick the best OCR string we have for a detection. Walks the
	engine-priority list and finally any non-empty value."""
	ocr = det.get( "ocr" ) or {}
	for eng in OCR_ENGINE_PRIORITY:
		v = ocr.get( eng )
		if v:
			return v
	for v in ocr.values():
		if v:
			return v
	return ""


def _first_line( text ):
	if not text:
		return ""
	return text.strip().split( "\n" , 1 )[ 0 ].strip()


def _classify_header( text ):
	"""Return one of SECTION_KEYS or None. Strips a leading "1." /
	"III." style prefix before matching the keyword patterns."""
	first = _first_line( text )
	if not first:
		return None
	cleaned = _HEADER_PREFIX_RE.sub( "" , first ).strip()
	if not cleaned:
		return None
	for name , rx in _COMPILED_HEADERS:
		if rx.match( cleaned ):
			return name
	return None


def _looks_like_caption( rx , text ):
	"""True if the first line of `text` matches the caption pattern
	( "Figure 1" / "Table 3" / ... ). Dets with no OCR text default to
	True : their YOLO class is the only signal we have."""
	first = _first_line( text )
	if not first:
		return True
	return bool( rx.match( first ) )


def _looks_like_reference( text ):
	"""True if any of the reference-entry patterns fire on `text`."""
	if not text:
		return False
	for rx in _REFERENCE_PATTERNS:
		if rx.search( text ):
			return True
	return False


def _classify_sections( paper , ordered ):
	"""Bucket every detection into one of SECTION_KEYS using YOLO type ,
	header-keyword anchors , and document position ( for abstract and
	references , whose anchors are often missing in real papers )."""
	pages = ( paper.get( "yolo" ) or {} ).get( "pages" ) or []
	out = { k: [] for k in SECTION_KEYS }

	# --- Pass 1 : route figures / tables ; build the prose flow ;
	# record anchor positions and the paper-title position. -----------
	flow      = []   # [ ( page_idx , det_idx , type , text ) , ... ]
	anchors   = []   # [ ( flow_pos , section_name ) , ... ]
	title_pos = None # flow position of the paper title

	for page_idx , perm in enumerate( ordered ):
		for det_idx in perm:
			try:
				det = pages[ page_idx ][ det_idx ]
			except ( IndexError , TypeError ):
				continue
			if not isinstance( det , dict ):
				continue
			t    = det.get( "type" )
			text = _text_for_det( det )
			pair = [ page_idx , det_idx ]

			# Figures -- route aside unless it's a mis-classified caption.
			if t == "figure":
				out[ "figures" ].append( pair )
				continue
			if t == "figure_caption":
				if _looks_like_caption( _FIGURE_CAPTION_RE , text ):
					out[ "figures" ].append( pair )
					continue
				# Fall through : YOLO mislabel , treat as prose.

			# Tables -- same logic.
			if t == "table":
				out[ "tables" ].append( pair )
				continue
			if t == "table_caption":
				if _looks_like_caption( _TABLE_CAPTION_RE , text ):
					out[ "tables" ].append( pair )
					continue
				# Fall through.
			if t == "table_footnote":
				out[ "tables" ].append( pair )
				continue

			# Add to the prose flow.
			pos = len( flow )
			flow.append( ( page_idx , det_idx , t , text ) )

			# Record anchor / title positions.
			if t == "title":
				kw = _classify_header( text )
				if kw is not None:
					anchors.append( ( pos , kw ) )
				elif title_pos is None:
					title_pos = pos

	N = len( flow )
	if N == 0:
		return out

	# --- Pass 2 : assign every flow position to a bucket. ------------
	buckets = [ None ] * N

	# Anchors first ( the keyword headers themselves ).
	for pos , name in anchors:
		buckets[ pos ] = name
	# Paper title.
	if title_pos is not None and buckets[ title_pos ] is None:
		buckets[ title_pos ] = "title"

	anchors_sorted = sorted( anchors , key=lambda x: x[ 0 ] )

	# Forward-fill between anchors : everything after anchor k and
	# before anchor k+1 inherits anchor k's bucket.
	for k , ( pos , name ) in enumerate( anchors_sorted ):
		next_pos = anchors_sorted[ k + 1 ][ 0 ] if k + 1 < len( anchors_sorted ) else N
		for i in range( pos + 1 , next_pos ):
			if buckets[ i ] is None:
				buckets[ i ] = name

	# Pre-first-anchor positional fallback.
	if anchors_sorted:
		first_pos , first_name = anchors_sorted[ 0 ]
		for i in range( first_pos ):
			if buckets[ i ] is not None:
				continue
			if first_name == "abstract":
				# Real "Abstract" header exists ; pre-abstract content
				# is front matter ( journal banner , authors , affs ).
				buckets[ i ] = "misc"
			elif title_pos is not None and i < title_pos:
				# Before the paper title -- truly pre-content.
				buckets[ i ] = "misc"
			else:
				# Between title and first non-abstract anchor with no
				# abstract header in sight -- this is the structured
				# abstract zone ( JAMA / NEJM style ).
				buckets[ i ] = "abstract"
	else:
		# No anchors at all. Title ( if any ) is title , the rest is
		# misc -- without anchors we have no basis to call anything
		# "abstract" or otherwise.
		for i in range( N ):
			if buckets[ i ] is None:
				buckets[ i ] = "misc"

	# References positional fallback : if no 'references' anchor was
	# found , walk backward from the end while text looks like
	# reference entries ; reassign that contiguous trailing run.
	has_references_anchor = any( name == "references" for _ , name in anchors_sorted )
	if not has_references_anchor and N >= 6:
		ref_start = N
		i = N - 1
		# Allow a few non-reference blocks ( affiliations / copyright
		# strings tucked between citations ) without breaking the run.
		gap_budget = 2
		gap = 0
		while i >= 0:
			_ , _ , t , text = flow[ i ]
			if _looks_like_reference( text ):
				ref_start = i
				gap = 0
			else:
				gap += 1
				if gap > gap_budget:
					break
			i -= 1
		# Require at least 3 detected reference-like blocks to commit.
		ref_like_n = sum(
			1 for j in range( ref_start , N )
			if _looks_like_reference( flow[ j ][ 3 ] )
		)
		if ref_like_n >= 3:
			for j in range( ref_start , N ):
				buckets[ j ] = "references"

	# Push flow positions into the output buckets in order.
	for pos , ( page_idx , det_idx , _ , _ ) in enumerate( flow ):
		name = buckets[ pos ] or "misc"
		out[ name ].append( [ page_idx , det_idx ] )

	return out


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def preprocess_paper( paper ):
	"""Compute reading order + section classification for a paper.
	Returns ( yolo_sorted_page_indexes , sections ) ; does NOT mutate
	`paper`."""
	yolo  = paper.get( "yolo" ) or {}
	pages = yolo.get( "pages" ) or []
	ordered  = [ preprocess_page( page_dets ) for page_dets in pages ]
	sections = _classify_sections( paper , ordered )
	return ordered , sections
