#!/usr/bin/env python3
"""
Append the papers in a figure-selection CSV ( the `image` / `caption` export ,
e.g. /Users/morpheous/TMP2/images-selected.csv ) to the /sort board as fully
formed rows -- tagged , with their Code / Datasets cells prefilled -- skipping
every paper the board already carries.

The CSV is FIGURE-level : one row per selected figure , `id` spelled
"<paper key>#figure-N". So the first thing this does is collapse it to one
record per paper , unioning the `modalities` column and collecting every
`caption`.

Why it talks to the running server instead of writing output/cache/sort.json :
BoardState.load() ( src/server/server.py ) runs only at STARTUP and
BoardState.snapshot() serves the in-memory document , so a file written
underneath a live server is both invisible to it and first in line to be
overwritten by the next browser save. POST /api/sort is the supported write --
it re-checks the view-only lock , snapshots the previous version into
output/cache/sort-history/ , and bumps `rev` so open tabs reload on their next
version poll.

Auth : the read ( GET /api/sort ) and the enrichment ( POST /api/paper-meta ,
in auth.ANON_POSTS ) are anonymous ; only the final write needs role `user` ,
which means an API key -- ` prma auth create-key <account> --key-name … ` . A
Bearer key skips the CSRF check , which is gated on cookie sessions
( src/server/auth.py ) , so the token is the whole credential. Pass it in
PRMA_API_KEY or --key , and revoke it afterwards.

Tags come from two places and NEVER from anywhere else :
  * the ` prma modalities ` stamp , via paper-meta's `mods` -- the same eight
    values clicking a search hit into the board gives you today ;
  * the figure CAPTIONS plus the title , matched against TAG_PATTERNS below.
Every emitted tag must already exist in the board's own vocab.tags , spelled
the vocab's way ; anything else is dropped and reported. The board has
inherited near-duplicate chips already ( "covert speech" / "covert-speech" ,
"inner speech" / "inner-speech" ) and this must not add more.

Deliberately NOT read : output/methods/<key>.txt . Folding the Methods section
in roughly triples the hit rate on the generic patterns ( CNN 26 -> 88 ,
ROI 14 -> 79 , time-series 17 -> 83 , open-data 0 -> 54 ) because a Methods
section that names a CNN baseline is not a CNN paper. Captions land at ~4.2
tags/row against the board's hand-curated 5.43 , which is the right side to
err on.

Idempotent : de-duplication uses the same alias map the page's own sheet import
does ( sort.html , `known` ) -- key , DOI , the key AS a DOI , zotero id ,
normalized title -- so a second run finds nothing to add.

Usage :
  python tools/import-selected-csv.py CSV --dry-run       # report , write nothing
  python tools/import-selected-csv.py CSV --key <token>   # do it
  PRMA_API_KEY=<token> python tools/import-selected-csv.py CSV
  python tools/import-selected-csv.py CSV --base http://127.0.0.1:9371
  python tools/import-selected-csv.py CSV --dry-run --show-tags   # per-row tags
"""

from __future__ import annotations

import argparse
import collections
import csv
import difflib
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime , timezone
from pathlib import Path

_REPO = Path( __file__ ).resolve().parent.parent
if str( _REPO ) not in sys.path:
	sys.path.insert( 0 , str( _REPO ) )

DEFAULT_BASE = "http://127.0.0.1:9371"
# The per-call cap in the /api/paper-meta handler ( PaperMeta.meta slices to 500 ).
META_BATCH   = 500

# One known duplicate identity. Board row 192 arrived through the .docx
# reference import -- src/db/refparse.py mints "ref-<title slug>" for a
# reference that carries no DOI -- so it has no doi , wid or year. It is the
# same paper as the CSV key on the right. Neither the alias map nor
# PaperMeta._resolve finds it by title , because the library's own title for
# that paper carries a trailing " - ScienceDirect" . Adopting the key is what
# adoptKey ( sort.html ) does after a meta lookup , and it is what lets /pdf ,
# /md , /code and /review find the paper at all. Checked against the titles at
# runtime before anything is written ; the row does not move and keeps its own
# tags and cells.
REKEY = {
	"ref-multimodal-deep-neural-decoding-reveals-highly-resolved-spatiotemporal-profile-o":
		"10.1016/j.neuroimage.2023.120164" ,
}

