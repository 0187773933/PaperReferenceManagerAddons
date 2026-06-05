"""
src/pdf/methods.py

Extract the METHODS section of a paper as a nicely-formatted plain-text
blob. Reuses the section classification already produced by
src/pdf/preprocess.py ( paper[ 'sections' ][ 'methods' ] is a list of
[ page_idx , det_idx ] pairs into paper[ 'yolo' ][ 'pages' ] ) and the
same per-block cleanup that src/pdf/md.py uses ( junk-line drop ,
hyphenation merge , whitespace collapse ).

Two paths , preferred in order :

  1. OCR / sections ( default ) -- walk paper[ 'sections' ][ 'methods' ]
     in reading order. 'title'-typed blocks become subsection headers
     ( prefixed with "## " so a human can scan them in the Excel cell ) ;
     empty blocks are dropped ; paragraphs are joined with a blank line
     so the cell wraps nicely.

  2. Raw-text fallback -- if the sections bucket is empty but
     paper[ 'raw_text' ] exists , scan the cleaned full-text dump for
     the same "Methods" / "Materials and Methods" / ... header
     patterns preprocess uses , slice from there to the next known
     section header ( results / discussion / conclusion / references /
     ... ) or end-of-document.

Top-level entry point :

  paper_methods( paper ) -> str

  Returns '' when neither path turns up any content. Output ends in
  '\\n' when non-empty so downstream callers can concatenate freely.
"""

import re

from . import md         as MD
from . import preprocess as PP
from . import text       as TEXT


# Section keys that come AFTER methods in a typical paper -- used by the
# raw-text fallback to find where the methods section ends.
_POST_METHODS_KEYS = (
	"results"      ,
	"conclusions"  ,
	"future"       ,
	"references"   ,
)

_HEADER_MAX_CHARS = 80   # a real heading line is short ; body prose is long


def _walk_methods_blocks( paper ):
	"""Yield ( det_type , cleaned_text , page_idx ) for every methods
	detection in reading order. Empty-text blocks are skipped."""
	sections = paper.get( "sections" ) or {}
	items    = sections.get( "methods" ) or []
	pages    = ( paper.get( "yolo" ) or {} ).get( "pages" ) or []
	for page_idx , det_idx in items:
		det = MD._get_det( pages , page_idx , det_idx )
		if det is None:
			continue
		text = MD._text( det )
		if not text:
			continue
		yield det.get( "type" ) , text , page_idx


def _from_sections( paper ):
	"""Build the methods blob from paper[ 'sections' ][ 'methods' ].
	'title' dets become "## Subsection" lines ; everything else is a
	paragraph. Blocks are blank-line separated so Excel wraps nicely."""
	parts = []
	for t , text , _page_idx in _walk_methods_blocks( paper ):
		if t == "title":
			# Skip a leading "Methods" header -- the column itself is
			# the section so repeating the word is just noise. Keep
			# subsection headers ( "Patients" , "Statistical analysis" ,
			# ... ) which are how authors actually divide methods.
			if PP._classify_header( text ) == "methods" and not parts:
				continue
			parts.append( f"## {text}" )
		else:
			parts.append( text )
	if not parts:
		return ""
	return "\n\n".join( parts ).strip() + "\n"


# A line whose preprocess._classify_header lands on a target key. Used by
# the raw-text fallback to find the methods header and the first
# post-methods header.
def _header_key_for_line( line ):
	stripped = line.strip()
	if not ( 0 < len( stripped ) <= _HEADER_MAX_CHARS ):
		return None
	return PP._classify_header( stripped )


def _from_raw_text( paper ):
	"""Fallback : scan paper[ 'raw_text' ] for a methods header and
	slice to the next post-methods header. Cleans the slice with the
	same whole-document pipeline 'prma text --source raw' uses."""
	raw = paper.get( "raw_text" )
	if not raw:
		return ""
	# Clean the full dump first so junk lines / repeated chrome are
	# gone before we slice -- otherwise the headers can be buried by
	# repeating page footers.
	cleaned = TEXT._clean_raw_text( raw )
	if not cleaned:
		return ""
	lines = cleaned.split( "\n" )

	start = None
	for i , line in enumerate( lines ):
		if _header_key_for_line( line ) == "methods":
			start = i
			break
	if start is None:
		return ""

	end = len( lines )
	for j in range( start + 1 , len( lines ) ):
		key = _header_key_for_line( lines[ j ] )
		if key in _POST_METHODS_KEYS:
			end = j
			break

	# Drop the methods header line itself ; emit the rest as-is. The
	# cleanup pass already reconstructed paragraphs.
	body = "\n".join( lines[ start + 1 : end ] ).strip()
	if not body:
		return ""
	return body + "\n"


def paper_methods( paper ):
	"""Return the methods section of a paper as a plain-text blob.
	Prefers the sections classification ; falls back to scanning the
	raw_text dump when the sections bucket is empty. Returns '' when
	neither path has content."""
	out = _from_sections( paper )
	if out:
		return out
	return _from_raw_text( paper )
