"""
prma datasets : harvest PUBLIC-DATASET evidence for every paper in the unified
papers/ DB and pin it on each record at paper[ 'datasets' ] , so the server can
serve the /datasets page.

Sibling of ` prma code ` , and deliberately shaped like it : one scan , stored
once , read by every surface. Where ` prma code ` asks "does this paper ship a
REPO" , this asks "what DATA does it stand on" -- which in this literature is
usually somebody else's public collection , not the authors' own upload.

Two kinds of evidence , because papers give it in two different ways :

  LINKS   a URL ( or a bare host/path , or a DOI ) pointing at a data archive --
          OpenNeuro , NeuroVault , DANDI , OSF , Zenodo , Dryad , Figshare ,
          Hugging Face , PhysioNet , Dataverse , NITRC , CRCNS , EBRAINS ,
          BALSA , ConnectomeDB , LONI / IDA , NDA , Synapse , ... Scanned out of
          the OpenAlex abstract + the OCR full text , through the SAME extractor
          ` prma code ` uses ( src/tasks/code.py ) -- the OCR line-break repair ,
          the plausibility test and the de-dup are hard , and there is one copy
          of them. Only the host table differs : data archives , not code forges
          ( so a GitHub repo is a /code link and not a /datasets one , while a
          Zenodo record is legitimately both ).

  NAMES   the dataset the paper NAMES but never links -- "we use HCP" , "trained
          on NSD" , "ABIDE I" -- matched against the same vocabulary /review
          screens with ( src/review/datasets.py ) , so a paper's datasets read
          identically on /review and /datasets. Read off the RENDERED md with
          the bibliography cut ( a reference list is a list of other papers'
          titles , and half of them name a dataset the paper never touched ) ,
          falling back to the isolated methods section + abstract.

  ACCESSIONS  the OpenNeuro id on its own -- ` ds000105 ` , with no URL around
          it , which is how most papers cite one. Turned into the canonical
          openneuro.org link so the page has something clickable , and marked
          with the accession it was recovered from.

Pipeline : depends on the rendered md ( for NAMES ) , so ` prma md ` is called
inline -- and that call pulls a paper up through snapshot -> yolo -> ocr ->
preprocess -> images -> code on the way , making ` prma datasets ` one-stop the
same way ` prma code ` is. Every inner stage is idempotent ; papers already done
fall through cheaply. This same run() is stage 'datasets' in the per-paper suite
( process.run_suite ) , placed AFTER md so the md it reads exists , so a paper
added under ` prma server --watch ` is scanned automatically.

Idempotency : a scanned paper carries a 'datasets' marker
  paper[ 'datasets' ] = { 'at': <iso> , 'links': [ ... ] , 'names': [ ... ] ,
                          'names_from': 'md' | 'methods' | '' }
( an EMPTY result still counts as scanned -- the marker is the sentinel ) .
Pass --force to re-scan.

No workbook. ` prma code ` writes output/code/code.xlsx because it also enriches
links from the GitHub / OSF APIs and that rollup is the only place the
enrichment lands ; here the scan IS the whole answer , and /datasets exports
whatever you have filtered to , in the order you put it in.
"""

import re

from tqdm import tqdm

from ..db     import papers as papers_db
from ..utils  import utils
from ..pdf    import section_text
from ..review import datasets as ds_vocab
from .        import code as code_task


# ---------------------------------------------------------------------------
# Known data archives -> human label ( the "source" shown in the UI )
# ---------------------------------------------------------------------------
# Same shape and the same first-match-wins rule as code.py's _HOST_LABELS , so
# more-specific subdomains come BEFORE the bare host they sit under.
#
# What belongs here : a host that serves DATA you can go and download. What does
# not : a code forge ( that is /code's table ) , and a journal / preprint host
# ( a paper is not a dataset ). The overlap is real and intended -- Zenodo , OSF ,
# Dryad , Figshare and Hugging Face host both , so those links show up on both
# pages , which is the honest answer about what they are.

