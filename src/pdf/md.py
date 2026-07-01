"""
src/pdf/md.py

Render a paper record into a Markdown document using its 'sections'
classification ( produced by src/pdf/preprocess.py ).

This module does ONE thing : turn a paper dict into a Markdown string.
It does NOT touch the filesystem , re-run any pipeline stage , or
mutate the input -- the orchestrating task ( src/tasks/md.py ) handles
file I/O and pre-flight requirements.

Document shape :

  # { paper title }

  **DOI:** [ 10.xxxx/yyy ]( https://doi.org/... )

  ## Source Code     ( bulleted unique links from ` prma code ` ; omitted if none )
  ## Abstract
  ## Introduction
  ## Background
  ## Methods
  ## Results
  ## Conclusions
  ## Future Work
  ## Figures
  ## Tables
  ## Other          ( the 'misc' bucket )
  ## References

  Sections with no detections are omitted entirely ( we don't emit
  empty H2s ).

Within a prose section :
  - 'title'-type detections become '### Subsection' headers ;
  - 'plain text' becomes a paragraph ;
  - empty-OCR detections are dropped.

Within Figures / Tables :
  - 'figure_caption' / 'table_caption' rendered as **bold** captions ;
  - 'figure' / 'table' becomes an image link if the corresponding
    PNG from ` prma images ` exists on disk ( relative path from the
    .md file ) ; otherwise an italic placeholder citing the page ;
  - 'table_footnote' rendered as italic text.

Within References :
  - Each entry is numbered ( 1. 2. 3. ... ) UNLESS the OCR text
    already starts with a "[N]" or "N." prefix , in which case it's
    emitted verbatim.
  - 'title'-type entries ( the 'References' heading itself ) are
    skipped -- the H2 already covers it.

Text cleanup ( applied to every block before emission ) :
  - drops boilerplate footer / copyright lines that YOLO sometimes
    misses tagging as 'abandon' ( "Downloaded from..." ,
    "© 2024 ..." , "All rights reserved" , etc. ) ;
  - merges end-of-line hyphenations ( "endo-\\nvascular" →
    "endovascular" ) ;
  - collapses single newlines inside a paragraph to a space while
    preserving real paragraph breaks ( "\\n\\n" ) ;
  - collapses runs of spaces / tabs.

Top-level entry point :

  paper_to_markdown( paper , image_rel_dir=None , image_abs_dir=None ,
                     prefix=None , include_references=False )

  Returns the full Markdown document as a string ending in '\\n'.
  Pass `image_rel_dir` ( relative path , e.g. "../images/ALL" ) ,
  `image_abs_dir` ( Path for existence checks ) , and `prefix`
  ( doi_to_filename ) to inline figure / table images that
  ` prma images ` wrote ; if any of these are None or the file
  doesn't exist , the figure / table falls back to a text placeholder.
  `include_references` defaults False -- the 'References' section is
  omitted entirely. Pass True to render it as a numbered list.
"""

import re

from . import preprocess as PP


# ---------------------------------------------------------------------------
# Text cleanup
# ---------------------------------------------------------------------------

