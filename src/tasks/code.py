"""
prma code : harvest SOURCE-CODE / DATA links for every paper in the unified
papers/ DB and pin them on each record at paper[ 'code' ] , so the dashboard
can show a "Code" column. The CLI command additionally rolls every link up
into output/code/code.xlsx.

Where the links come from , per paper :
  - the OpenAlex ABSTRACT  ( output/cache/openalex/<base64(doi)>.json ,
    reconstructed from abstract_inverted_index )
  - the OCR FULL TEXT      ( the text ` prma ocr ` pins on each YOLO
    detection -- paper[ 'yolo' ].pages[*][*].ocr )

We scan that combined haystack for URLs pointing at known code / data hosts
( GitHub , GitLab , Bitbucket , OSF , Dryad , Zenodo , Figshare , Code Ocean ,
SourceForge , Hugging Face , ... ) and store the unique , classified hits.

Pipeline : depends on OCR. ` ocr.run ` is called inline ( it runs YOLO inline
for any paper that doesn't have it ) so ` prma code ` is a one-stop command :
  snapshot -> yolo -> ocr -> scan
Every inner stage is idempotent ; papers already done fall through cheaply.
This same run() is stage 'code' in the per-paper suite ( process.run_suite ) ,
so a paper added under ` prma server --watch ` gets scanned automatically.

Idempotency : a paper that's been scanned carries a 'code' marker
  paper[ 'code' ] = { 'at': <iso> , 'links': [ { 'url' , 'source' , 'found_in' } ] }
( an EMPTY links list still counts as scanned -- the marker is the sentinel ,
so a paper with no links isn't re-walked every run ). Pass --force to re-scan.

The xlsx is written ONLY by the CLI ( write_workbook ) , never by the stage --
the per-paper suite scopes itself to one paper via args.only_keys , and a
one-row workbook isn't what you want from the live worker.
"""

import re
from pathlib import Path
from urllib.parse import urlparse , quote

from tqdm import tqdm

from ..db    import papers as papers_db
from ..utils import utils

# ezproxy host used across the project ( library_search , the missing.xlsx
# rollup ) ; swap here if you fork for a different institution.
_PROXY_URL_TEMPLATE = "https://doi-org.ezproxy.libraries.wright.edu/{doi}"


# ---------------------------------------------------------------------------
# Known code / data hosts -> human label ( the "source" shown in the UI )
# ---------------------------------------------------------------------------
# Order matters : more-specific subdomains ( gist. , raw. ) come before the
# bare host so they win the first-match in _classify_host.

_HOST_LABELS = (
	( "gist.github.com"            , "GitHub Gist"  ) ,
	( "raw.githubusercontent.com"  , "GitHub"       ) ,
	( "github.com"                 , "GitHub"       ) ,
	( "github.io"                  , "GitHub"       ) ,
	( "gitlab.com"                 , "GitLab"       ) ,
	( "bitbucket.org"              , "Bitbucket"    ) ,
	( "osf.io"                     , "OSF"          ) ,
	( "datadryad.org"              , "Dryad"        ) ,
	( "dryad.org"                  , "Dryad"        ) ,
	( "zenodo.org"                 , "Zenodo"       ) ,
	( "figshare.com"               , "Figshare"     ) ,
	( "codeocean.com"              , "Code Ocean"   ) ,
	( "sourceforge.net"            , "SourceForge"  ) ,
	( "huggingface.co"             , "Hugging Face" ) ,
	( "gitee.com"                  , "Gitee"        ) ,
	( "mybinder.org"               , "Binder"       ) ,
	( "kaggle.com"                 , "Kaggle"       ) ,
	( "colab.research.google.com"  , "Colab"        ) ,
	( "code.google.com"            , "Google Code"  ) ,
	( "savannah.gnu.org"           , "Savannah"     ) ,
	( "git.io"                     , "GitHub"       ) ,
)

# DOI URLs ( doi.org/... ) that resolve to a code / data archive : classified
# by a substring in the DOI path so e.g. https://doi.org/10.5281/zenodo.123 is
# still caught as Zenodo.
_DOI_PATH_HINTS = (
	( "zenodo"   , "Zenodo"   ) ,
	( "osf.io"   , "OSF"      ) ,
	( "dryad"    , "Dryad"    ) ,
	( "figshare" , "Figshare" ) ,
)

# A URL with an explicit scheme or a leading www. ...
_URL_RE = re.compile( r"(?i)\b(?:https?://|www\.)[^\s<>\"'()\[\]{}]+" )