_DATA_HOST_LABELS = (
	( "openneuro.org"                   , "OpenNeuro"        ) ,
	( "openfmri.org"                    , "OpenfMRI"         ) ,
	( "neurovault.org"                  , "NeuroVault"       ) ,
	( "dandiarchive.org"                , "DANDI"            ) ,
	( "brainlife.io"                    , "brainlife"        ) ,
	( "fcon_1000.projects.nitrc.org"    , "INDI / FCP-1000"  ) ,
	( "nitrc.org"                       , "NITRC"            ) ,
	( "crcns.org"                       , "CRCNS"            ) ,
	( "physionet.org"                   , "PhysioNet"        ) ,
	( "balsa.wustl.edu"                 , "BALSA"            ) ,
	( "db.humanconnectome.org"          , "ConnectomeDB"     ) ,
	( "humanconnectome.org"             , "ConnectomeDB"     ) ,
	( "adni.loni.usc.edu"               , "ADNI / LONI"      ) ,
	( "ida.loni.usc.edu"                , "LONI IDA"         ) ,
	( "loni.usc.edu"                    , "LONI"             ) ,
	( "nda.nih.gov"                     , "NDA ( NIMH )"     ) ,
	( "abcdstudy.org"                   , "ABCD"             ) ,
	( "ukbiobank.ac.uk"                 , "UK Biobank"       ) ,
	( "oasis-brains.org"                , "OASIS"            ) ,
	( "ppmi-info.org"                   , "PPMI"             ) ,
	( "naturalscenesdataset.org"        , "NSD"              ) ,
	( "things-initiative.org"           , "THINGS"           ) ,
	( "cneuromod.ca"                    , "CNeuroMod"        ) ,
	( "ebrains.eu"                      , "EBRAINS"          ) ,
	( "gin.g-node.org"                  , "G-Node GIN"       ) ,
	( "datalad.org"                     , "DataLad"          ) ,
	( "academictorrents.com"            , "Academic Torrents") ,
	( "data.mendeley.com"               , "Mendeley Data"    ) ,
	( "dataverse.harvard.edu"           , "Harvard Dataverse") ,
	( "dataverse.org"                   , "Dataverse"        ) ,
	( "synapse.org"                     , "Synapse"          ) ,
	# Generalists : code AND data. Also in code.py's table on purpose.
	( "osf.io"                          , "OSF"              ) ,
	( "zenodo.org"                      , "Zenodo"           ) ,
	( "datadryad.org"                   , "Dryad"            ) ,
	( "dryad.org"                       , "Dryad"            ) ,
	( "figshare.com"                    , "Figshare"         ) ,
	( "huggingface.co"                  , "Hugging Face"     ) ,
	( "kaggle.com"                      , "Kaggle"           ) ,
)

# DOI URLs that resolve to a data archive , classified by a substring of the DOI
# path -- so https://doi.org/10.18112/openneuro.ds002685.v1.0.1 is caught as
# OpenNeuro rather than read as just another paper DOI.
_DATA_DOI_HINTS = (
	( "openneuro" , "OpenNeuro" ) ,
	( "18112"     , "OpenNeuro" ) ,   # OpenNeuro's DOI prefix
	( "dandi"     , "DANDI"     ) ,
	( "zenodo"    , "Zenodo"    ) ,
	( "osf.io"    , "OSF"       ) ,
	( "dryad"     , "Dryad"     ) ,
	( "figshare"  , "Figshare"  ) ,
)

# The two halves of code.py's URL machinery , bound to the table above. Built
# once at import ; everything else about the scan -- the OCR line-break repair ,
# the trailing-punctuation trim , the "is this a real record" test , the de-dup
# -- is code.py's and is shared , not copied.
_classify_data_host = code_task.make_classifier( _DATA_HOST_LABELS , _DATA_DOI_HINTS )
_DATA_BARE_RE       = code_task.build_bare_re( _DATA_HOST_LABELS )