# Page-bottom / page-top boilerplate that YOLO sometimes labels as
# 'plain text' instead of 'abandon' , so it leaks through into the
# reading-order flow. Anchored to the START of a line so we don't
# accidentally trim legitimate prose that mentions these words.
_JUNK_LINE_PATTERNS = (
	re.compile( r"^\s*downloaded\s+from\b"               , re.IGNORECASE ) ,
	re.compile( r"^\s*©\s*\d{4}"                                          ) ,
	re.compile( r"^\s*copyright\s+©"                     , re.IGNORECASE ) ,
	re.compile( r"^\s*all\s+rights\s+reserved\.?\s*$"    , re.IGNORECASE ) ,
	re.compile( r"^\s*reprinted\s+with\s+permission\b"   , re.IGNORECASE ) ,
	re.compile( r"^\s*see\s+(?:also\s+)?http"            , re.IGNORECASE ) ,
	# Standalone bare URL or "domain.com" lines ( running header /
	# DOI breadcrumb on every page ).
	re.compile( r"^\s*https?://\S+\s*$"                  , re.IGNORECASE ) ,
	re.compile( r"^\s*www\.[a-z0-9.\-]+(?:/\S*)?\s*$"    , re.IGNORECASE ) ,
	# Publisher footers / licensing chrome that wraps onto many lines.
	# Anchor to start so a body paragraph mentioning "the terms and
	# conditions of this study" doesn't get nuked.
	re.compile( r"^\s*see\s+the\s+terms\s+and\s+conditions\b" , re.IGNORECASE ) ,
	re.compile( r"^\s*terms\s+and\s+conditions\s+\("           , re.IGNORECASE ) ,
	re.compile( r"^\s*on\s+wiley\s+online\s+library\b"         , re.IGNORECASE ) ,
	re.compile( r"^\s*oa\s+articles?\s+are\s+governed\b"       , re.IGNORECASE ) ,
	re.compile( r"^\s*(?:are\s+)?governed\s+by\s+the\s+applicable\s+creative\s+commons" , re.IGNORECASE ) ,
	re.compile( r"^\s*creative\s+commons\s+(?:license|attribution)" , re.IGNORECASE ) ,
	re.compile( r"^\s*this\s+article\s+is\s+(?:protected|licensed)\s+(?:by|under)\b" , re.IGNORECASE ) ,
	re.compile( r"^\s*for\s+rules?\s+of\s+use\s*;?"            , re.IGNORECASE ) ,
	re.compile( r"^\s*(?:re)?printed\s+from\b"                 , re.IGNORECASE ) ,
)


def _clean_text( text ):
	"""Prepare an OCR text blob for inline insertion into Markdown.
	Returns '' for empty / all-junk input."""
	if not text:
		return ""

	# Drop junk lines.
	kept = []
	for line in text.split( "\n" ):
		stripped = line.strip()
		if any( rx.match( stripped ) for rx in _JUNK_LINE_PATTERNS ):
			continue
		kept.append( line )
	text = "\n".join( kept )

	# Merge end-of-line hyphenations BEFORE collapsing newlines.
	# Soft hyphen + non-breaking hyphen were already normalized by
	# src/pdf/ocr._normalize_text ; this handles the regular '-\n'.
	text = re.sub( r"(\w)-\s*\n\s*(\w)" , r"\1\2" , text )

	# Preserve paragraph breaks ; collapse single newlines to spaces.
	text = re.sub( r"\n{2,}" , "\x00" , text )
	text = re.sub( r"\n"     , " "    , text )
	text = text.replace( "\x00" , "\n\n" )

	# Collapse runs of spaces / tabs.
	text = re.sub( r"[ \t]+" , " " , text )
	return text.strip()


# ---------------------------------------------------------------------------
# Image-index : map figure / table dets to their PNG sequence number
# ---------------------------------------------------------------------------

def _build_image_index( pages ):
	"""Walk yolo pages in original order and assign 1-indexed sequence
	numbers to every `figure` and `table` detection -- matching the
	numbering that src/pdf/images.extract() uses for its filenames .
	Returns ( figure_index , table_index ) , each a dict :
	  ( page_idx , det_idx ) -> N
	so the renderer can look up the right ` {prefix}-figure-{N}.png ` ."""
	figure_idx , table_idx = {} , {}
	fn , tn = 0 , 0
	for page_idx , page_dets in enumerate( pages or [] ):
		for det_idx , det in enumerate( page_dets or [] ):
			if not isinstance( det , dict ):
				continue
			t = det.get( "type" )
			if t == "figure":
				fn += 1
				figure_idx[ ( page_idx , det_idx ) ] = fn
			elif t == "table":
				tn += 1
				table_idx[ ( page_idx , det_idx ) ] = tn
	return figure_idx , table_idx


# Order to emit sections in. 'title' is rendered separately as the H1
# at the top , so it isn't in this list.
SECTION_ORDER = (
	( "abstract"     , "Abstract"     ) ,
	( "introduction" , "Introduction" ) ,
	( "background"   , "Background"   ) ,
	( "methods"      , "Methods"      ) ,
	( "results"      , "Results"      ) ,
	( "conclusions"  , "Conclusions"  ) ,
	( "future"       , "Future Work"  ) ,
	( "figures"      , "Figures"      ) ,
	( "tables"       , "Tables"       ) ,
	( "misc"         , "Other"        ) ,
	( "references"   , "References"   ) ,
)