# ... plus bare host/path mentions of the known code hosts ( ` github.com/u/r `
# with no scheme , common in OCR text ). Built from _HOST_LABELS so the two
# stay in sync , minus suffix-only hosts that are NEVER the registrable host
# ( ` github.io ` is always ` <user>.github.io ` -- matching it bare would drop
# the subdomain and yield a broken URL ; it's still classified for scheme'd URLs ).
_BARE_ONLY_SUFFIXES = { "github.io" }
_BARE_RE = re.compile(
	r"(?i)\b(?:" +
	"|".join( re.escape( h ) for h , _ in _HOST_LABELS if h not in _BARE_ONLY_SUFFIXES ) +
	r")/[^\s<>\"'()\[\]{}]+"
)

# Characters that are almost always sentence punctuation trailing a URL , not
# part of it. Stripped from each captured candidate's tail.
_TRAILING = ".,;:!?'\"”’)>]}»"


def _classify_host( host , path ):
	"""Map a URL's host ( + path , for doi.org archives ) to a source label ,
	or None if it isn't a code / data host we care about."""
	host = ( host or "" ).lower()
	if host.startswith( "www." ):
		host = host[ 4: ]
	for suffix , label in _HOST_LABELS:
		if host == suffix or host.endswith( "." + suffix ):
			return label
	if host == "doi.org" or host.endswith( ".doi.org" ):
		p = ( path or "" ).lower()
		for hint , label in _DOI_PATH_HINTS:
			if hint in p:
				return label
	return None


def _clean_url( raw ):
	"""Trim trailing sentence punctuation and normalize a captured candidate to
	a fetchable URL ( prepend https:// to bare-host / www. matches )."""
	u = ( raw or "" ).strip().rstrip( _TRAILING )
	if not u:
		return None
	if not re.match( r"(?i)^https?://" , u ):
		u = "https://" + u.lstrip( "/" )
	return u


def _dedup_key( url ):
	"""Canonical key for de-duping : drop scheme , a leading www. , and the
	trailing slash so https://www.zenodo.org/x/ and zenodo.org/x collapse to
	one ( the bare-host regex and the scheme regex can both match the same
	link )."""
	k = re.sub( r"(?i)^https?://" , "" , ( url or "" ).lower() )
	if k.startswith( "www." ):
		k = k[ 4: ]
	return k.rstrip( "/" )


def _is_plausible( url , path ):
	"""Reject links that clearly aren't a real , reachable repo / record so the
	dashboard never surfaces a dead link :
	  - a bare host with no path ( ` https://github.com ` -- no repo ) ;
	  - one STILL cut off at a hyphen after we tried to rejoin it across the OCR
	    line break ( ` …/kelseyhan- ` with no continuation found ).
	Better to drop a fragment than to emit a 404."""
	if _ends_truncated( url ):
		return False
	return bool( ( path or "" ).strip( "/" ) )


def extract_links_tagged( *labeled_texts ):
	"""Scan ( found_in , text ) pairs for code / data links. Returns a de-duped
	list ( first-seen order ) of { 'url' , 'source' , 'found_in' } dicts , where
	`found_in` is the source blob the link was first seen in ( 'abstract' ,
	'ocr' , ... ). De-duped on the normalized URL ( case-insensitive ) , first
	occurrence wins. OCR-split URLs are rejoined ( see _iter_candidates ) and
	still-broken fragments dropped ( see _is_plausible )."""
	seen  = set()
	links = []
	for found_in , text in labeled_texts:
		if not text:
			continue
		for m in _iter_candidates( text ):
			url = _clean_url( m )
			if not url:
				continue
			try:
				parsed = urlparse( url )
			except Exception:
				continue
			label = _classify_host( parsed.netloc , parsed.path )
			if not label:
				continue
			if not _is_plausible( url , parsed.path ):
				continue
			dedup = _dedup_key( url )
			if dedup in seen:
				continue
			seen.add( dedup )
			links.append( { "url": url , "source": label , "found_in": found_in } )
	return links


# ---------------------------------------------------------------------------
# OCR line-break repair
# ---------------------------------------------------------------------------
# Our OCR isn't perfect : it routinely splits a long URL across two physical
# lines , so the captured token is cut off ( ` https://github.com/kelseyhan- ` )
# and the rest ( ` lab/repo ` ) sits at the start of the next line. We rejoin
# them , but only on a HIGH-PRECISION 'this link is truncated' signal so we
# never glue a COMPLETE url to the prose that happens to follow it.

