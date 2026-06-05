"""
src/pdf/text.py

Render a paper record into a PLAIN-TEXT document. Two sources :

  source='raw' ( default ) -- start from paper[ 'raw_text' ] ( the
    pymupdf full-text dump produced during preprocess ) , clean it ,
    and emit. Cleanup ( see _clean_raw_text ) drops boilerplate
    footer / copyright lines , drops short running headers / footers
    that repeat across pages , merges end-of-line hyphenations ,
    reconstructs paragraphs ( so intra-paragraph line wraps become
    spaces while real paragraph breaks survive ) , and runs a
    wordninja safety pass.

  source='ocr'             -- walk the yolo + ocr 'sections'
    classification ( same content used by ` prma md ` ) and emit
    each section's blocks as plain paragraphs , with uppercase
    underlined section headers. No Markdown syntax , no image
    links. Useful when the PDF text layer is missing / garbled and
    OCR is the only good source. Per-block cleanup is already done
    by MD._text -- no extra whole-document pass needed.

Top-level entry point :

  paper_to_text( paper , source='raw' , include_references=False )

  Returns the full text document as a string ending in '\\n'.
  Empty string if the chosen source has no content for this paper.
  `include_references` defaults False -- the references / bibliography
  section is dropped from the output. For source='ocr' that's just
  skipping the 'references' bucket ; for source='raw' we detect the
  References section in the cleaned full-text dump using the same
  signals as preprocess ( a short "References" / "REFERENCES" /
  "Bibliography" header line , or failing that , a contiguous
  trailing run of reference-pattern text ) and truncate there.
"""

import re
from collections import Counter

from . import md         as MD
from . import ocr        as OCR
from . import preprocess as PP


# ---------------------------------------------------------------------------
# Raw-text whole-document cleanup
# ---------------------------------------------------------------------------

# A line that appears at least this many times across the raw_text
# dump is almost certainly a running header / footer / journal banner /
# page-number repetition -- drop it. ( We don't cap line length any
# more : publisher footers like Wiley's "Downloaded from ... See the
# Terms and Conditions ... on Wiley Online Library for rules of use ..."
# can run hundreds of chars per line and need to be caught too. )
_REPEAT_MIN_OCCURRENCES = 3

# Sentence-final punctuation followed by uppercase start on the next
# line is our paragraph-break signal.
_SENTENCE_FINAL_RE = re.compile( r"[.!?][\"\'\)\]]?\s*$" )

# Letter-spaced banner text like "R E S E A R C H  A R T I C L E"
# or "O R I G I N A L  I N V E S T I G A T I O N" -- collapse the
# spaces. We require 5+ consecutive single-letter tokens so we don't
# munge things like "I am O K with that".
_LETTER_SPACED_RE = re.compile( r"(?:\b[A-Za-z]\s){4,}[A-Za-z]\b" )

# Some PDFs encode hyperlink annotations as bracket-paren markdown
# syntax in their text layer ( e.g. Wiley's
# "[https://publo](https://publo) ns.com/..." ). Strip the (url) part ,
# keep the text in the brackets.
_MD_LINK_IN_TEXT_RE = re.compile( r"\[([^\[\]\n]+)\]\([^)\n]+\)" )

# A URL split at a line wrap inside the host portion : the OCR / text
# layer reads it as "https://publo" + space + "ns.com/path" . If the
# continuation looks like a domain ( has a dot + tld ) we rejoin.
_URL_SPLIT_RE = re.compile(
	r"(https?://[a-z0-9.\-]+)\s+([a-z0-9.\-]+\.[a-z]{2,}\S*)" ,
	re.IGNORECASE
)


def _drop_junk_lines( lines ):
	"""Filter the same boilerplate patterns as the markdown cleanup
	( "Downloaded from..." , "© 20xx ..." , "All rights reserved" ,
	bare URL lines , etc. )."""
	keep = []
	for line in lines:
		stripped = line.strip()
		if any( rx.match( stripped ) for rx in MD._JUNK_LINE_PATTERNS ):
			continue
		keep.append( line )
	return keep