# ---------------------------------------------------------------------------
# Bare accessions : the dataset id with no URL around it
# ---------------------------------------------------------------------------
# OpenNeuro accessions are ` ds ` + exactly six digits , and papers cite them
# bare far more often than they link them ( "the data are available on OpenNeuro
# ( ds000105 )" ). The pattern is specific enough to stand alone -- six digits
# glued to a leading ` ds ` is not a word , a year or a p-value -- so we mint the
# canonical record URL from it and mark where it came from.
_ACCESSION_RE = re.compile( r"(?i)\bds\d{6}\b" )
_ACCESSION_URL = "https://openneuro.org/datasets/{acc}"


def _accession_links( *labeled_texts ):
	"""OpenNeuro accessions in the scanned text , as synthesized links. Each
	carries `accession` so the page can say the link was RECOVERED from an id
	rather than copied from something the paper actually printed."""
	seen , out = set() , []
	for found_in , text in labeled_texts:
		if not text:
			continue
		for m in _ACCESSION_RE.finditer( text ):
			acc = m.group( 0 ).lower()
			if acc in seen:
				continue
			seen.add( acc )
			out.append( {
				"url"       : _ACCESSION_URL.format( acc=acc ) ,
				"source"    : "OpenNeuro" ,
				"found_in"  : found_in ,
				"accession" : acc ,
			} )
	return out


def extract_data_links( *labeled_texts ):
	"""Scan ( found_in , text ) pairs for data-archive links , then fold in the
	bare OpenNeuro accessions the URL pass can't see. De-duped across both , on
	code.py's canonical key , so ` ds000105 ` mentioned once as an id and once as
	a full openneuro.org URL is ONE link ( the real URL wins , since the URL pass
	runs first )."""
	links = code_task.extract_links_tagged( *labeled_texts ,
		classify=_classify_data_host , bare_re=_DATA_BARE_RE )
	# An accession is a duplicate whenever it already appears ANYWHERE in a link
	# we found -- as the record path ( openneuro.org/datasets/ds000105 ) or
	# buried in a versioned DOI ( doi.org/10.18112/openneuro.ds003020.v1.0.0 ) --
	# so this is a substring test over the found URLs , not a key comparison.
	found = " ".join( ( l.get( "url" ) or "" ).lower() for l in links )
	for l in _accession_links( *labeled_texts ):
		if l[ "accession" ] not in found:
			links.append( l )
	return links


# ---------------------------------------------------------------------------
# Per-paper text : the same three sources the rest of prma reads
# ---------------------------------------------------------------------------

def _md_text( args , key ):
	"""The paper's rendered md ( ` prma md ` ) with the bibliography cut , or ""
	when it was never rendered. strip_refs is /review's , so both surfaces stop
	reading at the same line."""
	from ..review.build import strip_refs
	prefix = utils.doi_to_filename( key )
	if not prefix:
		return ""
	fp = args.output.joinpath( "md" , f"{prefix}.md" )
	if not fp.exists():
		return ""
	try:
		return strip_refs( fp.read_text( encoding="utf-8" , errors="replace" ) )
	except Exception:
		return ""


def _methods_text( args , key ):
	"""The isolated methods section ( ` prma methods ` , or the '## Methods'
	slice of the md ) -- the fallback for naming a dataset when nothing rendered ,
	and the one part of a paper that always says which data it ran on."""
	try:
		return section_text.resolve_section_text( args , key , "methods" ) or ""
	except Exception:
		return ""


def paper_datasets( args , key , paper ):
	"""Scan one paper and return ( links , names , names_from ) -- what gets
	stored under paper[ 'datasets' ].

	LINKS come from the abstract + the OCR full text , exactly like ` prma code `
	( a data URL is just as likely to be printed in a data-availability
	statement the md never got as in the body ). NAMES come from the md minus
	its bibliography , or the methods section when there is no md : a dataset
	NAME is a common English-ish token and the reference list is full of them ,
	so the name pass gets the narrower , cleaner text on purpose."""
	doi      = utils.normalize_doi( paper.get( "doi" ) )
	abstract = code_task._paper_abstract( args , doi )
	ocr_text = code_task._ocr_fulltext( paper )
	links    = extract_data_links( ( "abstract" , abstract ) , ( "ocr" , ocr_text ) )

	md = _md_text( args , key )
	if md:
		names , names_from = ds_vocab.detect( f"{md}\n{abstract}" ) , "md"
	else:
		methods = _methods_text( args , key )
		names , names_from = ( ( ds_vocab.detect( f"{methods}\n{abstract}" ) , "methods" )
			if methods else ( [] , "" ) )
	return links , sorted( names ) , names_from