# ---------------------------------------------------------------------------
# The tag table : caption / title text -> a tag the vocab already has.
#
# Written for PRECISION over recall. A figure caption is a strong signal of
# what a paper's method IS ( the architecture diagram names it ) and a weak
# signal of everything a paper merely mentions , so patterns here name
# architectures , tasks , stimuli and populations rather than anything that
# turns up in passing. Add to it freely ; the vocab guard below will tell you
# if a tag you reached for isn't a chip yet.
# ---------------------------------------------------------------------------
TAG_PATTERNS = [
	# -- architectures / models ------------------------------------------
	( "transformer"          , r"\btransformers?\b|\bvision transformer\b|\bViT\b|\bself-?attention\b|\bmulti-?head attention\b|\bBERT\b|\bencoder-?decoder\b" ) ,
	( "CNN"                  , r"\bCNNs?\b|\bconvolutional\b|\bconv\s?(?:layer|block|1d|2d|3d)\b|\bResNet\b|\bU-?Net\b|\bVGG\b|\bInception\b" ) ,
	( "llm"                  , r"\bLLMs?\b|\blarge language model|\bGPT-?[234]?\b|\bLLaMA\b|\bChatGPT\b" ) ,
	( "lstm"                 , r"\bLSTM\b|\bBi-?LSTM\b|\blong short-?term memory\b" ) ,
	( "RNN"                  , r"\bRNNs?\b|\brecurrent neural net" ) ,
	( "GRU"                  , r"\bGRU\b|\bgated recurrent\b" ) ,
	( "Mamba"                , r"\bMamba\b|\bselective state space\b|\bSSM\b" ) ,
	( "state-space"          , r"\bstate[- ]space model" ) ,
	( "diffusion"            , r"\bdiffusion (?:model|transformer|prior|process|network)|\bdenoising diffusion\b|\blatent diffusion\b|\bDDPM\b|\bDiT\b|\bstable diffusion\b" ) ,
	( "GAN"                  , r"\bGANs?\b|\bgenerative adversarial\b" ) ,
	( "vae"                  , r"\bVAE\b|\bvariational auto-?encoder\b" ) ,
	( "CLIP"                 , r"\bCLIP\b(?!\s*art)" ) ,
	( "gcn"                  , r"\bGCN\b|\bgraph convolution" ) ,
	( "graph-network"        , r"\bgraph neural net|\bGNN\b|\bgraph attention\b|\bGAT\b" ) ,
	( "SVM"                  , r"\bSVM\b|\bsupport vector\b" ) ,
	( "KNN"                  , r"\bk-?NN\b|\bk-?nearest neighb" ) ,
	( "ridge"                , r"\bridge regression\b|\bridge model\b" ) ,
	( "GLM"                  , r"\bGLM\b|\bgeneral linear model\b" ) ,
	( "ICA"                  , r"\bICA\b|\bindependent component analysis\b" ) ,
	( "MVPA"                 , r"\bMVPA\b|\bmultivariate pattern (?:analysis|classif)|\bsearchlight\b|\bRSA\b|\brepresentational similarity\b" ) ,
	( "machine-learning"     , r"\bmachine learning\b" ) ,
	# -- what the model is FOR -------------------------------------------
	( "decoding"             , r"\bdecod(?:e|ing|er)\b|\bbrain-?to-?(?:text|image|speech)\b" ) ,
	( "encoding-models"      , r"\bencoding model|\bvoxel-?wise encoding\b|\bforward model\b" ) ,
	( "classification"       , r"\bclassif(?:y|ier|ication)\b|\bconfusion matrix\b" ) ,
	( "reconstruction"       , r"\breconstruct(?:ion|ed|ing)\b" ) ,
	( "captioning"           , r"\bcaption(?:ing|s)?\b" ) ,
	# -- how it was trained ----------------------------------------------
	( "pretraining"          , r"\bpre-?train(?:ed|ing)\b|\bself-?supervised\b|\bmasked (?:modeling|autoencod|patch)" ) ,
	( "fine-tuning"          , r"\bfine-?tun(?:e|ed|ing)\b" ) ,
	( "transfer-learning"    , r"\btransfer learning\b" ) ,
	( "foundation-model"     , r"\bfoundation model" ) ,
	( "zero-shot"            , r"\bzero-?shot\b" ) ,
	( "cross-subject"        , r"\bcross-?subject\b|\bleave-?one-?subject-?out\b|\binter-?subject\b|\bsubject-?independent\b" ) ,
	# -- signal / acquisition --------------------------------------------
	( "resting-state"        , r"\bresting[- ]state\b|\brs-?fMRI\b" ) ,
	( "functional-connectivity" , r"\bfunctional connectivity\b|\bFNC\b|\bconnectome\b" ) ,
	( "time-series"          , r"\btime[- ]series\b|\btemporal dynamics\b" ) ,
	( "real-time"            , r"\breal-?time\b|\bonline decoding\b" ) ,
	( "ROI"                  , r"\bROIs?\b|\bregions? of interest\b" ) ,
	( "7t-fmri"              , r"\b7\s?T\b|\b7-?Tesla\b" ) ,
	( "preprocessing"        , r"\bpre-?processing (?:pipeline|steps)\b" ) ,
	( "fusion"               , r"\bfus(?:ion|ing|ed)\b" ) ,
	( "multimodal"           , r"\bmulti-?modal\b" ) ,
	( "network-analysis"     , r"\bgraph theor|\bnetwork (?:metrics|topology)\b" ) ,
	# -- language / stimulus ---------------------------------------------
	( "semantic"             , r"\bsemantic\b" ) ,
	( "phonology"            , r"\bphonem|\bphonolog" ) ,
	( "syllable"             , r"\bsyllabl" ) ,
	( "word"                 , r"\bsingle[- ]words?\b|\bword-?level\b" ) ,
	( "spelling"             , r"\bspell(?:ing|er)\b" ) ,
	( "narrative"            , r"\bnarrativ|\bstory\b|\bstories\b" ) ,
	( "reading"              , r"\breading\b|\bread (?:aloud|silently)\b" ) ,
	( "vision"               , r"\bvisual stimuli\b|\bimage stimuli\b|\bnatural images?\b|\bvisual cortex\b" ) ,
	( "image-evoked"         , r"\bimage-?evoked\b|\bviewing images\b|\bpresented images\b" ) ,
	( "video-evoked"         , r"\bvideo (?:clips?|stimuli)\b|\bmovie (?:clips?|watching)\b" ) ,
	( "audio-listening"      , r"\blisten(?:ing|ed)\b|\baudiobook\b|\bauditory stimuli\b|\bspeech (?:audio|sound)\b" ) ,
	( "music"                , r"\bmusic(?:al)?\b" ) ,
	( "conversation"         , r"\bconversation|\bdialog" ) ,
	# -- speech ----------------------------------------------------------
	( "speech-production"    , r"\bspeech production\b|\bspoken production\b|\barticulat" ) ,
	( "overt-speech"         , r"\bovert speech\b|\bovertly\b|\bspoken aloud\b" ) ,
	( "covert-speech"        , r"\bcovert speech\b|\bcovertly\b" ) ,
	( "inner-speech"         , r"\binner speech\b" ) ,
	( "imagined-speech"      , r"\bimagined speech\b|\bspeech imagery\b" ) ,
	( "silent-speech"        , r"\bsilent speech\b" ) ,
	( "verbal-fluency"       , r"\bverbal fluency\b" ) ,
	( "naming"               , r"\bpicture naming\b|\bobject naming\b" ) ,
	( "bci"                  , r"\bBCIs?\b|\bbrain-?computer interface" ) ,
	( "neuroprosthesis"      , r"\bneuroprosthe|\bspeech prosthe" ) ,
	# -- who was scanned -------------------------------------------------
	( "clinical"             , r"\bpatients?\b|\bdisorder\b|\bdiagnos|\bschizophren|\bautism\b|\bAlzheimer|\bepilep|\bdepress|\baphasi|\bParkinson" ) ,
	( "aging"                , r"\baging\b|\bolder adults?\b" ) ,
	( "development"          , r"\bchildren\b|\badolescen|\binfants?\b|\bdevelopmental\b" ) ,
	( "bilingual"            , r"\bbilingual|\bL2 speakers?\b|\bsecond language\b" ) ,
	# -- task ------------------------------------------------------------
	( "emotion"              , r"\bemotion(?:al)?\b|\bvalence\b|\baffective\b" ) ,
	( "working-memory"       , r"\bworking memory\b|\bn-?back\b" ) ,
	( "motor"                , r"\bmotor (?:cortex|task|movement)\b|\bhand movement\b" ) ,
	( "motor-imagery"        , r"\bmotor imagery\b" ) ,
	# -- what kind of paper ----------------------------------------------
	( "open-data"            , r"\bpublicly available\b|\bopen(?:ly)? (?:available|access)\b" ) ,
	( "dataset"              , r"\bdataset\b.*\b(?:we (?:collect|release)|introduce|present)\b|\bbenchmark dataset\b" ) ,
	( "benchmark"            , r"\bbenchmark" ) ,
	( "review"               , r"\bthis review\b|\bsystematic review\b|\bPRISMA\b" ) ,
	( "meta-analysis"        , r"\bmeta-?analysis\b" ) ,
	( "simulation"           , r"\bsimulat(?:ion|ed)\b" ) ,
]
_TAG_RX = [ ( tag , re.compile( pat , re.I ) ) for tag , pat in TAG_PATTERNS ]