# A run of URL-ish characters -- used to grab the continuation off the next line.
_URLCHARS_RE = re.compile( r"[^\s<>\"'()\[\]{}]+" )
# The whitespace a line break leaves between a cut-off URL and its continuation :
# optional spaces , an optional single newline , optional spaces. ( _ocr_fulltext
# joins detections with '\n' and OCR engines emit '\n' within a detection , so
# the break shows up as a newline ; cross-detection it can be a lone space. )
_BREAK_GAP_RE = re.compile( r"[ \t]*\n?[ \t]*" )


def _ends_truncated( token ):
	"""Does this URL token look CUT OFF mid-link? A real host / GitHub username /
	path segment never ends in a hyphen ( GitHub forbids a trailing '-' , and
	PDF / OCR hyphenation drops one at the break ) , so a trailing '-' -- after
	stripping sentence punctuation -- is the tell."""
	return token.rstrip( _TRAILING ).endswith( "-" )


def _host_only( token ):
	"""True when the token is just a bare host with no path yet ( ` github.com/ ` ,
	` https://osf.io ` ) -- i.e. the repo / record id was split onto the next
	line. Distinguished from a real link like ` github.com/owner/repo ` which
	already carries a path."""
	t = re.sub( r"(?i)^https?://" , "" , token.rstrip( _TRAILING ) )
	host , _ , rest = t.partition( "/" )
	return not rest.strip( "/" )


def _stitch( text , end , token ):
	"""Rejoin a URL the OCR split across a line break onto `token` , reading
	continuation run( s ) off the following line( s ). Fires only on a precise
	'cut off' signal :
	  - the token ends in a hyphen -> rejoin across spaces OR a newline ( the
	    hyphen itself is the signal , so either gap is safe ) ;
	  - the token is a bare host with no path -> rejoin ONLY across a real
	    newline ( the path is on the next line ) , never across a lone space
	    ( which is far more likely just the next word ).
	Capped at a few hops so a pathological input can't loop. The continuation
	fragment isn't itself a URL match , so it won't be double-yielded."""
	for _ in range( 4 ):
		hyphen = _ends_truncated( token )
		hostonly = _host_only( token )
		if not ( hyphen or hostonly ):
			break
		gap = _BREAK_GAP_RE.match( text , end )
		if not gap or gap.end() == end:                 # nothing split off
			break
		if hostonly and not hyphen and "\n" not in text[ end : gap.end() ]:
			break                                       # host-only needs a real line break
		cont = _URLCHARS_RE.match( text , gap.end() )
		if not cont:
			break
		token , end = token + cont.group( 0 ) , cont.end()
	return token


def _iter_candidates( text ):
	"""Yield raw URL-ish substrings from `text` ( scheme/www. matches first ,
	then bare known-host matches ) , each rejoined across OCR line breaks via
	_stitch. Overlaps are fine -- _clean_url normalizes and the caller de-dupes."""
	for rx in ( _URL_RE , _BARE_RE ):
		for m in rx.finditer( text ):
			yield _stitch( text , m.end() , m.group( 0 ) )


# ---------------------------------------------------------------------------
# Per-paper text sources : OpenAlex abstract + OCR full text
# ---------------------------------------------------------------------------

def _reconstruct_abstract( inv_index ):
	"""Rebuild plain abstract text from OpenAlex's abstract_inverted_index
	( { word: [positions] } ). Mirrors dashboard.indexer._reconstruct_abstract ."""
	if not inv_index:
		return ""
	positions = [ ( pos , word ) for word , poses in inv_index.items() for pos in poses ]
	positions.sort()
	return " ".join( w for _ , w in positions )


def _paper_oa_meta( args , doi ):
	"""The full OpenAlex work meta for `doi` from the on-disk cache , or {} on
	any miss. No network -- reads whatever ` prma main ` / reindex fetched."""
	if not doi:
		return {}
	b64 = utils.base64_encode( doi )
	if not b64:
		return {}
	fp = args.output.joinpath( "cache" , "openalex" , f"{b64}.json" )
	if not fp.exists():
		return {}
	try:
		return utils.read_json( fp ) or {}
	except Exception:
		return {}


def _paper_abstract( args , doi ):
	"""OpenAlex abstract text for `doi` from the on-disk cache , or "" on any miss."""
	return _reconstruct_abstract( _paper_oa_meta( args , doi ).get( "abstract_inverted_index" ) )