def display_links( paper ):
	"""What a BROWSER needs off paper[ 'datasets' ] : the stored links compacted
	to { url , source } pairs ( + `accession` where the link was minted from a
	bare id ) , ready for the /datasets page's renderer.

	The one reader for this field -- the same contract code.display_links has ,
	and for the same reason : the page , the index entry and the export all come
	through here , so no two of them can claim different data for a paper.
	[] when the paper hasn't been scanned or had no links."""
	out = []
	for l in ( ( paper.get( "datasets" ) or {} ).get( "links" ) ) or []:
		url = l.get( "url" )
		if not url:
			continue
		entry = { "url": url , "source": l.get( "source" ) }
		if l.get( "accession" ):
			entry[ "accession" ] = l[ "accession" ]
		out.append( entry )
	return out


def display_names( paper ):
	"""The public datasets the paper NAMES , off the same field. [] when unscanned."""
	return list( ( ( paper.get( "datasets" ) or {} ).get( "names" ) ) or [] )


# ---------------------------------------------------------------------------
# The stage : scan each scoped paper and pin paper[ 'datasets' ]
# ---------------------------------------------------------------------------

def run( args ):
	"""Scan every in-scope paper for public-dataset evidence and store it on the
	record. Honors args.only_keys ( so process.run_suite / the --watch worker can
	scope it to one paper ) , --manager , and --force.

	` prma md ` is ensured inline ( idempotent , and it pulls papers up through
	yolo -> ocr -> preprocess -> images -> code on the way ) so a standalone
	` prma datasets ` works end-to-end ; inside run_suite md already ran as the
	stage before this one and the call is a cheap no-op."""
	from . import md as md_task
	md_task.run( args )

	force         = getattr( args , "datasets_force" , False )
	managers      = code_task._resolve_managers( args )
	manager_label = " + ".join( managers ) if managers else "all"

	# Plan : which papers still need scanning.
	jobs , skip_done , skip_other = [] , 0 , 0
	for key , paper in papers_db.iter_all( args ):
		if not code_task._paper_matches_managers( paper , managers ):
			skip_other += 1
			continue
		if not force and paper.get( "datasets" ) is not None:
			skip_done += 1
			continue
		jobs.append( key )

	print(
		f"DATASETS  :: ({manager_label})  {len(jobs)} papers to scan "
		f"( skipped: already-done={skip_done} other-manager={skip_other} )"
	)

	n_with , n_links , n_names = 0 , 0 , 0
	for key in tqdm( jobs , desc="Papers" , unit="paper" ):
		paper = papers_db.load( args , key )
		if paper is None:
			continue
		try:
			links , names , names_from = paper_datasets( args , key , paper )
		except Exception as e:
			print( f"DATASETS  :: {key}: scan failed ( {e} )" )
			continue
		paper[ "datasets" ] = { "at": papers_db._utc_now_iso() , "links": links ,
			"names": names , "names_from": names_from }
		try:
			papers_db.save( args , paper )
		except Exception as e:
			print( f"DATASETS  :: {key}: save failed ( {e} )" )
			continue
		if links or names:
			n_with  += 1
			n_links += len( links )
			n_names += len( names )

	print(
		f"DATASETS  :: scanned {len(jobs)} papers -- "
		f"{n_with} stand on public data ( {n_links} archive links , "
		f"{n_names} named datasets )"
	)