# ---------------------------------------------------------------------------
# Helpers ported verbatim from the page , so a row built here is byte-for-byte
# a row the page would have built : foldText ( static/common.js ) , ntitle ,
# cellText and linkLine ( sort.html ).
# ---------------------------------------------------------------------------

# Titles and captions are TYPESET : "Brain\u2013Computer Interface" carries an en
# dash , "multi\u2010modal" a hyphenation hyphen , and a pattern written with a
# plain "-" misses both. Folded once , up front , rather than spelled into
# eighty regexes.
_DASHES = dict.fromkeys( [ 0x2010 , 0x2011 , 0x2012 , 0x2013 , 0x2014 , 0x2015 , 0x2212 ] , "-" )


def plain_dashes( s ):
	return str( s or "" ).translate( _DASHES )


def fold_text( s ):
	"""normalize_title with the spaces dropped too -- only letters and digits
	get compared. ( static/common.js foldText. )"""
	s = unicodedata.normalize( "NFKD" , str( s or "" ) ).lower()
	return re.sub( r"[^a-z0-9]+" , "" , s )


def ntitle( s ):
	"""A title with case and punctuation gone , words still separated.
	( sort.html ntitle -- the "t:" alias in the import de-dup map. )"""
	s = unicodedata.normalize( "NFKD" , str( s or "" ) ).lower()
	return re.sub( r"[^a-z0-9]+" , " " , s ).strip()


