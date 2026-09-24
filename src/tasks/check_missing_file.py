"""
` prma check-missing-file ` -- which papers in a research LOG aren't in the
library yet.

The log is the running Markdown notebook you keep while reading : dated
headings , and under each one a run of ` &nbsp; `-separated blocks where a
block is a paper's TITLE on one line and its LINK(S) on the next :

  # 03SEP2026

  Naturalistic auditory semantic mapping using high-density diffuse optical tomography

  [https://www-spiedigitallibrary-org...](https://www-spiedigitallibrary-org...)

  &nbsp;

Every such block is one paper. Its DOI is lifted out of the link when the
link carries one ( doi.org , publisher article routes , bioRxiv , ezproxy'd
copies of all of those ) , after the URL adornments a browser glues on --
` .full ` / ` .abstract ` / ` v1 ` / ` /pdf ` / MIT Press's trailing article
number -- are cut back off , because a DOI with junk on the end is a DOI the
library will never match.

Each paper is then looked up against the library with the SAME policy the
/exists userscript endpoint uses ( server.lookup : a DOI settles it outright ,
a title is fuzzy-matched at the same cutoff ) -- run IN-PROCESS on the manager
snapshot , so nothing needs to be listening on a port. A DOI miss is retried
by title alone before it is called missing : the note may carry the preprint
DOI while the library holds the journal version , and the title is what says
they are the same paper. Those recoveries are listed separately in the
report , since a DOI that doesn't match is worth a glance.

Output : output/check-missing/<log-stem>.md -- a table of what's missing ,
each row with the log's date , line number and link so you can find it again ,
plus the count of times the log mentions it ( a paper noted five times is one
you probably want ). Overwritten on each run. --list-only dumps what the
parser extracted , as JSON , without checking anything -- for when a log's
layout drifts and rows go missing from the report.
"""

import re
import sys
import html
import json
from datetime import datetime , timezone
from pathlib import Path
from urllib.parse import unquote , urlparse

from ..utils import utils


# ---------------------------------------------------------------------------
# Reading the log
# ---------------------------------------------------------------------------

_DATE_HEADING_RE = re.compile(
	r"^\s*#{1,6}\s+(\d{1,2}(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{4})\s*$" ,
	re.IGNORECASE )
_MD_LINK_RE  = re.compile( r"\[[^\]]*\]\((https?://[^\s]+)\)" , re.IGNORECASE )
_BARE_URL_RE = re.compile( r"https?://[^\s<>)\]]+" , re.IGNORECASE )

# A line that separates blocks. The log is written in a WYSIWYG editor that
# emits ` &nbsp; ` for an empty paragraph , so that is the block boundary.
_BLOCK_BREAKS = { "&nbsp;" , "<br>" , "<br/>" , "<br />" }

# Bare words that sit where a title would but are organisation , not a paper.
_NON_TITLE = {
	"abstract" , "code" , "data" , "dataset" , "datasets" , "github" , "notes" ,
	"paper" , "papers" , "pdf" , "project" , "resources" , "supplement" ,
	"supplementary material" , "tangent" , "todo" , "video" ,
}

# What a browser glues onto a DOI inside a URL , and utils.normalize_doi's
# trailing-PATH trim doesn't cover : dotted suffixes ( bioRxiv's ` .full ` /
# ` .abstract ` ) , a couple more path words , and the ` v1 ` preprint version
# that is routing , not part of the deposited DOI.
_DOI_DOT_SUFFIX_RE  = re.compile( r"(?i)(?:\.full|\.abstract|\.pdf)+$" )
_DOI_PATH_SUFFIX_RE = re.compile( r"(?i)(?:/html|/_p)$" )
_DOI_VERSION_RE     = re.compile( r"(?i)/?v\d+$" )


def _strip_markdown( value ):
	"""The small subset of Markdown a title line gets wrapped in : list
	markers , blockquote , heading hashes , bold / italic , backslash escapes."""
	value = html.unescape( value.strip() )
	value = re.sub( r"^\s{0,3}(?:[-*+]\s+|\d+[.)]\s+)" , "" , value )
	value = re.sub( r"^\s*>\s?" , "" , value )
	value = re.sub( r"^#{1,6}\s+" , "" , value )
	value = re.sub( r"^\*\*(.*?)\*\*$" , r"\1" , value )
	value = re.sub( r"^__(.*?)__$" , r"\1" , value )
	value = re.sub( r"^\*(.*?)\*$" , r"\1" , value )
	value = re.sub( r"^_(.*?)_$" , r"\1" , value )
	value = value.replace( r"\_" , "_" ).replace( r"\#" , "#" ).replace( r"\&" , "&" )
	return " ".join( value.split() )


def _urls( line ):
	"""The links on a line : the targets of Markdown links when there are any ,
	else the bare URLs. Trailing punctuation is prose , not URL."""
	found = [ u.rstrip( ").,;" ) for u in _MD_LINK_RE.findall( line ) ]
	if found:
		return found
	return [ u.rstrip( ".,;" ) for u in _BARE_URL_RE.findall( line ) ]