def _drop_repeated_lines( lines ):
	"""Drop lines that repeat across the document. pymupdf returns
	every page's running header / footer / page-number verbatim , so
	we see them once per page. If the same line appears N+ times ,
	it's chrome , not content -- regardless of length ( publisher
	footers can wrap to long lines that still repeat per page )."""
	counts = Counter( l.strip() for l in lines if l.strip() )
	drop = { s for s , c in counts.items() if c >= _REPEAT_MIN_OCCURRENCES }
	if not drop:
		return lines
	return [ l for l in lines if l.strip() not in drop ]


def _collapse_letter_spaced( text ):
	"""Collapse 'R E S E A R C H A R T I C L E' -> 'RESEARCH ARTICLE'.
	Joins the spaced letters into one token , then if the result is
	all-caps and long enough , uses wordninja on a lowercased copy
	to recover word boundaries , then restores the uppercase ( so
	"RESEARCHARTICLE" -> "RESEARCH ARTICLE" , not "researcharticle" )."""
	wn = None
	try:
		import wordninja
		wn = wordninja
	except Exception:
		pass

	def _join( m ):
		joined = m.group( 0 ).replace( " " , "" )
		if wn is None or not joined.isalpha() or len( joined ) < 6:
			return joined
		try:
			parts = wn.split( joined.lower() )
		except Exception:
			return joined
		# Only commit to the split if every piece is a "real" word
		# ( >= 2 chars ; wordninja sometimes spits out single letters
		# for tokens it can't resolve , and we'd rather keep the
		# stuck form than introduce garbage ).
		if len( parts ) < 2 or any( len( p ) < 2 for p in parts ):
			return joined
		if joined.isupper():
			return " ".join( p.upper() for p in parts )
		return " ".join( parts )

	return _LETTER_SPACED_RE.sub( _join , text )


def _strip_inline_md_links( text ):
	"""Strip `[label](url)` syntax that some PDFs emit in their text
	layer when they have hyperlink annotations. Keeps the label ;
	drops the url. After this , a URL-split-mid-host residue like
	'https://publo ns.com/path' may remain -- _repair_split_urls
	stitches that back together."""
	return _MD_LINK_IN_TEXT_RE.sub( r"\1" , text )


def _repair_split_urls( text ):
	"""Rejoin URLs that broke at a line wrap inside the host
	( 'https://publo ns.com/...' -> 'https://publons.com/...' ).
	Requires the continuation to look like a domain ( contains a
	dot + 2+-char TLD ) so we don't accidentally join 'see https://x
	more info'."""
	# Re-run until stable -- a URL may have multiple internal splits.
	prev = None
	for _ in range( 4 ):
		new = _URL_SPLIT_RE.sub( r"\1\2" , text )
		if new == prev:
			break
		prev , text = text , new
	return text


def _reconstruct_paragraphs( text ):
	"""pymupdf returns each visual line as its own '\\n'-terminated
	line , so a paragraph that wraps over six lines comes out as
	six separate lines. Collapse those wraps to spaces ; recognize a
	real paragraph boundary when the previous line ends with sentence
	-final punctuation AND the next starts with uppercase ( or there's
	a blank line in between )."""
	out = []                         # accumulated text fragments
	pending_break = False            # blank line seen -> emit \n\n
	                                  # before the next non-empty line
	for raw in text.split( "\n" ):
		s = raw.strip()
		if not s:
			pending_break = True
			continue
		if not out:
			out.append( s )
			pending_break = False
			continue
		if pending_break:
			out.append( "\n\n" )
			out.append( s )
			pending_break = False
			continue
		prev_tail = out[ -1 ]
		if (
			_SENTENCE_FINAL_RE.search( prev_tail )
			and s[ 0 ].isupper()
		):
			out.append( "\n\n" )
			out.append( s )
		else:
			out.append( " " )
			out.append( s )
	return "".join( out )


# A "References" header line on its own page is almost always SHORT
# ( just the word "References" / "REFERENCES" / "Bibliography" , maybe
# with a "1." prefix ). We refuse to fire on long lines so we don't
# falsely cut on body text that happens to start with "References to..."
_REFERENCES_HEADER_MAX_CHARS = 30


