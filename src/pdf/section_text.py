"""
src/pdf/section_text.py

Resolve the plain-text body of ONE section ( abstract , methods ,
results , ... ) of one paper. Two-tier lookup :

  1. Per-section task output ( fast path ) -- if the user has already
     run ` prma {section} ` ( currently only 'methods' has its own
     extractor task , but the pattern generalizes ) , the file at
       args.output / {section} / { doi_to_filename }.txt
     is the authoritative , already-cleaned section text. Use it.

  2. Markdown fallback -- otherwise read
       args.output / md / { doi_to_filename }.md
     and slice out the "## {SectionDisplay}" block. The slice runs
     from the line AFTER the matching ## header to the next ## header
     ( or end-of-document ) and is stripped of leading / trailing
     whitespace.

Top-level entry point :

  resolve_section_text( args , doi , section_key ) -> str

  Returns the section's text , or '' when neither source has anything.
  `section_key` is one of the keys from src/pdf/md.SECTION_ORDER
  ( 'abstract' , 'introduction' , ... , 'misc' , 'references' ).

  list_sections() -> tuple[ ( key , display ) , ... ]

  The summarizable subset of SECTION_ORDER -- everything except 'figures'
  / 'tables' / 'misc' / 'references' which either aren't prose or aren't
  worth summarizing.
"""

import re

from . import md as MD
from ..utils import utils


# Sections we'll let users run ` prma summarize ` on. Drops the four
# buckets that aren't useful as a free-text summary :
#   - figures   : just captions and placeholder image links ;
#   - tables    : same ;
#   - misc      : journal banner / authors / affiliations chrome ;
#   - references: a bibliography is its own beast , summarize separately.
_SUMMARIZABLE = {
	"abstract"     , "introduction" , "background" , "methods"    ,
	"results"      , "conclusions"  , "future"     ,
}


def list_sections():
	"""Return SECTION_ORDER filtered to summarizable sections , in the
	canonical paper order."""
	return tuple(
		( key , display ) for key , display in MD.SECTION_ORDER
		if key in _SUMMARIZABLE
	)


def _section_display_for( section_key ):
	"""Look up the human-readable header used in the rendered Markdown
	( e.g. 'methods' -> 'Methods' , 'future' -> 'Future Work' )."""
	for key , display in MD.SECTION_ORDER:
		if key == section_key:
			return display
	# Fallback : title-case the key.
	return section_key.title()


def _read_per_section_txt( args , doi , section_key ):
	"""Read args.output/{section_key}/{doi_to_filename}.txt if it exists.
	Returns the text or ''."""
	prefix = utils.doi_to_filename( doi )
	if not prefix:
		return ""
	p = args.output.joinpath( section_key , f"{prefix}.txt" )
	if not p.exists():
		return ""
	try:
		return p.read_text( encoding="utf-8" ).strip()
	except Exception:
		return ""


def _read_md( args , doi ):
	"""Read args.output/md/{doi_to_filename}.md if it exists."""
	prefix = utils.doi_to_filename( doi )
	if not prefix:
		return ""
	p = args.output.joinpath( "md" , f"{prefix}.md" )
	if not p.exists():
		return ""
	try:
		return p.read_text( encoding="utf-8" )
	except Exception:
		return ""


# Match the start of a Markdown H2 ( "## " at column 0 ).
_H2_RE = re.compile( r"^##\s+(.+?)\s*$" , re.MULTILINE )


def _extract_section_from_md( md_text , section_display ):
	"""Slice the body of `## {section_display}` out of a rendered md doc.
	Returns '' when the header isn't present. The match is case-insensitive
	so 'Methods' / 'methods' / 'METHODS' all hit."""
	if not md_text:
		return ""
	target = section_display.strip().lower()
	# Walk every H2 ; remember the start of the target body and the
	# end-of-section ( = start of the next H2 or end of document ).
	start , end = None , len( md_text )
	for m in _H2_RE.finditer( md_text ):
		header = m.group( 1 ).strip().lower()
		if start is None:
			if header == target:
				start = m.end()
		else:
			# We're past the target ; the first next H2 ends the slice.
			end = m.start()
			break
	if start is None:
		return ""
	return md_text[ start : end ].strip()


def resolve_section_text( args , doi , section_key ):
	"""Best available text for one ( paper , section ) pair.
	Per-section .txt takes priority over slicing the md."""
	# Fast path : per-section task output.
	txt = _read_per_section_txt( args , doi , section_key )
	if txt:
		return txt
	# Fallback : slice the rendered Markdown.
	md_text = _read_md( args , doi )
	if not md_text:
		return ""
	display = _section_display_for( section_key )
	return _extract_section_from_md( md_text , display )