def cell_text( values ):
	"""One line each , first spelling wins : the scans often find the same link
	twice ( the abstract and the full text both print it ). ( sort.html
	cellText. )"""
	seen , out = set() , []
	for v in values:
		v = str( v or "" ).strip()
		if v and v.lower() not in seen:
			seen.add( v.lower() )
			out.append( v )
	return "\n".join( out )


def link_line( label , url ):
	"""A named dataset as one cell line : just the URL when the name is already
	spelled out in it , otherwise "NAME — URL". ( sort.html linkLine. )"""
	label = str( label or "" ).strip()
	u     = fold_text( url )
	words = [ w for w in re.split( r"[^a-z0-9]+" ,
		re.sub( r"^hf:" , "" , unicodedata.normalize( "NFKD" , label ).lower() ) )
		if w and w not in ( "www" , "http" , "https" ) ]
	return url if all( w in u for w in words ) else f"{label} — {url}"


def now_iso():
	"""nowISO() -- the page writes an ISO string with the Z , via
	Date.toISOString()."""
	return datetime.now( timezone.utc ).strftime( "%Y-%m-%dT%H:%M:%S.%f" )[ :-3 ] + "Z"


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------

def _call( base , path , body=None , token=None , timeout=120 ):
	url = base.rstrip( "/" ) + path
	data = None
	head = { "Accept": "application/json" }
	if body is not None:
		data = json.dumps( body ).encode( "utf-8" )
		head[ "Content-Type" ] = "application/json"
	if token:
		head[ "Authorization" ] = f"Bearer {token}"
	req = urllib.request.Request( url , data=data , headers=head ,
		method="POST" if data is not None else "GET" )
	try:
		with urllib.request.urlopen( req , timeout=timeout ) as r:
			return json.loads( r.read().decode( "utf-8" , errors="replace" ) )
	except urllib.error.HTTPError as e:
		raw = e.read().decode( "utf-8" , errors="replace" )
		try:
			return { "_status": e.code , **json.loads( raw ) }
		except Exception:
			return { "_status": e.code , "ok": False , "error": raw[ :400 ] }