# ---------------------------------------------------------------------------
# Record enrichment : what IS the thing at the other end of the link
# ---------------------------------------------------------------------------
# The scan above says a paper points at ` openneuro.org/datasets/ds003020 ` . It
# does not say that this is "An fMRI dataset during a passive natural language
# listening task" -- and on a page with one row per collection , that sentence is
# the whole point. So : fetch each UNIQUE record once , cache it , and let every
# surface read the cache.
#
# Same shape as ` prma code `'s fetch_github / fetch_osf , and CLI-ONLY for the
# same reason : the per-paper suite is scoped to one paper and must not reach the
# network , so the ` datasets ` stage only ever SCANS. Fetching happens when you
# run ` prma datasets ` yourself.
#
# FOUR APIS AND A FALLBACK , in that order , because the APIs give a clean answer
# and the fallback gives a page title :
#
#   Zenodo     zenodo.org/api/records/<id>            metadata.title
#   Figshare   api.figshare.com/v2/articles/<id>      title
#   OpenNeuro  openneuro.org/crn/graphql              draft.description.Name
#   OSF        api.osf.io/v2/nodes/<guid>/            attributes.title
#   anything   GET the page , read <title>
#
# OSF is on that list specifically BECAUSE the fallback fails there : osf.io
# renders client-side and every node's <title> is the bare word "OSF" . The
# others are on it because an API answer beats a title with the site's name
# bolted onto it. Everything else -- NITRC , NeuroVault , Dryad , PhysioNet ,
# Hugging Face , the INDI directories , ConnectomeDB -- answers the page-title
# question perfectly well , which is why there is no host table here : the
# fallback is the rule and the APIs are the exceptions.
#
# None of these need a key.

_RECORDS_DIR = ( "cache" , "datasets" )

# An HONEST User-Agent , and not only on principle : a spoofed Chrome string is
# what BREAKS this. Zenodo's API answers 200 to ` curl ` and to the string below
# and 403s a pretended Chrome -- their anti-bot rule reads a browser UA on an API
# endpoint as exactly the lie it is. Every other host here ( Figshare , OSF ,
# NITRC , NeuroVault , Dryad , PhysioNet , Hugging Face , ConnectomeDB , LONI )
# answers 200 to both , so there is nothing to trade away.
_UA = "prma/1.0 (paper-reference-manager-addons; +https://github.com/0187773933)"

_TITLE_RE = re.compile( r"<title[^>]*>(.*?)</title>" , re.S | re.I )

# A 200 is not the same as an answer. These are the titles a live page hands back
# when there is nothing behind the URL -- a soft 404 , a directory index , or the
# site's own name because the real title is rendered by JavaScript we don't run.
_JUNK_TITLE = re.compile(
	r"(?i)^\s*(?:"
	r"index of\b.*"                                              # an Apache directory listing
	r"|osf|zenodo|figshare|openneuro|dryad|nitrc|neurovault"      # bare site names
	r")\s*$" )
# ` not found ` anywhere in a title is a soft 404 wearing a 200 -- and no real
# dataset is called that. Searched rather than anchored , because the archives
# decorate it ( "NITRC: Requested Page not Found (Error 404)" ) and _TITLE_TRIMS
# has already eaten the prefix that would have anchored it.
_NOT_FOUND_TITLE = re.compile( r"(?i)\bnot\s+found\b|\berror\s*40\d\b|\b40[34]\b" )

# Cosmetics the archives glue onto every title. Stripping them is not tidying for
# its own sake : the row already has an Archive column saying OSF / Dryad /
# Hugging Face , so repeating it inside the description wastes the line.
_TITLE_TRIMS = (
	( re.compile( r"(?i)^dryad\s*\|\s*data:\s*" )              , "" ) ,
	( re.compile( r"(?i)\s*·\s*datasets? at hugging face\s*$" ) , "" ) ,
	( re.compile( r"(?i)^nitrc:\s*" )                          , "" ) ,
	( re.compile( r"(?i):\s*tool/resource info\s*$" )          , "" ) ,
	( re.compile( r"(?i)\s*[-|]\s*openneuro\s*$" )             , "" ) ,
)


def _records_dir( args ):
	d = args.output.joinpath( *_RECORDS_DIR )
	d.mkdir( parents=True , exist_ok=True )
	return d


