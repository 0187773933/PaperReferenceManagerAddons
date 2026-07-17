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
from ..utils import methods as methods_vocab
from ..pdf   import section_text

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
# Repo / project enrichment : fetch details , tag each by method keyword
# ---------------------------------------------------------------------------
# When config.yaml has a github.api_key , fetch_github() pulls each UNIQUE github
# repo's metadata + README into output/cache/github/ ; likewise , with an
# osfio.api_key , fetch_osf() pulls each UNIQUE OSF node's metadata + Home wiki
# into output/cache/osf/ . detect_methods() then scans that text for the
# neuroimaging / electrophysiology modalities below so code.xlsx can carry a
# sortable column per method ( GitHub and OSF links alike ). A link ALSO inherits
# the modalities of the papers that cite it -- _paper_text_methods() runs the same
# keyword scan over each linking paper's abstract + isolated METHODS section ( NOT
# the full OCR , whose references would false-positive ) -- so a repo whose README
# never names a method still gets tagged from its citing fMRI / EEG / ... studies.

# The vocabulary itself lives in < --config >/methods.py -- see
# src/utils/methods.py for the format ( and the built-in fallback ). It's
# loaded per-run rather than baked in here so ` prma method-images ` renders
# the same labels this workbook columns by , and adding a modality is a config
# edit. Everything below therefore takes `args` : that's what knows --config .


def _record_haystack( rec ):
	"""Free-text blob for method detection , covering both a cached GitHub repo
	( full_name / description / topics / language / homepage / README ) and a
	cached OSF node ( title / description / tags / category / wiki )."""
	parts = [
		rec.get( "full_name" ) or rec.get( "title" )
			or f"{rec.get( 'owner' , '' )}/{rec.get( 'repo' , '' )}" ,
		rec.get( "description" ) or "" ,
		" ".join( rec.get( "topics" ) or rec.get( "tags" ) or [] ) ,
		rec.get( "language" ) or "" ,
		rec.get( "category" ) or "" ,
		rec.get( "homepage" ) or "" ,
		rec.get( "readme" ) or "" ,
	]
	return " ".join( p for p in parts if p ).lower()


def _scan_methods( args , hay ):
	"""Method labels whose acronym ( whole word ) or spelled-out phrases appear in
	`hay`. The one keyword pass both the repo/node records and the linking papers'
	own text run through -- and the same one ` prma method-images ` uses , via the
	shared vocabulary in < --config >/methods.py . Case is handled there ; `hay`
	needn't be pre-lowercased."""
	return methods_vocab.scan( args , hay )


def detect_methods( args , rec ):
	"""Which method labels appear in a cached GitHub repo or OSF node record
	( name / title + description + topics / tags + language + category +
	homepage + README / wiki )? [] for a missing / errored / empty record."""
	if not rec or rec.get( "status" ) != "ok":
		return []
	return _scan_methods( args , _record_haystack( rec ) )


def _paper_text_methods( args , key , meta ):
	"""Method labels found in a LINKING paper's own text : its OpenAlex abstract
	+ the isolated METHODS SECTION ( ` prma methods ` output , falling back to the
	'## Methods' slice of the rendered md ). Deliberately NOT the full OCR dump --
	that includes the reference list , which false-positives every modality the
	paper merely CITES. This lets a code link inherit the modality of the papers
	that use it even when the repo's README never spells it out ( e.g. a generic
	analysis repo linked only from fMRI studies gets tagged fMRI ). Empty when the
	paper has no methods section rendered ( then only abstract + README tag it )."""
	abstract = _reconstruct_abstract( ( meta or {} ).get( "abstract_inverted_index" ) )
	try:
		methods = section_text.resolve_section_text( args , key , "methods" )
	except Exception:
		methods = ""
	return _scan_methods( args , f"{abstract} {methods}" )


def paper_methods( args , key , paper ):
	"""Which modalities did THIS paper USE ? -- the same keyword pass the workbook's
	method columns run , over the paper's title + OpenAlex abstract + isolated
	METHODS section. As in _paper_text_methods , deliberately NOT the full OCR :
	its reference list names every modality the paper merely CITES. So a label
	here means the study itself used it , and reviews / theory papers correctly
	come back empty rather than claiming every modality they survey.

	Public because ` prma method-images ` shows these under each paper title --
	keep _METHODS the single vocabulary for both , so adding a modality there
	lights it up in the workbook AND the report."""
	doi   = utils.normalize_doi( paper.get( "doi" ) or key ) or ""
	meta  = _paper_oa_meta( args , doi )
	found = set( _paper_text_methods( args , key , meta ) )
	# The title is authoritative about the study and costs nothing to scan
	# ( "... using fMRI" ) ; it tags papers whose abstract never cached.
	found |= set( _scan_methods( args , paper.get( "title" ) or "" ) )
	return _order_methods( args , found )