def _strip_references_from_raw( text ):
	"""Cut everything from the References section onward in a pymupdf
	raw_text dump. Uses two signals , preferring the first :
	  1. An explicit short header line whose text classifies as
	     'references' via preprocess._classify_header ( same patterns
	     preprocess uses for yolo titles ) ;
	  2. A contiguous trailing run of reference-pattern lines
	     ( preprocess._looks_like_reference catches DOIs , "et al." ,
	     "(2024)" , "[1]" prefixes , and "12. Author , ..." -style
	     numbered entries ).
	Returns the input unchanged if neither signal fires."""
	if not text:
		return text
	lines = text.split( "\n" )
	n = len( lines )

	# 1) Explicit short header line.
	for i , line in enumerate( lines ):
		stripped = line.strip()
		if not ( 0 < len( stripped ) <= _REFERENCES_HEADER_MAX_CHARS ):
			continue
		if PP._classify_header( stripped ) == "references":
			return "\n".join( lines[ :i ] )

	# 2) Positional fallback : walk backward from the end while the
	# run stays reference-ish. Allow a small gap so trailing pymupdf
	# chrome between citations doesn't break the run.
	if n < 6:
		return text
	ref_start = n
	gap , gap_budget = 0 , 3
	for i in range( n - 1 , -1 , -1 ):
		s = lines[ i ].strip()
		if not s:
			continue
		if PP._looks_like_reference( s ):
			ref_start = i
			gap = 0
		else:
			gap += 1
			if gap > gap_budget:
				break
	ref_like_n = sum(
		1 for j in range( ref_start , n )
		if PP._looks_like_reference( lines[ j ].strip() )
	)
	if ref_like_n >= 3:
		return "\n".join( lines[ :ref_start ] )

	return text


def _clean_raw_text( text ):
	"""Whole-document cleanup for a pymupdf raw_text dump. Returns
	'' for empty input. See module docstring for the pipeline."""
	if not text:
		return ""

	# 1. Strip bracket-paren markdown-link syntax that some PDFs emit
	# in their text layer ( hyperlink annotations ) and repair URLs
	# that pymupdf broke at line wraps.
	text = _strip_inline_md_links( text )
	text = _repair_split_urls( text )

	# 2. Collapse letter-spaced banner text ( "R E S E A R C H ..." ).
	# Wordninja later will re-split the result if it can.
	text = _collapse_letter_spaced( text )

	# 3. Drop boilerplate junk lines and repeated running headers/footers.
	lines = text.split( "\n" )
	lines = _drop_junk_lines( lines )
	lines = _drop_repeated_lines( lines )
	text  = "\n".join( lines )

	# 4. Merge end-of-line hyphenations BEFORE we collapse \n into
	# spaces ( otherwise we'd lose the hyphen-newline signal ).
	text = re.sub( r"(\w)-\n(\w)" , r"\1\2" , text )

	# 5. Reconstruct paragraphs ( collapse line-wrap \n to spaces ,
	# preserve real paragraph breaks ).
	text = _reconstruct_paragraphs( text )

	# 6. wordninja safety pass : pymupdf's embedded text usually has
	# correct spacing , but the OCR module's _split_stuck_words also
	# splits collapsed-letter-spaced cases like "RESEARCHARTICLE" ->
	# "RESEARCH ARTICLE" when wordninja's dictionary knows the words.
	try:
		text = OCR._split_stuck_words( text )
	except Exception:
		pass

	# 7. Final whitespace cleanup.
	text = re.sub( r"[ \t]+" , " "    , text )
	text = re.sub( r"\n{3,}" , "\n\n" , text )
	return text.strip() + "\n"


# ---------------------------------------------------------------------------
# Source = 'raw'
# ---------------------------------------------------------------------------