def _record_path( args , url ):
	"""One cache file per record , named by a hash of the CANONICAL url -- so
	` http://www.osf.io/x/ ` and ` https://osf.io/x ` are one entry , the same way
	the scan de-dupes them. The url itself is stored inside the file , since the
	filename can't carry it."""
	import hashlib
	key = code_task._dedup_key( url )
	return _records_dir( args ).joinpath( hashlib.sha1( key.encode() ).hexdigest() + ".json" )


def _tidy_title( raw ):
	"""One line of prose out of whatever the archive handed back , or "" when what
	it handed back was not an answer."""
	import html as _html
	t = " ".join( _html.unescape( re.sub( r"<[^>]+>" , " " , raw or "" ) ).split() )
	for rx , sub in _TITLE_TRIMS:
		t = rx.sub( sub , t ).strip()
	if not t or _JUNK_TITLE.match( t ) or _NOT_FOUND_TITLE.search( t ):
		return ""
	return t[ :300 ]


def _api_target( url ):
	"""( api-name , id ) when a url is one an archive API can answer for , else
	( "" , "" ) and the page-title fallback takes it."""
	u = ( url or "" ).lower()
	if "zenodo" in u:
		m = re.search( r"zenodo[./](\d+)" , u )
		return ( "zenodo" , m.group( 1 ) ) if m else ( "" , "" )
	if "figshare" in u:
		m = ( re.search( r"figshare\.(\d+)" , u )
			or re.search( r"/articles/(?:[^/]+/){0,2}(\d+)" , u ) )
		return ( "figshare" , m.group( 1 ) ) if m else ( "" , "" )
	if "openneuro" in u:
		m = re.search( r"(ds\d{6})" , u )
		return ( "openneuro" , m.group( 1 ) ) if m else ( "" , "" )
	if "osf.io" in u:
		try:
			from ..osf.osf import parse_node
			g = parse_node( url )
		except Exception:
			g = ""
		return ( "osf" , g ) if g else ( "" , "" )
	return ( "" , "" )


def _retryable( status ):
	"""Is an empty answer worth ASKING AGAIN later? A 404 is a fact about the
	link and caching it saves a request forever. A timeout , a 5xx or a
	rate-limit is a fact about right now , and caching THAT would silently
	blacklist a perfectly good record for good."""
	return status == 0 or status == 429 or status >= 500


def _fetch_title( session , url ):
	"""( title , via , http-status , retryable ) for one record. Never raises :
	a dead link , a timeout and a page with nothing to say all come back as an
	empty title , because a missing description is a blank line and not an
	error -- but only the DEAD one is worth remembering ( see _retryable )."""
	api , ident = _api_target( url )
	try:
		if api == "zenodo":
			r = session.get( f"https://zenodo.org/api/records/{ident}" , timeout=12 )
			t = _tidy_title( ( r.json().get( "metadata" ) or {} ).get( "title" ) ) if r.ok else ""
			return t , "zenodo api" , r.status_code , _retryable( r.status_code )
		if api == "figshare":
			r = session.get( f"https://api.figshare.com/v2/articles/{ident}" , timeout=12 )
			t = _tidy_title( r.json().get( "title" ) ) if r.ok else ""
			return t , "figshare api" , r.status_code , _retryable( r.status_code )
		if api == "openneuro":
			r = session.post( "https://openneuro.org/crn/graphql" , timeout=12 ,
				json={ "query": '{dataset(id:"%s"){draft{description{Name}}}}' % ident } )
			name , soft_fail = "" , False
			if r.ok:
				body = r.json()
				# GraphQL answers 200 and puts the failure in the body. An
				# ` errors ` array with no data is OpenNeuro having a bad minute
				# ( "fetch failed" / INTERNAL_SERVER_ERROR ) , not a missing
				# dataset -- observed live on a record that resolves fine
				# either side of it. Retry that ; don't bury it in the cache.
				d = ( body.get( "data" ) or {} ).get( "dataset" ) or {}
				name = ( ( d.get( "draft" ) or {} ).get( "description" ) or {} ).get( "Name" ) or ""
				soft_fail = bool( body.get( "errors" ) ) and not name
			return ( _tidy_title( name ) , "openneuro graphql" , r.status_code ,
				soft_fail or _retryable( r.status_code ) )
		if api == "osf":
			# The page-title fallback is useless here -- osf.io renders in the
			# browser and every node's <title> is the bare word "OSF".
			r = session.get( f"https://api.osf.io/v2/nodes/{ident}/" , timeout=12 )
			t = _tidy_title( ( ( r.json().get( "data" ) or {} ).get( "attributes" ) or {} ).get( "title" ) ) if r.ok else ""
			return t , "osf api" , r.status_code , _retryable( r.status_code )
		r = session.get( url , timeout=12 , allow_redirects=True )
		if not r.ok:
			return "" , "page title" , r.status_code , _retryable( r.status_code )
		m = _TITLE_RE.search( r.text )
		return ( _tidy_title( m.group( 1 ) ) if m else "" ) , "page title" , r.status_code , False
	except Exception as e:
		return "" , f"error: {type( e ).__name__}" , 0 , True