def paper_methods_inferred( args , key , paper ):
	"""Last-resort modalities for a paper we are BLIND about -- OpenAlex never
	cached an abstract AND no Methods section was extracted , so paper_methods()
	had nothing but the title to read. Falls back to the rendered md body , which
	` prma md ` writes WITHOUT the reference list , making it the full text minus
	the citations that would false-positive every modality.

	Returns [] when we DO have an abstract or a Methods section : if that text
	named no modality , that is an answer , not a gap , and the md body would
	only add what the paper's intro / discussion CITES. ( Measured on this
	library : inferring for such papers tagged a BCI ethics paper with all five
	modalities and a behavioural aphasia study with fMRI + PET. ) Evidence here
	is weaker than paper_methods() -- callers should mark it as inferred."""
	doi      = utils.normalize_doi( paper.get( "doi" ) or key ) or ""
	abstract = _reconstruct_abstract( _paper_oa_meta( args , doi ).get( "abstract_inverted_index" ) )
	try:
		methods = section_text.resolve_section_text( args , key , "methods" )
	except Exception:
		methods = ""
	if abstract or methods:
		return []
	fname = utils.doi_to_filename( key )
	if not fname:
		return []
	md_path = args.output.joinpath( "md" , f"{fname}.md" )
	if not md_path.exists():
		return []
	try:
		md = md_path.read_text( encoding="utf-8" , errors="ignore" )
	except Exception:
		return []
	return _order_methods( args , set( _scan_methods( args , md ) ) )


def fetch_github( args ):
	"""Fetch + cache GitHub repo details ( metadata + README ) for every UNIQUE
	github repo among the library's code links , so write_workbook can tag them
	by method. No-op ( with a note ) when config.yaml has no github.api_key.
	Idempotent : cached repos are skipped ( ` --force-download ` re-fetches )."""
	from ..github.github import GitHub , parse_repo
	gh = GitHub( args )
	if not gh.configured():
		print(
			"CODE      :: no github.api_key in config.yaml -- skipping GitHub "
			"enrichment ( the method columns in code.xlsx will be blank )"
		)
		return
	# Downloads are re-fetched only under --force-download ; plain --force reuses
	# the cache ( new / uncached repos are still fetched either way ).
	force = getattr( args , "code_force_download" , False )

	repos = {}   # ( owner , repo ) -> True
	for key , paper in papers_db.iter_all( args ):
		for l in ( ( paper.get( "code" ) or {} ).get( "links" ) ) or []:
			pr = parse_repo( ( l or {} ).get( "url" ) )
			if pr:
				repos[ pr ] = True
	if not repos:
		print( "CODE      :: no GitHub repos among the code links -- nothing to fetch" )
		return

	print( f"CODE      :: fetching GitHub details for {len( repos )} repo(s) -> {gh.cache_dir}" )
	n_ok , n_miss = 0 , 0
	for owner , repo in tqdm( sorted( repos ) , desc="GitHub" , unit="repo" ):
		rec = gh.fetch_repo( owner , repo , force=force )
		if rec.get( "status" ) == "ok":
			n_ok += 1
		else:
			n_miss += 1
	print( f"CODE      :: GitHub fetch done ( {n_ok} ok , {n_miss} missing/error )" )