def _ocr_fulltext( paper ):
	"""Concatenate the OCR text pinned on a paper's YOLO detections
	( ` prma ocr ` output ) into one blob , one engine's text per detection
	( whichever is present ). Joined with NEWLINES ( not spaces , unlike the
	dashboard's haystack copy ) so the line-break repair in _stitch keeps the
	detection boundaries it needs to rejoin OCR-split URLs."""
	parts = []
	for page in ( ( paper.get( "yolo" ) or {} ).get( "pages" ) or [] ):
		for det in ( page or [] ):
			ocr = det.get( "ocr" ) or {}
			for v in ocr.values():
				if v:
					parts.append( v )
					break
	return "\n".join( parts )


def paper_code_links( args , key , paper ):
	"""Scan one paper's abstract + OCR text and return the tagged link list
	( what gets stored under paper[ 'code' ][ 'links' ] )."""
	doi      = utils.normalize_doi( paper.get( "doi" ) )
	abstract = _paper_abstract( args , doi )
	ocr_text = _ocr_fulltext( paper )
	return extract_links_tagged( ( "abstract" , abstract ) , ( "ocr" , ocr_text ) )


# ---------------------------------------------------------------------------
# Manager scoping ( same shape every PDF task uses )
# ---------------------------------------------------------------------------

def _resolve_managers( args ):
	name = ( getattr( args , "manager" , None ) or "zotero" ).lower()
	if getattr( args , "mendeley" , False ):
		name = "mendeley"
	elif getattr( args , "zotero" , False ):
		name = "zotero"
	if name == "all":
		return ( papers_db.SOURCE_ZOTERO , papers_db.SOURCE_MENDELEY )
	return ( name , )


def _paper_matches_managers( paper , managers ):
	if not managers:
		return True
	srcs = paper.get( "sources" ) or {}
	return any( m in srcs for m in managers )


# ---------------------------------------------------------------------------
# The stage : scan each scoped paper and pin paper[ 'code' ]
# ---------------------------------------------------------------------------

def run( args ):
	"""Scan every in-scope paper for code / data links and store them on the
	record. Honors args.only_keys ( so process.run_suite / the --watch worker
	can scope it to one paper ) , --manager , and --force.

	OCR is ensured inline ( idempotent ) so a standalone ` prma code ` works
	end-to-end ; inside run_suite , OCR already ran as an earlier stage and
	this second pass is a cheap no-op."""
	from . import ocr as ocr_task
	ocr_task.run( args )

	force         = getattr( args , "code_force" , False )
	managers      = _resolve_managers( args )
	manager_label = " + ".join( managers ) if managers else "all"

	# Plan : which papers still need scanning.
	jobs , skip_done , skip_other = [] , 0 , 0
	for key , paper in papers_db.iter_all( args ):
		if not _paper_matches_managers( paper , managers ):
			skip_other += 1
			continue
		if not force and paper.get( "code" ) is not None:
			skip_done += 1
			continue
		jobs.append( key )

	print(
		f"CODE      :: ({manager_label})  {len(jobs)} papers to scan "
		f"( skipped: already-done={skip_done} other-manager={skip_other} )"
	)

	n_with , n_links = 0 , 0
	for key in tqdm( jobs , desc="Papers" , unit="paper" ):
		paper = papers_db.load( args , key )
		if paper is None:
			continue
		try:
			links = paper_code_links( args , key , paper )
		except Exception as e:
			print( f"CODE      :: {key}: scan failed ( {e} )" )
			continue
		paper[ "code" ] = { "at": papers_db._utc_now_iso() , "links": links }
		try:
			papers_db.save( args , paper )
		except Exception as e:
			print( f"CODE      :: {key}: save failed ( {e} )" )
			continue
		if links:
			n_with  += 1
			n_links += len( links )

	print(
		f"CODE      :: scanned {len(jobs)} papers -- "
		f"{n_with} have code/data links ( {n_links} links total )"
	)


# ---------------------------------------------------------------------------
# CLI-only : roll every paper's stored links up into output/code/code.xlsx
# ---------------------------------------------------------------------------
# Three sheets :
#   "Links by Paper" -- one row per ( paper , link ) , full paper metadata ;
#   "By Paper"       -- one row per paper , its links collapsed ;
#   "Unique Links"   -- one row per UNIQUE link across the library , with the
#                       DOIs it appears in and a total paper count.

_LINKS_HEADERS  = ( "DOI" , "Title" , "Added" , "Published" ,
                    "Source" , "URL" , "Found In" , "Proxy" , "PDF" )
_BYPAPER_HEADERS = ( "DOI" , "Title" , "Added" , "Published" ,
                     "# Links" , "Sources" , "Links" , "Proxy" , "PDF" )