def _from_raw( paper , include_references=False ):
	"""Header + cleaned raw_text ( pymupdf full-text dump ). When
	include_references is False ( default ) , the References section
	is detected and stripped BEFORE cleanup."""
	raw = paper.get( "raw_text" )
	if not raw:
		return ""
	if not include_references:
		raw = _strip_references_from_raw( raw )
	cleaned = _clean_raw_text( raw )
	if not cleaned:
		return ""
	pages = ( paper.get( "yolo" ) or {} ).get( "pages" ) or []
	doi   = paper.get( "doi" )
	title = MD._resolve_title( paper , pages )

	header_lines = [ title ]
	if doi:
		header_lines.append( f"DOI: https://doi.org/{doi}" )
	header_lines.append( "" )
	return "\n".join( header_lines ) + "\n" + cleaned


# ---------------------------------------------------------------------------
# Source = 'ocr'
# ---------------------------------------------------------------------------

def _heading( title , char ):
	"""Emit a heading line followed by an underline made of `char`."""
	return [ title , char * max( 1 , len( title ) ) , "" ]


def _render_prose_text( pages , items , lines ):
	for page_idx , det_idx in items:
		det = MD._get_det( pages , page_idx , det_idx )
		if det is None:
			continue
		text = MD._text( det )
		if not text:
			continue
		# Subsection-header dets just emit the heading line ; both
		# subsection headers and paragraphs are separated from the
		# next block by a blank line below.
		lines.append( text )
		lines.append( "" )


def _render_figures_or_tables_text( pages , items , lines ):
	for page_idx , det_idx in items:
		det = MD._get_det( pages , page_idx , det_idx )
		if det is None:
			continue
		t    = det.get( "type" )
		text = MD._text( det )
		if t in ( "figure" , "table" ):
			label = "Figure" if t == "figure" else "Table"
			lines.append( f"[ {label} on page {page_idx + 1} ]" )
			lines.append( "" )
			continue
		if text:
			lines.append( text )
			lines.append( "" )


def _render_references_text( pages , items , lines ):
	n = 0
	for page_idx , det_idx in items:
		det = MD._get_det( pages , page_idx , det_idx )
		if det is None:
			continue
		text = MD._text( det )
		if not text:
			continue
		if det.get( "type" ) == "title":
			# The 'References' heading itself -- the underlined H2 above
			# already covers it.
			continue
		n += 1
		if MD._PRE_NUMBERED_REF_RE.match( text ):
			lines.append( text )
		else:
			lines.append( f"{n}. {text}" )
		lines.append( "" )


def _from_ocr( paper , include_references=False ):
	"""Walk paper[ 'sections' ] and emit a plain-text document with
	uppercase , underlined section headers. When include_references
	is False ( default ) , the 'references' bucket is skipped entirely
	( no header , no entries )."""
	sections = paper.get( "sections" ) or {}
	pages    = ( paper.get( "yolo" ) or {} ).get( "pages" ) or []
	doi      = paper.get( "doi" )
	title    = MD._resolve_title( paper , pages )

	lines = []
	# Title : =====-underlined.
	lines.extend( _heading( title , "=" ) )
	if doi:
		lines.append( f"DOI: https://doi.org/{doi}" )
		lines.append( "" )

	for key , display in MD.SECTION_ORDER:
		if key == "references" and not include_references:
			continue
		items = sections.get( key ) or []
		if not items:
			continue
		# Section header : UPPERCASE , dash-underlined.
		lines.extend( _heading( display.upper() , "-" ) )
		if key == "references":
			_render_references_text( pages , items , lines )
		elif key in ( "figures" , "tables" ):
			_render_figures_or_tables_text( pages , items , lines )
		else:
			_render_prose_text( pages , items , lines )

	while lines and lines[ -1 ] == "":
		lines.pop()
	if not lines:
		return ""
	return "\n".join( lines ) + "\n"


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def paper_to_text( paper , source="raw" , include_references=False ):
	"""Render a paper as plain text. `source` is 'raw' ( pymupdf
	full-text dump , default ) or 'ocr' ( walk yolo+ocr sections ).
	Returns '' if the requested source has no content.
	`include_references` defaults False -- the References section is
	stripped ( from the cleaned full text for 'raw' , from the
	'references' bucket for 'ocr' )."""
	if source == "ocr":
		return _from_ocr( paper , include_references=include_references )
	return _from_raw( paper , include_references=include_references )