def fetch_meta( base , keys ):
	"""PaperMeta for every key , in handler-sized batches , asking for the
	modality stamp and the code / data links -- which is exactly what
	addPaper's own fetchMeta( keys , true ) asks for."""
	out = {}
	for i in range( 0 , len( keys ) , META_BATCH ):
		chunk = keys[ i : i + META_BATCH ]
		res   = _call( base , "/api/paper-meta" ,
			{ "keys": chunk , "mods": True , "links": True } )
		if not res.get( "ok" ):
			raise SystemExit( f"paper-meta failed : {res.get( 'error' ) or res}" )
		out.update( res.get( "meta" ) or {} )
	return out


# ---------------------------------------------------------------------------
# The CSV
# ---------------------------------------------------------------------------

def read_selection( path ):
	"""The figure-level CSV collapsed to one record per paper : key , the title
	it was exported with , the union of its `modalities` , and every non-empty
	`caption`."""
	papers = {}
	with open( path , encoding="utf-8-sig" , newline="" ) as fh:
		for row in csv.DictReader( fh ):
			key = ( row.get( "id" ) or "" ).split( "#" )[ 0 ].strip()
			if not key:
				continue
			p = papers.setdefault( key , {
				"key": key , "title": ( row.get( "title" ) or "" ).strip() ,
				"doi": ( row.get( "doi" ) or "" ).strip() ,
				"mods": set() , "captions": [] , "figures": 0 ,
			} )
			for m in ( row.get( "modalities" ) or "" ).split( ";" ):
				m = m.strip()
				if m:
					p[ "mods" ].add( m )
			cap = ( row.get( "caption" ) or "" ).strip()
			if cap:
				p[ "captions" ].append( cap )
			p[ "figures" ] += 1
	return papers


def alias_index( doc ):
	"""key / DOI / zotero id / title -> the row that already answers to it , the
	same map the page's sheet import builds ( sort.html `known` ). The list goes
	in before the shelf : it wins a tie , which is also how
	sortboard.normalize threads its seen_keys."""
	known = {}
	def index( it ):
		def put( k ):
			if k and k not in known:
				known[ k ] = it
		key = str( it.get( "key" ) or "" )
		put( key.lower() )
		if it.get( "doi" ):
			put( "doi:" + str( it[ "doi" ] ).lower() )
		if key.startswith( "10." ):
			put( "doi:" + key.lower() )
		z = re.match( r"^nodoi-zotero-(.+)$" , key , re.I )
		if z:
			put( ( "zotero:" + z.group( 1 ) ).lower() )
		t = ntitle( it.get( "title" ) )
		if t:
			put( "t:" + t )
	for it in doc.get( "items" ) or []:
		index( it )
	for it in doc.get( "staging" ) or []:
		index( it )
	return known


def already_on_board( known , paper ):
	"""The row that already stands for this CSV paper , or None."""
	key = paper[ "key" ]
	doi = paper[ "doi" ] or ( key if key.startswith( "10." ) else "" )
	z   = re.match( r"^nodoi-zotero-(.+)$" , key , re.I )
	for alias in (
		key.lower() ,
		( "doi:" + doi.lower() ) if doi else "" ,
		( "zotero:" + z.group( 1 ) ).lower() if z else "" ,
		( "t:" + ntitle( paper[ "title" ] ) ) if paper[ "title" ] else "" ,
	):
		if alias and alias in known:
			return known[ alias ]
	return None