# Matches lines that already begin with a reference index like "[12]"
# or "12." so we don't double-number them.
_PRE_NUMBERED_REF_RE = re.compile( r"^\s*(?:\[\s*\d+\s*\]|\d{1,3}\.)\s" )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_det( pages , page_idx , det_idx ):
	try:
		return pages[ page_idx ][ det_idx ]
	except ( IndexError , TypeError ):
		return None


def _text( det ):
	"""Return the cleaned OCR text for a detection -- junk lines
	dropped , hyphenations merged , single newlines collapsed."""
	raw = ( PP._text_for_det( det ) if det else "" ).strip()
	return _clean_text( raw )


def _resolve_title( paper , pages ):
	"""Title resolution order :
	  1. paper[ 'title' ] from the reference-manager metadata
	     ( authoritative ) ;
	  2. OCR text of the first det in sections[ 'title' ]
	     ( fallback when the manager didn't store a title ) ;
	  3. '(untitled)' ."""
	t = ( paper.get( "title" ) or "" ).strip()
	if t:
		return t
	title_items = ( paper.get( "sections" ) or {} ).get( "title" ) or []
	for page_idx , det_idx in title_items:
		txt = _text( _get_det( pages , page_idx , det_idx ) )
		if txt:
			# Collapse any remaining whitespace so the H1 stays single-line.
			return re.sub( r"\s+" , " " , txt ).strip()
	return "(untitled)"


def _image_link( kind , page_idx , det_idx , idx_map , prefix ,
                 image_rel_dir , image_abs_dir ):
	"""Return ( link_path , label ) if a matching image exists , else
	( None , None ). `kind` is 'figure' or 'table'."""
	if not ( prefix and image_rel_dir is not None and image_abs_dir is not None ):
		return None , None
	seq = idx_map.get( ( page_idx , det_idx ) )
	if seq is None:
		return None , None
	fname = f"{prefix}-{kind}-{seq}.png"
	if not ( image_abs_dir / fname ).exists():
		return None , None
	# image_rel_dir is a string ; join with / for portable Markdown.
	rel = f"{image_rel_dir.rstrip('/')}/{fname}"
	label = f"{kind.capitalize()} {seq}"
	return rel , label


# ---------------------------------------------------------------------------
# Per-section renderers
# ---------------------------------------------------------------------------

def _render_prose( pages , items , lines ):
	for page_idx , det_idx in items:
		det = _get_det( pages , page_idx , det_idx )
		if det is None:
			continue
		text = _text( det )
		if not text:
			continue
		if det.get( "type" ) == "title":
			lines.append( f"### {text}" )
		else:
			lines.append( text )
		lines.append( "" )


def _render_figures_or_tables( pages , items , lines , kind ,
                               figure_idx , table_idx ,
                               prefix , image_rel_dir , image_abs_dir ):
	"""`kind` is 'figure' or 'table'. Emits an image link
	( ![label](rel/path) ) when an extracted PNG exists ; otherwise an
	italic placeholder. Captions / footnotes are rendered verbatim."""
	for page_idx , det_idx in items:
		det = _get_det( pages , page_idx , det_idx )
		if det is None:
			continue
		t    = det.get( "type" )
		text = _text( det )
		if t in ( "figure" , "table" ):
			idx_map = figure_idx if t == "figure" else table_idx
			rel , label = _image_link(
				t , page_idx , det_idx , idx_map , prefix ,
				image_rel_dir , image_abs_dir ,
			)
			if rel is not None:
				lines.append( f"![{label}]({rel})" )
			else:
				placeholder = "Figure" if t == "figure" else "Table"
				lines.append( f"*[ {placeholder} on page {page_idx + 1} ]*" )
			lines.append( "" )
			continue
		if t in ( "figure_caption" , "table_caption" ):
			if text:
				lines.append( f"**{text}**" )
				lines.append( "" )
			continue
		if t == "table_footnote":
			if text:
				lines.append( f"_{text}_" )
				lines.append( "" )
			continue
		# Anything else that landed here ( shouldn't be common ).
		if text:
			lines.append( text )
			lines.append( "" )