def _doi_in( value ):
	"""A clean DOI out of a URL or a line of text , or "" . utils.normalize_doi
	finds it and cuts the path junk ( /full , /figures/3 ... ) ; the rest here
	is the URL-only debris listed above."""
	value = unquote( value ).replace( r"\_" , "_" ).replace( r"\#" , "#" )
	doi = utils.normalize_doi( value ) or ""
	if not doi:
		return ""
	doi = _DOI_DOT_SUFFIX_RE.sub( "" , doi )
	doi = _DOI_PATH_SUFFIX_RE.sub( "" , doi )
	doi = _DOI_VERSION_RE.sub( "" , doi ).rstrip( "/" )
	# MIT Press article routes put an internal article number and a slug AFTER
	# the DOI ( .../doi/10.1162/jocn_a_02087/118023/Title ) , and the DOI class
	# swallows all of it. Their suffixes never contain a slash , so cut to two.
	host = ( urlparse( value ).hostname or "" ).lower()
	if host == "direct.mit.edu" or host.startswith( "direct-mit-edu." ):
		parts = doi.split( "/" )
		doi = "/".join( parts[ :2 ] )
	return doi


def _looks_like_title( line ):
	raw = line.strip()
	if not raw or raw.lower() in _BLOCK_BREAKS or _urls( raw ):
		return False
	if raw.startswith( ( "```" , "<!--" , "|" ) ) or raw.endswith( "-->" ):
		return False
	# Tab-indented lines are annotations ( "Tangent = Theory" , "ROI !!" ) ,
	# never the paper. A single stray leading SPACE does occur on real titles.
	if line.startswith( "\t" ):
		return False
	title = _strip_markdown( raw )
	if len( title ) < 2 or title.lower() in _NON_TITLE:
		return False
	if _DATE_HEADING_RE.match( raw ) or re.fullmatch( r"\d{4}" , title ):
		return False
	return True


def _parse_block( block , date ):
	"""One ` &nbsp; `-delimited block -> one paper , or None when it has no link
	( a block with no link is a note , not a paper ) or no title above the first
	link. The title is the FIRST eligible line ; later unindented lines are
	access notes , subtitles , or text pasted off a publisher page."""
	urls , first_url_at = [] , None
	for pos , ( _ , line ) in enumerate( block ):
		found = _urls( line )
		if found:
			if first_url_at is None:
				first_url_at = pos
			urls.extend( found )
	if first_url_at is None:
		return None

	for line_no , line in block[ :first_url_at ]:
		if _looks_like_title( line ):
			break
	else:
		return None

	doi = ""
	for candidate in [ *urls , *( l for _ , l in block ) ]:
		doi = _doi_in( candidate )
		if doi:
			break

	return {
		"title": _strip_markdown( line ) ,
		"line":  line_no ,
		"date":  date ,
		"url":   urls[ 0 ] ,
		"doi":   doi ,
		"occurrences": [ line_no ] ,
	}


def extract_papers( path ):
	"""Every paper block in the log , in order , as
	{ title , line , date , url , doi , occurrences } . Returns ( papers , n_lines )."""
	lines  = path.read_text( encoding="utf-8-sig" ).splitlines()
	papers , block , date = [] , [] , ""

	def flush():
		nonlocal block
		paper = _parse_block( block , date )
		if paper:
			papers.append( paper )
		block = []

	for line_no , line in enumerate( lines , start=1 ):
		m = _DATE_HEADING_RE.match( line )
		if m:
			flush()
			date = m.group( 1 ).upper()
			continue
		if line.strip().lower() in _BLOCK_BREAKS:
			flush()
			continue
		block.append( ( line_no , line ) )
	flush()
	return papers , len( lines )


def deduplicate( papers ):
	"""One entry per paper , keyed on the library's own title normalization so a
	re-noted paper with different capitalisation or dashes is the same paper.
	The first mention wins the row ; later ones add their line to occurrences
	and fill in a DOI / link the first mention lacked."""
	unique = {}
	for p in papers:
		key = utils.normalize_title( p[ "title" ] ) or p[ "title" ]
		seen = unique.get( key )
		if seen is None:
			unique[ key ] = p
			continue
		seen[ "occurrences" ].extend( p[ "occurrences" ] )
		if not seen[ "doi" ] and p[ "doi" ]:
			seen[ "doi" ] = p[ "doi" ]
		if not seen[ "url" ] and p[ "url" ]:
			seen[ "url" ] = p[ "url" ]
	return list( unique.values() )


# ---------------------------------------------------------------------------
# Checking against the library
# ---------------------------------------------------------------------------