# ---------------------------------------------------------------------------
# Building a row
# ---------------------------------------------------------------------------

def vocab_canon( doc ):
	"""The board's chip spellings , by lowercase name. Emitting a tag the vocab
	doesn't have would mint a new chip -- which is how a board ends up with both
	"covert speech" and "covert-speech" -- so every tag goes through here."""
	canon = {}
	for t in ( ( doc.get( "vocab" ) or {} ).get( "tags" ) or [] ):
		name = str( ( t or {} ).get( "name" ) or "" ).strip()
		if name:
			canon.setdefault( name.lower() , name )
	return canon


def tags_for( paper , meta , canon , dropped ):
	"""The modality stamp , plus whatever the captions and the title say.
	Alphabetical , case-insensitively de-duped -- which is what
	tiers._clean_list will do to it server-side anyway , done here so --dry-run
	shows the truth."""
	want = set()
	want.update( meta.get( "mods" ) or [] )     # live stamp
	want.update( paper[ "mods" ] )              # the stamp as the CSV exported it
	text = plain_dashes( paper[ "title" ] + "\n" + "\n".join( paper[ "captions" ] ) )
	for tag , rx in _TAG_RX:
		if rx.search( text ):
			want.add( tag )
	out , seen = [] , set()
	for t in want:
		hit = canon.get( t.lower() )
		if not hit:
			dropped[ t ] += 1
			continue
		if hit.lower() not in seen:
			seen.add( hit.lower() )
			out.append( hit )
	return sorted( out , key=str.lower )


def new_row( paper , meta , columns , tags ):
	"""newItem() + prefill() from sort.html , in that order : the thirteen keys
	a /sort row has and nothing else. `placed` / `pending` are both false --
	every one of these papers has an .md , so backfillPending would clear
	`pending` on its next poll regardless."""
	fields = { c[ "id" ]: "" for c in columns }
	code   = cell_text( [ l.get( "url" ) for l in ( meta.get( "code" ) or [] ) ] )
	data   = cell_text(
		[ l.get( "url" ) for l in ( meta.get( "data" ) or [] ) ] +
		[ link_line( n.get( "name" ) , n.get( "url" ) ) if n.get( "url" )
			else str( n.get( "name" ) or "" )
			for n in ( meta.get( "names" ) or [] ) ] )
	for want , val in ( ( "code" , code ) , ( "datasets" , data ) ):
		col = _fill_col( columns , want )
		if col and val and not fields.get( col , "" ).strip():
			fields[ col ] = val
	return {
		"key":      meta.get( "key" ) or paper[ "key" ] ,
		"title":    meta.get( "title" ) or paper[ "title" ] ,
		"authors":  "" ,
		"doi":      meta.get( "doi" ) or paper[ "doi" ] ,
		"wid":      meta.get( "wid" ) or "" ,
		"pdf":      "" ,
		"year":     meta.get( "year" ) ,
		"journal":  "" ,
		"tags":     tags ,
		"placed":   False ,
		"pending":  False ,
		"fields":   fields ,
		"added_at": now_iso() ,
	}


def _fill_col( columns , which ):
	"""FILL_COLS / fillCol : the column whose id -- or failing that label --
	is spelled "code" or "datasets"."""
	rx = re.compile( r"^code$" if which == "code" else r"^datasets?$" , re.I )
	for c in columns:
		if rx.match( str( c.get( "id" ) or "" ) ):
			return c[ "id" ]
	for c in columns:
		if rx.match( str( c.get( "label" ) or "" ) ):
			return c[ "id" ]
	return None