def resolve_github( args ):
	"""Second pass over the GitHub links fetch_github got a 404 for : most are
	OCR damage ( a truncated / merged repo name ) rather than genuinely private
	repos , and the owner ( profile ) almost always survived intact. For each
	broken repo we list the owner's REAL repos ( cheap , core rate limit ) and
	fuzzy-match the mangled name ; on a confident hit we fetch the corrected
	repo's metadata and pin a `resolved_url` on every paper link that pointed at
	the broken one -- so code.xlsx and the dashboard show the working link ( and
	it gets method-tagged like any other ).

	No-op without a github.api_key. Idempotent : a not_found record we've already
	tried carries a `resolve` marker and isn't re-looked-up ; ` --force-download `
	retries ( plain ` --force ` still resolves NEW not_found links + re-pins )."""
	from ..github.github import GitHub , parse_repo
	gh = GitHub( args )
	if not gh.configured():
		return   # fetch_github already noted the missing key
	# --force-download re-runs the ( networked ) match for links already resolved ;
	# plain --force keeps cached resolutions but still resolves NEW not_found ones
	# and re-pins resolved_url on papers ( the local recalc ).
	force = getattr( args , "code_force_download" , False )

	# Broken repos = the not_found records fetch_github just cached. Reading the
	# cache dir avoids a second full papers pass just to rediscover them.
	broken = []
	for fp in sorted( gh.cache_dir.glob( "*.json" ) ):
		rec = utils.read_json( fp ) if fp.exists() else None
		if rec and rec.get( "status" ) == "not_found" and rec.get( "owner" ) and rec.get( "repo" ):
			broken.append( rec )
	if not broken:
		print( "CODE      :: no unresolved GitHub links to repair" )
		return

	print( f"CODE      :: repairing {len( broken )} unresolved GitHub link(s) via owner repo listings" )
	resolved = {}   # ( owner.lower , repo.lower ) -> corrected url
	n_fixed = n_none = 0
	for rec in tqdm( broken , desc="Resolve" , unit="repo" ):
		owner , repo = rec[ "owner" ] , rec[ "repo" ]
		res = rec.get( "resolve" )
		if res is None or force:
			match = gh.resolve_repo( owner , repo )
			if match:
				new_repo , score , method = match
				gh.fetch_repo( owner , new_repo , force=force )   # cache real metadata
				res = {
					"matched": True , "repo": new_repo ,
					"url": f"https://github.com/{owner}/{new_repo}" ,
					"score": round( score , 1 ) , "method": method , "at": papers_db._utc_now_iso() ,
				}
			else:
				res = { "matched": False , "at": papers_db._utc_now_iso() }
			gh.record_resolution( owner , repo , res )
		if res.get( "matched" ):
			n_fixed += 1
			resolved[ ( owner.lower() , repo.lower() ) ] = res[ "url" ]
		else:
			n_none += 1
	print( f"CODE      :: GitHub repair done ( {n_fixed} matched , {n_none} still unresolved )" )

	if not resolved:
		return
	# Pin the corrected URL on every paper link that parsed to a repaired repo.
	n_links = n_papers = 0
	for key , paper in papers_db.iter_all( args ):
		changed = False
		for l in ( ( paper.get( "code" ) or {} ).get( "links" ) ) or []:
			pr = parse_repo( ( l or {} ).get( "url" ) )
			if not pr:
				continue
			new_url = resolved.get( ( pr[ 0 ].lower() , pr[ 1 ].lower() ) )
			if new_url and l.get( "resolved_url" ) != new_url:
				l[ "resolved_url" ] = new_url
				changed = True
				n_links += 1
		if changed:
			try:
				papers_db.save( args , paper )
				n_papers += 1
			except Exception as e:
				print( f"CODE      :: {key}: save failed ( {e} )" )
	print( f"CODE      :: pinned resolved_url on {n_links} link(s) across {n_papers} paper(s)" )


def fetch_osf( args ):
	"""Fetch + cache OSF details ( metadata + Home wiki ) for every UNIQUE OSF
	node among the library's code links , so write_workbook can tag them by
	method. No-op ( with a note ) when config.yaml has no osfio.api_key.
	Idempotent : cached nodes are skipped ( ` --force-download ` re-fetches )."""
	from ..osf.osf import OSF , parse_node
	osf = OSF( args )
	if not osf.configured():
		print(
			"CODE      :: no osfio.api_key in config.yaml -- skipping OSF "
			"enrichment ( OSF links stay un-tagged in code.xlsx )"
		)
		return
	# Re-fetched only under --force-download ; plain --force reuses the cache.
	force = getattr( args , "code_force_download" , False )

	nodes = {}   # guid -> True
	for key , paper in papers_db.iter_all( args ):
		for l in ( ( paper.get( "code" ) or {} ).get( "links" ) ) or []:
			g = parse_node( ( l or {} ).get( "url" ) )
			if g:
				nodes[ g ] = True
	if not nodes:
		print( "CODE      :: no OSF nodes among the code links -- nothing to fetch" )
		return

	print( f"CODE      :: fetching OSF details for {len( nodes )} node(s) -> {osf.cache_dir}" )
	n_ok , n_miss = 0 , 0
	for guid in tqdm( sorted( nodes ) , desc="OSF" , unit="node" ):
		rec = osf.fetch_node( guid , force=force )
		if rec.get( "status" ) == "ok":
			n_ok += 1
		else:
			n_miss += 1
	print( f"CODE      :: OSF fetch done ( {n_ok} ok , {n_miss} missing/error )" )