def check_papers( args , papers ):
	"""Stamp each paper with how it matched the library : ` found ` = "doi" |
	"title" | "" ( missing ) , plus ` doi_miss ` = True when the DOI on the note
	is NOT in the library but the title is -- a recovered false negative.

	Same code the /exists endpoint runs ( server.lookup over a SnapshotCache ) ,
	in this process : the manager's titles + DOIs are read straight from the
	source , the way the server reads them , so no server has to be up and the
	answer can't differ from the userscript's."""
	from ..server import server as exists_server

	cache = exists_server.SnapshotCache( args )
	for p in papers:
		p[ "found" ] , p[ "doi_miss" ] = "" , False

	queries = [ { "id": i , "title": p[ "title" ] , "doi": p[ "doi" ] }
		for i , p in enumerate( papers ) ]
	for p , r in zip( papers , exists_server.lookup( cache , queries ) ):
		if r[ "exists" ]:
			p[ "found" ] = "doi" if p[ "doi" ] else "title"

	# A DOI is authoritative to lookup(), so a DOI that isn't in the library
	# ends the question there. Ask again on the title alone : the note may
	# carry the preprint DOI for a paper the library has as the journal
	# version , or a DOI the URL cleanup above still got slightly wrong.
	retry = [ p for p in papers if not p[ "found" ] and p[ "doi" ] ]
	queries = [ { "id": i , "title": p[ "title" ] , "doi": "" }
		for i , p in enumerate( retry ) ]
	for p , r in zip( retry , exists_server.lookup( cache , queries ) ):
		if r[ "exists" ]:
			p[ "found" ] , p[ "doi_miss" ] = "title" , True


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def _cell( value ):
	return str( value or "" ).replace( "|" , r"\|" ).replace( "\n" , " " )


def _link( url ):
	if not url:
		return "—"
	safe = url.replace( "\\" , "" ).replace( "(" , "%28" ).replace( ")" , "%29" )
	return f"[source]({safe})"


def _rows( papers ):
	out = []
	for i , p in enumerate( papers , start=1 ):
		out.append(
			f"| {i} | {_cell( p['title'] )} | {_cell( p['doi'] ) or '—'} | "
			f"{p['date'] or '—'} | {p['line']} | {_link( p['url'] )} | "
			f"{len( p['occurrences'] )} |" )
	return out

_TABLE_HEAD = (
	"| # | Title | DOI | Date | Line | Link | Mentions |" ,
	"|---:|---|---|---|---:|---|---:|" ,
)


def render_report( source , manager_label , n_extracted , papers ):
	missing   = [ p for p in papers if not p[ "found" ] ]
	recovered = [ p for p in papers if p[ "doi_miss" ] ]
	generated = datetime.now( timezone.utc ).astimezone().isoformat( timespec="seconds" )

	lines = [
		"# Missing papers report" ,
		"" ,
		f"- Generated: {generated}" ,
		f"- Source: `{source}`" ,
		f"- Library: {manager_label}" ,
		f"- Extracted entries: {n_extracted}" ,
		f"- Unique papers checked: {len( papers )}" ,
		f"- Missing: {len( missing )}" ,
		f"- Found by title only ( DOI not in library ): {len( recovered )}" ,
		"" ,
	]
	if not missing:
		lines += [ "Every paper in the log is in the library." , "" ]
	else:
		lines += [ "## Missing" , "" , *_TABLE_HEAD , *_rows( missing ) , "" ]
	if recovered:
		lines += [
			"## Found by title only" ,
			"" ,
			"The DOI the log carries is not in the library , but the title matched. "
			"Usually a preprint DOI where the library holds the journal version -- "
			"worth a glance." ,
			"" ,
			*_TABLE_HEAD , *_rows( recovered ) , "" ,
		]
	return "\n".join( lines )


def report_path( args , source ):
	out = getattr( args , "check_missing_out" , None )
	if out:
		out = Path( out )
		return out if out.is_absolute() else args.output.joinpath( out )
	return args.output.joinpath( "check-missing" , f"{source.stem}.md" )


# ---------------------------------------------------------------------------
# The task
# ---------------------------------------------------------------------------

def run( args ):
	source = Path( args.check_missing_input )
	if not source.is_file():
		raise SystemExit( f"CHECK     :: not a file : {source}" )

	extracted , n_lines = extract_papers( source )
	papers = deduplicate( extracted )
	list_only = getattr( args , "check_missing_list_only" , False )
	# --list-only owns stdout ( the JSON is meant to be piped ) , so the tally
	# steps aside to stderr there.
	print( f"CHECK     :: {source.name} -- {len( extracted )} entries "
	       f"( {len( papers )} unique ) over {n_lines} lines" ,
	       file=sys.stderr if list_only else sys.stdout )

	if list_only:
		json.dump( papers , sys.stdout , ensure_ascii=False , indent=2 )
		sys.stdout.write( "\n" )
		return

	check_papers( args , papers )

	from .code import _resolve_managers
	managers = _resolve_managers( args )
	label    = " + ".join( managers ) if managers else "all"

	out = report_path( args , source )
	out.parent.mkdir( parents=True , exist_ok=True )
	out.write_text( render_report( source , label , len( extracted ) , papers ) ,
		encoding="utf-8" )

	n_missing   = sum( 1 for p in papers if not p[ "found" ] )
	n_recovered = sum( 1 for p in papers if p[ "doi_miss" ] )
	print( f"CHECK     :: {n_missing} missing , {len( papers ) - n_missing} in the "
	       f"library ( {n_recovered} by title after a DOI miss ) -> {out}" )