def apply_rekey( doc , meta_by_key , report ):
	"""Adopt the library's key onto the rows in REKEY , in place. Refuses any
	pair whose titles aren't recognisably the same paper , and any key another
	row already holds -- that would be a duplicate for you to settle , not two
	rows to fold together ( the same rule adoptKey keeps )."""
	taken = { str( it.get( "key" ) or "" ) for it in doc.get( "items" ) or [] }
	for it in doc.get( "items" ) or []:
		old = str( it.get( "key" ) or "" )
		new = REKEY.get( old )
		if not new:
			continue
		m = meta_by_key.get( new ) or {}
		if not m.get( "in_library" ):
			report.append( f"  SKIPPED {old} -> {new} : not in the library" )
			continue
		if new in taken:
			report.append( f"  SKIPPED {old} -> {new} : another row already holds that key" )
			continue
		a , b = ntitle( it.get( "title" ) ) , ntitle( m.get( "title" ) )
		ratio = difflib.SequenceMatcher( None , a[ :160 ] , b[ :160 ] ).ratio()
		if ratio < 0.85:
			report.append( f"  SKIPPED {old} -> {new} : titles differ ( {ratio:.2f} )" )
			continue
		it[ "key" ] = m.get( "key" ) or new
		for f , v in ( ( "doi" , m.get( "doi" ) ) , ( "wid" , m.get( "wid" ) ) ):
			if v and not str( it.get( f ) or "" ).strip():
				it[ f ] = v
		if it.get( "year" ) is None and m.get( "year" ):
			it[ "year" ] = m[ "year" ]
		taken.add( it[ "key" ] )
		report.append( f"  {old}\n    -> {it['key']}  doi={it.get('doi') or '-'}  "
			f"wid={it.get('wid') or '-'}  year={it.get('year')}  ( titles {ratio:.2f} )" )


# ---------------------------------------------------------------------------