# ---------------------------------------------------------------------------
# CLI-only : roll every paper's stored links up into output/code/code.xlsx
# ---------------------------------------------------------------------------
# Three sheets :
#   "Links by Paper" -- one row per ( paper , link ) , full paper metadata ;
#   "By Paper"       -- one row per paper , its links collapsed ;
#   "Unique Links"   -- one row per UNIQUE link across the library , with the
#                       DOIs it appears in , a total paper count , and ( for
#                       github repos / OSF nodes ) a column per detected method.
# A method is tagged from two sources : the repo/node README ( ✓ , strong ) and
# the text of the papers that link it -- their OpenAlex abstract + OCR ( · ,
# contextual ) , so a repo whose README never names a modality still inherits it
# from the fMRI / EEG / ... papers citing it. The URL columns show the OCR-REPAIRED
# link where ` resolve_github ` fixed one ; an "OCR Fix" column spells out the
# original mangled name + match confidence so a wrong guess is easy to spot.

_LINKS_HEADERS  = ( "DOI" , "Title" , "Added" , "Published" ,
                    "Source" , "URL" , "Methods" , "Found In" , "OCR Fix" , "Proxy" , "PDF" )
_BYPAPER_HEADERS = ( "DOI" , "Title" , "Added" , "Published" ,
                     "# Links" , "Sources" , "Methods" , "Links" , "Proxy" , "PDF" )