def _render_source_code( links , lines ):
	"""Emit a '## Source Code' section : a bulleted list of the UNIQUE
	source-code / data links ` prma code ` harvested for this paper
	( paper[ 'code' ].links -- already host-classified and deduped ). We
	de-dupe once more here ( belt + suspenders ) and skip the section
	entirely when there are no links , so papers without code don't get an
	empty header."""
	seen , bullets = set() , []
	for l in links or []:
		# Prefer the OCR-repaired URL ` prma code ` pinned , if any.
		url = ( ( l or {} ).get( "resolved_url" ) or ( l or {} ).get( "url" ) or "" ).strip()
		if not url:
			continue
		k = url.lower().rstrip( "/" )
		if k in seen:
			continue
		seen.add( k )
		bullets.append( f"- [{url}]({url})" )
	if not bullets:
		return
	lines.append( "## Source Code" )
	lines.append( "" )
	lines.extend( bullets )
	lines.append( "" )


def _render_references( pages , items , lines ):
	n = 0
	for page_idx , det_idx in items:
		det = _get_det( pages , page_idx , det_idx )
		if det is None:
			continue
		text = _text( det )
		if not text:
			continue
		if det.get( "type" ) == "title":
			# The 'References' heading itself -- H2 already covers it.
			continue
		n += 1
		if _PRE_NUMBERED_REF_RE.match( text ):
			lines.append( text )
		else:
			lines.append( f"{n}. {text}" )
		lines.append( "" )


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def paper_to_markdown( paper , image_rel_dir=None , image_abs_dir=None ,
                       prefix=None , include_references=False ):
	"""Render a paper record into a Markdown document string. Requires
	paper[ 'sections' ] ( from preprocess ) ; returns a single string
	ending in a newline.

	If `image_rel_dir` , `image_abs_dir` , and `prefix` are provided
	AND a matching ` {prefix}-figure-N.png ` exists in
	image_abs_dir , figure / table detections render as Markdown
	image links ( href = image_rel_dir/filename ) instead of italic
	placeholders. Pass them as None to always use placeholders.

	`include_references` defaults False : the 'References' section is
	omitted from the output entirely ( no H2 , no entries )."""
	sections = paper.get( "sections" ) or {}
	pages    = ( paper.get( "yolo" ) or {} ).get( "pages" ) or []
	doi      = paper.get( "doi" )
	title    = _resolve_title( paper , pages )
	figure_idx , table_idx = _build_image_index( pages )

	lines = []
	lines.append( f"# {title}" )
	lines.append( "" )
	if doi:
		lines.append( f"**DOI:** [{doi}](https://doi.org/{doi})" )
		lines.append( "" )

	# Source-code / data links ( prma code runs as the stage right before md ) ,
	# rendered up top as a bulleted list of unique links so they're prominent.
	_render_source_code( ( ( paper.get( "code" ) or {} ).get( "links" ) ) or [] , lines )

	for key , display in SECTION_ORDER:
		if key == "references" and not include_references:
			continue
		items = sections.get( key ) or []
		if not items:
			continue
		lines.append( f"## {display}" )
		lines.append( "" )
		if key == "references":
			_render_references( pages , items , lines )
		elif key == "figures":
			_render_figures_or_tables(
				pages , items , lines , "figure" ,
				figure_idx , table_idx ,
				prefix , image_rel_dir , image_abs_dir ,
			)
		elif key == "tables":
			_render_figures_or_tables(
				pages , items , lines , "table" ,
				figure_idx , table_idx ,
				prefix , image_rel_dir , image_abs_dir ,
			)
		else:
			_render_prose( pages , items , lines )

	# Trim trailing blank lines so files end with exactly one newline.
	while lines and lines[ -1 ] == "":
		lines.pop()
	return "\n".join( lines ) + "\n"