def main():
	ap = argparse.ArgumentParser( description=__doc__ ,
		formatter_class=argparse.RawDescriptionHelpFormatter )
	ap.add_argument( "csv" , help="The figure-selection CSV" )
	ap.add_argument( "--base" , default=DEFAULT_BASE , help=f"PRMA server ( {DEFAULT_BASE} )" )
	ap.add_argument( "--key" , default=os.environ.get( "PRMA_API_KEY" , "" ) ,
		help="API key for the write ( or PRMA_API_KEY )" )
	ap.add_argument( "--dry-run" , action="store_true" , help="Report , write nothing" )
	ap.add_argument( "--show-tags" , action="store_true" ,
		help="Print every row that would be added , with its tags" )
	args = ap.parse_args()

	csv_path = Path( args.csv ).expanduser()
	if not csv_path.exists():
		raise SystemExit( f"no such CSV : {csv_path}" )

	papers = read_selection( csv_path )
	print( f"{csv_path.name} : {sum( p['figures'] for p in papers.values() )} figure rows "
		f"-> {len( papers )} distinct papers" )

	board = _call( args.base , "/api/sort" )
	if not board.get( "ok" ):
		raise SystemExit( f"could not read the board : {board.get( 'error' ) or board}" )
	if board.get( "locked" ):
		raise SystemExit( "the /sort board is in view-only mode -- nothing was written" )
	rev     = board.get( "rev" )
	doc     = board.get( "doc" ) or {}
	columns = doc.get( "columns" ) or []
	before  = len( doc.get( "items" ) or [] )
	print( f"board : rev {rev} , {before} items , columns "
		f"{[ c.get( 'id' ) for c in columns ]}" )

	# Every CSV paper , plus the REKEY targets whose library identity we need
	# before touching their rows. One lookup : the handler takes 500 a call.
	meta = fetch_meta( args.base ,
		list( papers.keys() ) + [ k for k in REKEY.values() ] )

	# Re-keying FIRST , so a row that adopts a DOI is in the alias map before
	# the CSV paper carrying that DOI is matched against it -- otherwise it
	# reads as new and we build a second row under the key we just adopted.
	rekey_report = []
	apply_rekey( doc , meta , rekey_report )

	known = alias_index( doc )
	have , todo = [] , []
	for key in papers:
		p = papers[ key ]
		if already_on_board( known , p ) is not None:
			have.append( p )
		else:
			todo.append( p )
	print( f"already on the board : {len( have )}   to resolve : {len( todo )}" )

	canon   = vocab_canon( doc )
	dropped = collections.Counter()
	rows , not_in_library = [] , []
	for p in todo:
		m = meta.get( p[ "key" ] ) or {}
		if not m.get( "in_library" ):
			not_in_library.append( p )
			continue
		rows.append( new_row( p , m , columns , tags_for( p , m , canon , dropped ) ) )

	# A library key can differ from the CSV's spelling , and two CSV rows can
	# resolve onto one paper : the alias map has the last word.
	fresh , kept = alias_index( doc ) , []
	for r in rows:
		if already_on_board( known , { "key": r[ "key" ] , "doi": r[ "doi" ] ,
				"title": r[ "title" ] } ) is not None or r[ "key" ].lower() in fresh:
			continue
		kept.append( r )
		fresh[ r[ "key" ].lower() ] = r
	collapsed = len( rows ) - len( kept )
	rows      = kept

	# ---- what it did -------------------------------------------------------
	print()
	if rekey_report:
		print( "re-keyed in place :" )
		print( "\n".join( rekey_report ) )
		print()
	if not_in_library:
		print( f"NOT in the library , skipped ( {len( not_in_library )} ) :" )
		for p in not_in_library[ :20 ]:
			print( f"  {p['key']}  {p['title'][ :80 ]}" )
		print()
	if collapsed:
		print( f"collapsed onto a row already present / each other : {collapsed}\n" )
	if dropped:
		print( "DROPPED -- not in vocab.tags , so they would have minted a new chip :" )
		for t , c in dropped.most_common():
			print( f"  {c:4d}  {t}" )
		print()

	if rows:
		n     = [ len( r[ "tags" ] ) for r in rows ]
		freq  = collections.Counter( t for r in rows for t in r[ "tags" ] )
		ncode = sum( 1 for r in rows if ( r[ "fields" ].get( "code" ) or "" ).strip() )
		ndata = sum( 1 for r in rows if ( r[ "fields" ].get( "datasets" ) or "" ).strip() )
		print( f"to add : {len( rows )} rows , #{before + 1}-#{before + len( rows )}" )
		print( f"  tags      : mean {sum( n ) / len( n ):.2f}/row , "
			f"min {min( n )} , max {max( n )} , untagged {n.count( 0 )}" )
		print( f"  year       : {sum( 1 for r in rows if r['year'] )}/{len( rows )}" )
		print( f"  wid        : {sum( 1 for r in rows if r['wid'] )}/{len( rows )}" )
		print( f"  Code cell  : {ncode}/{len( rows )}" )
		print( f"  Datasets   : {ndata}/{len( rows )}" )
		print( f"  distinct tags used : {len( freq )}" )
		print( "  " + " · ".join( f"{t} {c}" for t , c in freq.most_common( 25 ) ) )
		if args.show_tags:
			print()
			for i , r in enumerate( rows , start=before + 1 ):
				print( f"#{i:<4} {r['key']}" )
				print( f"      {r['title'][ :96 ]}" )
				print( f"      {' , '.join( r['tags'] )}" )
	else:
		print( "nothing to add" )

	if not rows and not rekey_report:
		print( "\nNothing to do." )
		return 0
	if args.dry_run:
		print( "\n( dry-run -- nothing written )" )
		return 0

	# ---- the write ---------------------------------------------------------
	if not args.key:
		raise SystemExit( "\nPOST /api/sort needs role `user` : pass --key or set "
			"PRMA_API_KEY ( prma auth create-key <account> --key-name … )" )
	now = _call( args.base , "/api/sort/version" )
	if now.get( "locked" ):
		raise SystemExit( "the board went view-only -- nothing was written" )
	if now.get( "rev" ) != rev:
		raise SystemExit( f"the board moved under us ( rev {rev} -> {now.get( 'rev' )} ) "
			"-- re-run to pick up the change" )

	doc[ "items" ] = ( doc.get( "items" ) or [] ) + rows
	res = _call( args.base , "/api/sort" , { "doc": doc } , token=args.key )
	if not res.get( "ok" ):
		raise SystemExit( f"\nthe write was refused ( {res.get( '_status' , '?' )} ) : "
			f"{res.get( 'error' ) or res}" )
	stored = res.get( "doc" ) or {}
	print( f"\nWrote it. rev {rev} -> {res.get( 'rev' )} , "
		f"{before} -> {len( stored.get( 'items' ) or [] )} items , "
		f"vocab {len( ( stored.get( 'vocab' ) or {} ).get( 'tags' ) or [] )} chips." )
	print( "The previous version is in output/cache/sort-history/ ." )
	return 0


if __name__ == "__main__":
	raise SystemExit( main() )