def cached_record( args , url ):
	"""The cached title for one record url , or "" . The one reader for the cache
	-- the /datasets page goes through the server , which goes through here."""
	fp = _record_path( args , url )
	if not fp.exists():
		return ""
	try:
		return ( utils.read_json( fp ) or {} ).get( "title" ) or ""
	except Exception:
		return ""


def fetch_records( args ):
	"""Fetch + cache a human title for every UNIQUE data-archive record among the
	library's pinned dataset links , so /datasets can say what each collection
	IS rather than only where it lives.

	Idempotent : a url already in the cache is skipped , INCLUDING one cached
	with an empty title ( a 404 stays a 404 -- re-asking every run would be a
	slow way to learn nothing ). ` --force-download ` re-fetches everything ;
	` --no-fetch ` skips this entirely."""
	if getattr( args , "datasets_no_fetch" , False ):
		print( "DATASETS  :: --no-fetch -- skipping record titles "
		       "( rows will show their URL and no description )" )
		return
	import time
	import requests

	force = getattr( args , "datasets_force_download" , False )

	urls = {}
	for key , paper in papers_db.iter_all( args ):
		for l in ( ( paper.get( "datasets" ) or {} ).get( "links" ) ) or []:
			u = ( l or {} ).get( "url" )
			if u:
				urls[ code_task._dedup_key( u ) ] = u
	if not urls:
		print( "DATASETS  :: no archive links pinned yet -- nothing to fetch" )
		return

	todo = [ u for u in sorted( urls.values() )
		if force or not _record_path( args , u ).exists() ]
	print( f"DATASETS  :: {len( urls )} unique records ; {len( todo )} to fetch "
	       f"-> {_records_dir( args )}" )
	if not todo:
		return

	session = requests.Session()
	session.headers.update( { "User-Agent": _UA } )
	n_ok , n_blank , n_retry = 0 , 0 , 0
	for u in tqdm( todo , desc="Records" , unit="record" ):
		title , via , status , retry = _fetch_title( session , u )
		if not title and retry:
			# Nothing to say , but only for now -- leave the cache alone so the
			# next run asks again rather than remembering a bad minute forever.
			n_retry += 1
			time.sleep( 0.5 )
			continue
		try:
			utils.write_json( _record_path( args , u ) , {
				"url": u , "title": title , "via": via , "http": status ,
				"fetched_at": papers_db._utc_now_iso() ,
			} )
		except Exception as e:
			print( f"DATASETS  :: {u}: cache write failed ( {e} )" )
			continue
		if title:
			n_ok += 1
		else:
			n_blank += 1
		time.sleep( 0.5 )         # the archives are free ; don't hammer them

	print( f"DATASETS  :: record titles -- {n_ok} described , "
	       f"{n_blank} dead or nameless ( cached , not re-asked )" +
	       ( f" , {n_retry} temporarily unreachable ( will retry next run )" if n_retry else "" ) )