# One column per modality , so the set ( and its order ) comes from
# < --config >/methods.py at run time rather than from a constant here.
def _unique_headers( args ):
	return ( "URL" , "Source" , "Methods" ) + tuple( methods_vocab.labels( args ) ) \
	       + ( "# Papers" , "DOIs" , "OCR Fix" )


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
	the whole library. Reads the 'code' marker ` run ` pinned + the github cache
	` fetch_github ` populated ; call after both. Whole-library : iter_all is NOT
	scoped here ( the CLI leaves args.only_keys unset )."""
	from ..github.github import GitHub , parse_repo
	from ..osf.osf       import OSF , parse_node
	gh  = GitHub( args )
	osf = OSF( args )
	# One read of < --config >/methods.py for the whole workbook : the flag
	# columns and the sort offset below both key off this order.
	method_labels = methods_vocab.labels( args )

	# Memo : a link's detected methods ( from its cached github repo / OSF node ,
	# whichever the URL points at ). Called with the EFFECTIVE ( OCR-repaired )
	# url so a fixed link tags off the corrected repo.
	_mcache = {}
	def methods_for( url ):
		if url not in _mcache:
			pr = parse_repo( url )
			if pr:
				rec = gh.cached( *pr )
			else:
				guid = parse_node( url )
				rec  = osf.cached( guid ) if guid else None
			_mcache[ url ] = detect_methods( args , rec ) if rec else []
		return _mcache[ url ]

	# The "OCR Fix" note for a repaired GitHub link : reads the `resolve` marker
	# resolve_github pinned on the ( broken ) repo's cache record.
	def fix_note( raw_url ):
		pr = parse_repo( raw_url )
		if not pr:
			return ""
		res = ( gh.cached( *pr ) or {} ).get( "resolve" ) or {}
		if not res.get( "matched" ):
			return ""
		return f"{res.get( 'method' , '?' )} {res.get( 'score' , '?' )}% ← {pr[ 0 ]}/{pr[ 1 ]}"

	out_dir = args.output.joinpath( "code" )
	out_dir.mkdir( parents=True , exist_ok=True )
	out_path = out_dir.joinpath( "code.xlsx" )

	link_rows     = []
	by_paper_rows = []
	unique        = {}   # dedup-key -> { url , source , dois:set() , fix }
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
		meta      = _paper_oa_meta( args , doi )
		published = _pub_date( meta )
		pdf_path  = paper.get( "pdf_path" ) or ""

		# Method keywords in THIS paper's own text ( abstract + METHODS section ,
		# not the full OCR -- references would false-positive ) -- every link it
		# carries inherits them , so a repo whose README never says 'fMRI' still
		# gets tagged when the papers citing it are fMRI studies. Computed once.
		text_methods = _paper_text_methods( args , key , meta )

		# Per-paper cells reused across this paper's rows ( read-only ).
		doi_cell   = utils.Link( doi , f"https://doi.org/{doi}" ) if doi else ""
		proxy_url  = _PROXY_URL_TEMPLATE.format( doi=doi ) if doi else ""
		proxy_cell = utils.Link( proxy_url , proxy_url ) if proxy_url else ""
		pdf_cell   = utils.Link( Path( pdf_path ).name , _file_url( pdf_path ) ) if pdf_path else ""

		sources , urls , paper_methods = [] , [] , []
		for l in links:
			url , src = l.get( "url" ) , l.get( "source" ) or ""
			eff  = l.get( "resolved_url" ) or url    # OCR-repaired link if we have one
			note = fix_note( url ) if l.get( "resolved_url" ) else ""
			# Union : what the repo/node README says + what this paper's text says.
			methods = _order_methods( args , set( methods_for( eff ) ) | set( text_methods ) )
			link_rows.append( [
				doi_cell , title , added , published ,
				src , utils.Link( eff , eff ) , " , ".join( methods ) ,
				l.get( "found_in" ) or "" , note , proxy_cell , pdf_cell ,
			] )
			urls.append( eff )
			if src and src not in sources:
				sources.append( src )
			for m in methods:
				if m not in paper_methods:
					paper_methods.append( m )
			u = unique.setdefault( _dedup_key( eff ) ,
				{ "url": eff , "source": src , "dois": set() , "fix": "" , "text_methods": set() } )
			if note and not u[ "fix" ]:
				u[ "fix" ] = note
			u[ "text_methods" ].update( text_methods )
			if doi:
				u[ "dois" ].add( doi )

		by_paper_rows.append( [
			doi_cell , title , added , published ,
			len( urls ) , " , ".join( sources ) ,
			" , ".join( _order_methods( args , paper_methods ) ) , "\n".join( urls ) ,
			proxy_cell , pdf_cell ,
		] )

	# Most-linked papers first ; alphabetical within a tie.
	by_paper_rows.sort( key=lambda r: ( -r[ 4 ] , ( r[ 1 ] or "" ).lower() ) )

	# Unique links : repos / nodes with detected methods first ( so you can eyeball
	# the fMRI / EEG / ... ones up top ) , then by how many papers share them.
	unique_rows = []
	for u in unique.values():
		dois = sorted( u[ "dois" ] )
		# Two provenances : the repo/node README ( strong ) and the linking papers'
		# own text ( contextual ). The flag columns tell them apart -- ✓ = confirmed
		# in the README/metadata , · = inferred only from the citing papers.
		repo_methods = set( methods_for( u[ "url" ] ) )
		text_methods = u[ "text_methods" ]
		all_methods  = repo_methods | text_methods
		flags = [ ( "✓" if m in repo_methods else ( "·" if m in text_methods else "" ) )
		          for m in method_labels ]
		unique_rows.append( [
			utils.Link( u[ "url" ] , u[ "url" ] ) , u[ "source" ] or "" ,
			" , ".join( _order_methods( args , all_methods ) ) , *flags ,
			len( dois ) , "\n".join( dois ) , u.get( "fix" ) or "" ,
		] )
	# Sort : any-method first , then most-shared , then host.
	_n_methods = len( method_labels )
	unique_rows.sort( key=lambda r: (
		0 if r[ 2 ] else 1 ,                      # rows with a method label first
		-r[ 3 + _n_methods ] ,                    # then by # Papers
		( r[ 1 ] or "" ) ,                        # then host
	) )

	utils.write_xlsx( out_path , [
		( "Links by Paper" , _LINKS_HEADERS        , link_rows     ) ,
		( "By Paper"       , _BYPAPER_HEADERS      , by_paper_rows ) ,
		( "Unique Links"   , _unique_headers( args ) , unique_rows ) ,
	] )
	n_tagged = sum( 1 for r in unique_rows if r[ 2 ] )
	n_fixed  = sum( 1 for u in unique.values() if u.get( "fix" ) )
	print(
		f"CODE      :: workbook -> {out_path} "
		f"( {len( link_rows )} links / {len( unique_rows )} unique "
		f"across {n_papers_with} papers ; {n_tagged} repos method-tagged ; "
		f"{n_fixed} OCR-repaired )"
	)
	return out_path


def _order_methods( args , methods ):
	"""Sort a set of matched method labels into the order < --config >/methods.py
	lists them ( so 'fMRI , EEG' always reads the same regardless of match order )."""
	return methods_vocab.order( args , methods )