_UNIQUE_HEADERS = ( "URL" , "Source" , "# Papers" , "DOIs" )


def _pub_date( meta ):
	"""Publication date string from an OpenAlex meta dict ( YYYY-MM-DD , or
	YYYY-01-01 when only the year is known ) , or "" ."""
	if not meta:
		return ""
	pd = meta.get( "publication_date" )
	if pd:
		return str( pd )
	py = meta.get( "publication_year" )
	return f"{py}-01-01" if py else ""


def _file_url( abs_path ):
	"""A clickable file:// URL for a local PDF path , or "" ."""
	if not abs_path:
		return ""
	return "file://" + quote( str( abs_path ) , safe="/:" )


def _added_date( paper ):
	"""When the paper was first added to the manager ( created_at ) , date only."""
	return str( paper.get( "created_at" ) or "" )[ :10 ]


def write_workbook( args ):
	"""Write output/code/code.xlsx ( three sheets -- see header comment ) over
	the whole library. Reads the 'code' marker ` run ` already pinned ; call
	after run(). Whole-library : iter_all is NOT scoped here ( the CLI leaves
	args.only_keys unset )."""
	out_dir = args.output.joinpath( "code" )
	out_dir.mkdir( parents=True , exist_ok=True )
	out_path = out_dir.joinpath( "code.xlsx" )

	link_rows     = []
	by_paper_rows = []
	unique        = {}   # dedup-key -> { url , source , dois:set() }
	n_papers_with = 0

	for key , paper in papers_db.iter_all( args ):
		links = ( ( paper.get( "code" ) or {} ).get( "links" ) ) or []
		links = [ l for l in links if ( l or {} ).get( "url" ) ]
		if not links:
			continue
		n_papers_with += 1

		doi       = utils.normalize_doi( paper.get( "doi" ) ) or ""
		title     = paper.get( "title" ) or ""
		added     = _added_date( paper )
		published = _pub_date( _paper_oa_meta( args , doi ) )
		pdf_path  = paper.get( "pdf_path" ) or ""

		# Per-paper cells reused across this paper's rows ( read-only ).
		doi_cell   = utils.Link( doi , f"https://doi.org/{doi}" ) if doi else ""
		proxy_url  = _PROXY_URL_TEMPLATE.format( doi=doi ) if doi else ""
		proxy_cell = utils.Link( proxy_url , proxy_url ) if proxy_url else ""
		pdf_cell   = utils.Link( Path( pdf_path ).name , _file_url( pdf_path ) ) if pdf_path else ""

		sources , urls = [] , []
		for l in links:
			url , src = l.get( "url" ) , l.get( "source" ) or ""
			link_rows.append( [
				doi_cell , title , added , published ,
				src , utils.Link( url , url ) , l.get( "found_in" ) or "" ,
				proxy_cell , pdf_cell ,
			] )
			urls.append( url )
			if src and src not in sources:
				sources.append( src )
			u = unique.setdefault( _dedup_key( url ) ,
				{ "url": url , "source": src , "dois": set() } )
			if doi:
				u[ "dois" ].add( doi )

		by_paper_rows.append( [
			doi_cell , title , added , published ,
			len( urls ) , " , ".join( sources ) , "\n".join( urls ) ,
			proxy_cell , pdf_cell ,
		] )

	# Most-linked papers first ; alphabetical within a tie.
	by_paper_rows.sort( key=lambda r: ( -r[ 4 ] , ( r[ 1 ] or "" ).lower() ) )

	# Unique links : most-shared first ( "found in other papers" ) , then host.
	unique_rows = []
	for u in unique.values():
		dois = sorted( u[ "dois" ] )
		unique_rows.append( [
			utils.Link( u[ "url" ] , u[ "url" ] ) , u[ "source" ] or "" ,
			len( dois ) , "\n".join( dois ) ,
		] )
	unique_rows.sort( key=lambda r: ( -r[ 2 ] , ( r[ 1 ] or "" ) ) )

	utils.write_xlsx( out_path , [
		( "Links by Paper" , _LINKS_HEADERS   , link_rows     ) ,
		( "By Paper"       , _BYPAPER_HEADERS , by_paper_rows ) ,
		( "Unique Links"   , _UNIQUE_HEADERS  , unique_rows   ) ,
	] )
	print(
		f"CODE      :: workbook -> {out_path} "
		f"( {len(link_rows)} links / {len(unique_rows)} unique "
		f"across {n_papers_with} papers )"
	)
	return out_path
