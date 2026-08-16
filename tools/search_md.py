#!/usr/bin/env python3
"""
Grep-with-a-brain over the rendered library at output/md/*.md .

A flat ` grep -l "inner speech" ` on this library returns ~850 papers and a
flat ` grep -l "inner speech" | xargs grep -l fmri ` still returns ~570 --
because almost every one of those is a paper CITING inner-speech work in
its intro , not a paper that actually PUT SOMEBODY IN A SCANNER and asked
them to talk silently. This tool exists to separate those two piles.

How it does that :

  1) SECTION AWARENESS . Each rendered .md is split into title / abstract /
     methods / body ( ` prma md ` writes a "# <title>" line , a "**DOI:**"
     line , an "## Abstract" section and "## Methods" / "### METHODS"-ish
     headers ) . A term in the TITLE is worth far more than the same term
     buried on page 9 , so each axis is scored per-zone and weighted.

  2) AXES , NOT KEYWORDS . A hit has to satisfy several independent
     groups of evidence at once :

       INNER_SPEECH  -- inner / covert / imagined / silent speech ,
                        subvocalization , verbal imagery , covert
                        articulation , ...
       FMRI_MENTION  -- fmri / functional MRI / BOLD / ...
       FMRI_DID_IT   -- evidence the authors RAN a scanner themselves :
                        field strength , TR / TE , echo-planar , voxel
                        size , SPM / FSL / AFNI , MNI normalization , ...
                        This is what kills the EEG / MEG / review papers
                        that merely discuss the fMRI literature.
       TASK          -- evidence inner speech was an actual CONDITION :
                        "participants covertly repeated" , "imagine
                        saying" , "silently generate" , ...
       DECODE        -- classifier / MVPA / searchlight / RSA / BCI /
                        encoding model -- i.e. reading the content back
                        out rather than only mapping a blob.

  3) PROXIMITY . INNER_SPEECH terms are also scored for co-occurrence
     inside the same ~400-character window as a TASK or FMRI cue , which
     is the difference between "we asked them to covertly articulate
     during scanning" and an intro sentence citing Shergill 2002.

Nothing here is a final answer -- it is a RANKED CANDIDATE LIST with the
evidence quoted inline , meant to be read ( by a human or a model ) before
anything gets called a hit. Scores rank , they do not adjudicate.

Usage :
  python tools/search_md.py                                # rank all , print top 60
  python tools/search_md.py --top 200
  python tools/search_md.py --min-score 6
  python tools/search_md.py --json out.json                # full evidence dump
  python tools/search_md.py --csv  out.csv
  python tools/search_md.py --require-axes inner,fmri_did  # hard gates
  python tools/search_md.py --explain 10.1002_hbm.10046    # why did this rank here
  python tools/search_md.py --grep "covert articulation"   # ad-hoc term , with zones
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

_REPO = Path( __file__ ).resolve().parent.parent
DEFAULT_MD      = _REPO.joinpath( "output" , "md" )
DEFAULT_METHODS = _REPO.joinpath( "output" , "methods" )


# ---------------------------------------------------------------------------
# Term groups
#
# Every entry becomes a case-insensitive regex with \b-ish boundaries. Put
# the LONG / unambiguous forms here ; short acronyms ( "bci" , "mvpa" ) are
# fine because we anchor on word boundaries , unlike the fuzzy matcher in
# src/openalex/search_helpers.py which would smear a 3-letter term into
# unrelated prose.
# ---------------------------------------------------------------------------

INNER_SPEECH = [
	r"inner speech" , r"inner-speech" ,
	r"covert speech" , r"covert-speech" ,
	r"imagined speech" , r"imagined-speech" ,
	r"silent speech" ,
	r"imagery of speech" , r"speech imagery" ,
	r"verbal imagery" , r"auditory verbal imagery" , r"auditory imagery" ,
	r"phonological imagery" ,
	r"subvocal(?:ization|isation|ized|ised|)" ,
	r"covert articulation" , r"covert articulatory" ,
	r"covert rehearsal" , r"silent rehearsal" ,
	r"inner monologue" , r"inner dialogue" , r"internal monologue" ,
	r"covert naming" , r"covert word generation" , r"covert generation" ,
	r"covert verbal" , r"covert repetition" , r"covert reading" ,
	r"silent(?:ly)? (?:repeat|recit|generat|articulat|nam|read|say|speak|count|rehears)\w*" ,
	r"imagin\w+ (?:speak|saying|say|articulat|utter|vocaliz|vocalis|pronounc)\w*" ,
	r"imagined (?:word|words|syllable|syllables|phoneme|phonemes|vowel|vowels|sentence|sentences)" ,
	r"unspoken speech" , r"endophasia" ,
	r"internal speech" , r"self-talk" , r"self talk" ,
	r"articulatory rehearsal" ,
	r"phonetic imagery" ,
	r"mental(?:ly)? (?:rehears|repeat|recit|articulat)\w*" ,
]

# Merely saying the letters f-M-R-I .
FMRI_MENTION = [
	r"fmri" , r"f-mri" ,
	r"functional mri" ,
	r"functional magnetic resonance" ,
	r"blood[- ]oxygen(?:ation)?[- ]level" ,
	r"\bbold\b(?=[^a-z])" ,
	r"7t\b" , r"3t\b" ,
]

# Evidence the AUTHORS ran a scanner -- acquisition parameters , vendors ,
# preprocessing stacks , normalization targets. A review or an EEG paper
# cites fMRI findings but almost never carries its own TR / TE / voxel size.
FMRI_DID_IT = [
	r"echo[- ]planar" , r"\bepi\b" ,
	r"\bTR\s*[=:]" , r"\bTE\s*[=:]" ,
	r"repetition time" , r"echo time" ,
	r"flip angle" ,
	r"voxel size" , r"voxel[- ]wise" , r"isotropic voxel" ,
	r"\d(?:\.\d)?\s*(?:T|tesla)\b(?=[^a-z])" ,
	r"siemens" , r"philips (?:achieva|ingenia|intera)" , r"ge signa" ,
	r"\bprisma\b" , r"\btrio\b" , r"magnetom" , r"\bskyra\b" , r"verio" ,
	r"head coil" , r"birdcage coil" , r"channel coil" ,
	r"\bSPM\d*\b" , r"statistical parametric mapping" ,
	r"\bFSL\b" , r"\bFEAT\b" , r"\bAFNI\b" , r"freesurfer" , r"fmriprep" ,
	r"\bMNI\b" , r"talairach" ,
	r"hemodynamic response function" , r"haemodynamic response function" ,
	r"\bHRF\b" ,
	r"general linear model" ,
	r"slice[- ]timing" , r"motion correction" , r"realign" ,
	r"scanner noise" , r"sparse sampling" , r"sparse acquisition" ,
	r"t2\*[- ]weighted" , r"gradient[- ]echo" ,
	r"whole[- ]brain (?:coverage|acquisition)" ,
	r"functional (?:run|runs|scan|scans|session|sessions|volume|volumes)" ,
]

# Evidence inner speech was a CONDITION the subject performed , not a
# construct the authors discussed.
TASK = [
	r"participants? (?:were asked|had to|silently|covertly|imagined|were instructed)" ,
	r"subjects? (?:were asked|had to|silently|covertly|imagined|were instructed)" ,
	r"(?:were|was) (?:asked|instructed|cued|required|told) to (?:silently|covertly|imagine|mentally)" ,
	r"instructed to (?:imagine|silently|covertly|mentally)" ,
	r"in the scanner" , r"during scanning" , r"while (?:being )?scanned" ,
	r"block design" , r"event[- ]related design" , r"jittered" ,
	r"experimental (?:condition|conditions|paradigm|design)" ,
	r"\btrials?\b.{0,40}\bcondition" ,
	r"baseline condition" , r"rest(?:ing)? condition" ,
	r"stimuli were presented" , r"presented visually" ,
	r"button press" , r"\bcue\b" ,
	r"counterbalanc" ,
	r"informed consent" ,
	r"right[- ]handed" ,
]

# Reading the CONTENT back out of the signal.
DECODE = [
	r"decod\w+" ,
	r"classif(?:y|ier|iers|ication|ied)" ,
	r"multi[- ]?voxel pattern" , r"multivariate pattern" , r"\bMVPA\b" ,
	r"searchlight" ,
	r"representational similarity" , r"\bRSA\b" ,
	r"support vector" , r"\bSVM\b" ,
	r"cross[- ]validat\w+" ,
	r"above chance" , r"chance level" , r"classification accuracy" ,
	r"brain[- ]computer interface" , r"brain[- ]machine interface" , r"\bBCI\b" ,
	r"encoding model" , r"semantic (?:decoding|reconstruction)" ,
	r"pattern analysis" ,
	r"machine learning" , r"linear discriminant" ,
	r"reconstruct\w+ (?:speech|language|words|semantic|continuous)" ,
	r"mind[- ]reading" , r"neural decoding" , r"speech decoding" ,
]

# Strong negative context : the paper is about somebody ELSE's modality.
# Not a hard veto ( plenty of good papers compare against EEG ) , just a
# signal we surface for the reviewer.
OTHER_MODALITY = [
	r"\bECoG\b" , r"electrocorticograph\w+" ,
	r"\bMEG\b" , r"magnetoencephalograph\w+" ,
	r"\bEEG\b" , r"electroencephalograph\w+" ,
	r"stereo[- ]?EEG" , r"\bsEEG\b" , r"intracranial" ,
	r"microelectrode" , r"utah array" , r"neuropixels" ,
	r"\bfNIRS\b" , r"near[- ]infrared" ,
	r"\bTMS\b" , r"transcranial" ,
	r"\bPET\b" , r"positron emission" ,
]

AXES = {
	"inner"    : INNER_SPEECH ,
	"fmri"     : FMRI_MENTION ,
	"fmri_did" : FMRI_DID_IT ,
	"task"     : TASK ,
	"decode"   : DECODE ,
	"other_mod": OTHER_MODALITY ,
}

# Per-zone weight for each axis. Title / abstract carry the paper's actual
# CLAIM ; methods carry what was actually DONE ; body is mostly citations ,
# so it is worth little and gets capped hard.
ZONE_WEIGHTS = {
	"title"   : 6.0 ,
	"abstract": 3.0 ,
	"methods" : 2.0 ,
	"body"    : 0.4 ,
}

# How many body hits we let count before the axis saturates. Without this ,
# a 60-page review that says "inner speech" 200 times outranks the actual
# 8-subject scanner study that says it 12 times.
BODY_CAP = { "inner": 6 , "fmri": 4 , "fmri_did": 8 , "task": 6 , "decode": 6 , "other_mod": 10 }

PROXIMITY_WINDOW = 400


def compile_group( patterns ):
	return [ ( p , re.compile( p , re.IGNORECASE ) ) for p in patterns ]


COMPILED = { name: compile_group( pats ) for name , pats in AXES.items() }

# One alternation per axis. Scoring only ever needs "how many hits and
# where" , and a single fused scan is ~20x faster over 146 MB of markdown
# than running 40 separate patterns across the same text. The per-pattern
# breakdown ( which terms fired ) is genuinely useful for review , so it is
# computed lazily in per_pattern() for the handful of papers that survive
# the gates rather than for all 2200.
COMBINED = {
	name: re.compile( "|".join( f"(?:{p})" for p in pats ) , re.IGNORECASE )
	for name , pats in AXES.items()
}


def per_pattern( zone_text , axis ):
	"""Which individual terms fired , for the evidence dump. Slow path."""
	out = {}
	if not zone_text:
		return out
	for pat , rx in COMPILED[ axis ]:
		n = len( rx.findall( zone_text ) )
		if n:
			out[ pat ] = n
	return out


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_H_ABSTRACT = re.compile( r"^#{1,4}\s*abstract\b"          , re.IGNORECASE | re.MULTILINE )
_H_METHODS  = re.compile( r"^#{1,4}\s*(?:\d+\.?\s*)?(?:materials? and )?(?:methods?|methodology|experimental (?:procedure|design|method)s?|material and method)\b" ,
                          re.IGNORECASE | re.MULTILINE )
_H_ANY      = re.compile( r"^#{1,4}\s+\S"                  , re.MULTILINE )
_H_REFS     = re.compile( r"^#{1,4}\s*(?:references|bibliography|works cited)\b" ,
                          re.IGNORECASE | re.MULTILINE )


def _section_after( text , header_re , max_chars=40000 ):
	"""Text from the first match of header_re up to the next same-or-higher
	header , so 'Methods' picks up its own '### Subjects' / '### Image
	acquisition' children but stops at 'Results'."""
	m = header_re.search( text )
	if not m:
		return ""
	start  = m.end()
	level  = len( text[ m.start(): m.end() ].split()[ 0 ] )
	tail   = text[ start : start + max_chars ]
	for nxt in _H_ANY.finditer( tail ):
		hashes = len( tail[ nxt.start(): ].split()[ 0 ] )
		if hashes <= level:
			return tail[ : nxt.start() ]
	return tail


def parse_md( path , methods_dir=None ):
	raw = path.read_text( encoding="utf-8" , errors="replace" )

	# Drop a references section if ` prma md --include-references ` left one
	# in : a bibliography names every construct the paper ever cited and is
	# pure noise for this question.
	refs = _H_REFS.search( raw )
	if refs:
		raw = raw[ : refs.start() ]

	title = ""
	mt = re.search( r"^#\s+(.+)$" , raw , re.MULTILINE )
	if mt:
		title = mt.group( 1 ).strip()

	doi = ""
	md_ = re.search( r"\*\*DOI:\*\*\s*\[([^\]]+)\]" , raw )
	if md_:
		doi = md_.group( 1 ).strip()
	else:
		doi = path.stem.replace( "_" , "/" , 1 )

	abstract = _section_after( raw , _H_ABSTRACT , max_chars=12000 )

	# Prefer the dedicated ` prma methods ` extract over our own header-walk
	# of the rendered .md . It is the SAME question asked of better evidence :
	# that file is the protocol only -- no intro, no discussion, no
	# bibliography -- so a covert-speech term appearing in it is far more
	# likely to be a task the subject actually performed than a construct the
	# authors cited. Falls back to the parsed section when the extract is
	# missing or empty ( ~25% of the library has no methods extract yet ) .
	methods , methods_src = "" , "md-section"
	if methods_dir is not None:
		mf = Path( methods_dir ).joinpath( path.stem + ".txt" )
		if mf.exists():
			t = mf.read_text( encoding="utf-8" , errors="replace" ).strip()
			if len( t ) >= 10:
				methods , methods_src = t , "methods-extract"
	if not methods:
		methods = _section_after( raw , _H_METHODS , max_chars=40000 )

	return {
		"path"       : path ,
		"doi"        : doi ,
		"title"      : title ,
		"abstract"   : abstract ,
		"methods"    : methods ,
		"methods_src": methods_src ,
		"body"       : raw ,
		"chars"      : len( raw ) ,
	}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def zone_hits( zone_text , axis ):
	"""-> ( total_count , [ (pos , matched_text) ] ) via the fused regex."""
	if not zone_text:
		return 0 , []
	spans = [ ( m.start() , m.group( 0 ) ) for m in COMBINED[ axis ].finditer( zone_text ) ]
	return len( spans ) , spans


def proximity_score( body , axis_a="inner" , axis_b=( "task" , "fmri_did" ) ):
	"""Count INNER_SPEECH occurrences that sit within PROXIMITY_WINDOW chars
	of a task / acquisition cue -- i.e. inner speech described as something
	that HAPPENED IN THE SCANNER , not something in the literature."""
	_ , a_spans = zone_hits( body , axis_a )
	if not a_spans:
		return 0 , []
	b_spans = []
	for axis in axis_b:
		_ , s = zone_hits( body , axis )
		b_spans.extend( s )
	if not b_spans:
		return 0 , []
	b_pos = sorted( p for p , _ in b_spans )

	import bisect
	near , examples = 0 , []
	for pos , txt in a_spans:
		i = bisect.bisect_left( b_pos , pos )
		best = None
		for j in ( i - 1 , i ):
			if 0 <= j < len( b_pos ):
				d = abs( b_pos[ j ] - pos )
				if best is None or d < best:
					best = d
		if best is not None and best <= PROXIMITY_WINDOW:
			near += 1
			if len( examples ) < 4:
				lo = max( 0 , pos - 160 )
				examples.append( re.sub( r"\s+" , " " , body[ lo : pos + 220 ] ).strip() )
	return near , examples


def score_paper( paper ):
	detail = {}
	score  = 0.0

	for axis in AXES:
		axis_detail = {}
		axis_score  = 0.0
		for zone in ( "title" , "abstract" , "methods" , "body" ):
			cnt , _ = zone_hits( paper[ zone ] , axis )
			if not cnt:
				continue
			eff = min( cnt , BODY_CAP[ axis ] ) if zone == "body" else min( cnt , 3 )
			axis_score += eff * ZONE_WEIGHTS[ zone ]
			axis_detail[ zone ] = { "count": cnt }
		detail[ axis ] = { "score": round( axis_score , 2 ) , "zones": axis_detail }
		if axis != "other_mod":
			score += axis_score

	near , examples = proximity_score( paper[ "body" ] )
	detail[ "proximity" ] = { "near": near , "examples": examples }
	score += min( near , 8 ) * 2.0

	# An inner-speech term IN THE TITLE is the single strongest signal that
	# this is a paper ABOUT inner speech rather than one that mentions it.
	if detail[ "inner" ][ "zones" ].get( "title" ):
		score += 25.0
	if detail[ "inner" ][ "zones" ].get( "abstract" ):
		score += 10.0
	# Same for the scanner : fMRI in the title / abstract means it's the
	# paper's own method.
	if detail[ "fmri" ][ "zones" ].get( "title" ):
		score += 12.0
	if detail[ "fmri" ][ "zones" ].get( "abstract" ):
		score += 6.0

	detail[ "total" ] = round( score , 2 )
	return score , detail


def axis_present( detail , axis , zones=None ):
	z = detail[ axis ][ "zones" ]
	if zones is None:
		return bool( z )
	return any( k in z for k in zones )


# ---------------------------------------------------------------------------
# Dossier -- the compact brief a reviewer actually reads
# ---------------------------------------------------------------------------

_SCANNER_LINE = re.compile(
	r"[^.]*?(?:\d(?:\.\d)?\s*(?:T|tesla)\b|echo[- ]planar|siemens|philips|ge signa|"
	r"\bTR\s*[=:]|repetition time|voxel size|head coil)[^.]*\." ,
	re.IGNORECASE )


def _sentences_around( text , axis , other_axes , window=PROXIMITY_WINDOW , limit=6 ):
	"""Pull the sentences where `axis` fires close to any of `other_axes` --
	the actual quotable evidence that inner speech was a scanner task."""
	_ , a_spans = zone_hits( text , axis )
	if not a_spans:
		return []
	b_pos = []
	for ax in other_axes:
		_ , s = zone_hits( text , ax )
		b_pos.extend( p for p , _ in s )
	b_pos.sort()
	if not b_pos:
		return []

	import bisect
	out , seen = [] , set()
	for pos , _ in a_spans:
		i = bisect.bisect_left( b_pos , pos )
		near = any( 0 <= j < len( b_pos ) and abs( b_pos[ j ] - pos ) <= window
		            for j in ( i - 1 , i ) )
		if not near:
			continue
		lo = text.rfind( "." , 0 , max( 0 , pos - 240 ) ) + 1
		hi = text.find( "." , pos + 200 )
		hi = hi + 1 if hi != -1 else min( len( text ) , pos + 400 )
		frag = re.sub( r"\s+" , " " , text[ lo:hi ] ).strip()
		key  = frag[ :90 ]
		if len( frag ) > 60 and key not in seen:
			seen.add( key )
			out.append( frag )
		if len( out ) >= limit:
			break
	return out


def _write_dossier( path , kept ):
	with path.open( "w" , encoding="utf-8" ) as fh:
		for rank , r in enumerate( kept , 1 ):
			p , d = r[ "paper" ] , r[ "detail" ]
			fh.write( f"\n{'='*100}\n#{rank}  score={r['score']:.1f}  {p['doi']}\n" )
			fh.write( f"TITLE: {p['title']}\nFILE: {p['path'].name}\n" )
			fh.write( f"AXES: inner={d['inner']['score']} fmri={d['fmri']['score']} "
			          f"fmri_did={d['fmri_did']['score']} task={d['task']['score']} "
			          f"decode={d['decode']['score']} other_mod={d['other_mod']['score']} "
			          f"prox={d['proximity']['near']}\n" )

			abst = re.sub( r"\s+" , " " , p[ "abstract" ] ).strip()
			fh.write( f"\nABSTRACT: {abst[:1800]}\n" )

			scan = _SCANNER_LINE.search( p[ "methods" ] or "" ) or _SCANNER_LINE.search( p[ "body" ] )
			if scan:
				fh.write( f"\nSCANNER: {re.sub(chr(92)+'s+',' ',scan.group(0)).strip()[:400]}\n" )

			ev = _sentences_around( p[ "methods" ] or p[ "body" ] , "inner" , ( "task" , "fmri_did" ) )
			if ev:
				fh.write( "\nINNER-SPEECH-AS-TASK EVIDENCE:\n" )
				for e in ev:
					fh.write( f"  - {e[:420]}\n" )

			dec = _sentences_around( p[ "body" ] , "decode" , ( "inner" , ) , window=300 , limit=3 )
			if dec:
				fh.write( "\nDECODING EVIDENCE:\n" )
				for e in dec:
					fh.write( f"  - {e[:340]}\n" )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
	ap = argparse.ArgumentParser( description=__doc__ ,
		formatter_class=argparse.RawDescriptionHelpFormatter )
	ap.add_argument( "--md" , type=Path , default=DEFAULT_MD ,
		help="Directory of rendered .md papers ( default : output/md )" )
	ap.add_argument( "--methods-dir" , type=Path , default=DEFAULT_METHODS ,
		help="Directory of ` prma methods ` extracts ( default : output/methods ). These are "
		     "protocol-only text -- no intro , no discussion , no bibliography -- so they are a "
		     "much cleaner signal for 'was this a TASK the subject performed' than the rendered "
		     ".md . Used for the methods zone whenever an extract exists , falling back to the "
		     "header-walk of the .md otherwise. Pass --no-methods-dir to disable." )
	ap.add_argument( "--no-methods-dir" , dest="methods_dir" , action="store_const" , const=None ,
		help="Ignore output/methods and parse the methods section out of the .md instead" )
	ap.add_argument( "--top" , type=int , default=60 ,
		help="How many ranked candidates to print ( default : 60 )" )
	ap.add_argument( "--min-score" , type=float , default=0.0 )
	ap.add_argument( "--require-axes" , default="inner,fmri" ,
		help="Comma-separated axes that must fire at all ( default : inner,fmri ). "
		     "Use '' to disable. Choices : " + " , ".join( AXES ) )
	ap.add_argument( "--require-strong" , action="store_true" ,
		help="Also require inner speech in title/abstract/methods AND fmri_did evidence" )
	ap.add_argument( "--json" , type=Path , help="Write the full evidence dump here" )
	ap.add_argument( "--csv"  , type=Path , help="Write a ranked candidate table here" )
	ap.add_argument( "--explain" , help="Print the full evidence for one paper ( filename stem or DOI substring )" )
	ap.add_argument( "--dossier" , type=Path ,
		help="Write a compact human/model-readable brief per candidate : title , abstract , "
		     "scanner line , and the sentences where inner speech meets a task/acquisition cue. "
		     "This is the file you actually READ to decide what counts as a hit." )
	ap.add_argument( "--dossier-top" , type=int , default=250 ,
		help="How many ranked candidates to write dossiers for ( default : 250 )" )
	ap.add_argument( "--grep" , help="Ad-hoc : report every paper matching this regex , with zones" )
	ap.add_argument( "--quiet" , action="store_true" )
	args = ap.parse_args()

	files = sorted( args.md.glob( "*.md" ) )
	if not files:
		print( f"no .md under {args.md}" , file=sys.stderr )
		return 1
	if not args.quiet:
		print( f"scanning {len(files)} papers under {args.md} ..." , file=sys.stderr )

	# --- ad-hoc grep mode -------------------------------------------------
	if args.grep:
		rx = re.compile( args.grep , re.IGNORECASE )
		n  = 0
		for f in files:
			p = parse_md( f , args.methods_dir )
			zones = [ z for z in ( "title" , "abstract" , "methods" ) if rx.search( p[ z ] or "" ) ]
			hits  = len( rx.findall( p[ "body" ] ) )
			if hits:
				n += 1
				print( f"{hits:4d}  [{','.join(zones) or 'body':<24}]  {p['doi']}  {p['title'][:80]}" )
		print( f"\n{n} papers matched /{args.grep}/" , file=sys.stderr )
		return 0

	# --- score ------------------------------------------------------------
	rows = []
	for f in files:
		p = parse_md( f , args.methods_dir )
		s , d = score_paper( p )
		rows.append( { "paper": p , "score": s , "detail": d } )

	# --- explain mode -----------------------------------------------------
	if args.explain:
		key = args.explain.lower()
		for r in rows:
			if key in r[ "paper" ][ "path" ].stem.lower() or key in r[ "paper" ][ "doi" ].lower():
				for axis , adet in r[ "detail" ].items():
					if not isinstance( adet , dict ) or "zones" not in adet:
						continue
					for zone , zdet in adet[ "zones" ].items():
						zdet[ "terms" ] = per_pattern( r[ "paper" ][ zone ] , axis )
				print( json.dumps( {
					"doi"   : r[ "paper" ][ "doi" ] ,
					"title" : r[ "paper" ][ "title" ] ,
					"score" : r[ "score" ] ,
					"detail": r[ "detail" ] ,
				} , indent=2 ) )
				return 0
		print( f"no paper matching {args.explain!r}" , file=sys.stderr )
		return 1

	# --- gates ------------------------------------------------------------
	req = [ a.strip() for a in args.require_axes.split( "," ) if a.strip() ]
	def keeps( r ):
		if r[ "score" ] < args.min_score:
			return False
		for a in req:
			if not axis_present( r[ "detail" ] , a ):
				return False
		if args.require_strong:
			if not axis_present( r[ "detail" ] , "inner" , ( "title" , "abstract" , "methods" ) ):
				return False
			if not axis_present( r[ "detail" ] , "fmri_did" ):
				return False
		return True

	kept = [ r for r in rows if keeps( r ) ]
	kept.sort( key=lambda r: -r[ "score" ] )

	if not args.quiet:
		print( f"{len(kept)} / {len(rows)} papers pass the gates\n" , file=sys.stderr )

	def flags( d ):
		f = ""
		f += "T" if d[ "inner" ][ "zones" ].get( "title" ) else "-"
		f += "A" if d[ "inner" ][ "zones" ].get( "abstract" ) else "-"
		f += "M" if d[ "inner" ][ "zones" ].get( "methods" ) else "-"
		f += "|"
		f += "F" if d[ "fmri" ][ "zones" ].get( "title" ) or d[ "fmri" ][ "zones" ].get( "abstract" ) else "-"
		f += "S" if d[ "fmri_did" ][ "zones" ] else "-"
		f += "D" if d[ "decode" ][ "zones" ].get( "title" ) or d[ "decode" ][ "zones" ].get( "abstract" ) else "-"
		return f

	for r in kept[ : args.top ]:
		d = r[ "detail" ]
		print( f"{r['score']:7.1f}  {flags(d)}  prox={d['proximity']['near']:<3d}  "
		       f"{r['paper']['doi']:<40}  {r['paper']['title'][:95]}" )

	if args.csv:
		with args.csv.open( "w" , newline="" , encoding="utf-8" ) as fh:
			w = csv.writer( fh )
			w.writerow( [ "score" , "doi" , "title" , "inner_title" , "inner_abstract" ,
			              "inner_methods" , "inner_body" , "fmri_did" , "decode" ,
			              "other_modality" , "proximity" , "methods_src" , "file" ] )
			for r in kept:
				d , p = r[ "detail" ] , r[ "paper" ]
				z = d[ "inner" ][ "zones" ]
				w.writerow( [
					round( r[ "score" ] , 1 ) , p[ "doi" ] , p[ "title" ] ,
					z.get( "title"   , {} ).get( "count" , 0 ) ,
					z.get( "abstract", {} ).get( "count" , 0 ) ,
					z.get( "methods" , {} ).get( "count" , 0 ) ,
					z.get( "body"    , {} ).get( "count" , 0 ) ,
					sum( v[ "count" ] for v in d[ "fmri_did" ][ "zones" ].values() ) ,
					sum( v[ "count" ] for v in d[ "decode"   ][ "zones" ].values() ) ,
					sum( v[ "count" ] for v in d[ "other_mod" ][ "zones" ].values() ) ,
					d[ "proximity" ][ "near" ] , p[ "methods_src" ] , p[ "path" ].name ,
				] )
		if not args.quiet:
			print( f"\nwrote {args.csv}" , file=sys.stderr )

	if args.dossier:
		_write_dossier( args.dossier , kept[ : args.dossier_top ] )
		if not args.quiet:
			print( f"\nwrote {args.dossier}" , file=sys.stderr )

	if args.json:
		# Fill in WHICH terms fired -- the slow per-pattern pass , run only
		# for papers that survived the gates.
		for r in kept:
			for axis , adet in r[ "detail" ].items():
				if not isinstance( adet , dict ) or "zones" not in adet:
					continue
				for zone , zdet in adet[ "zones" ].items():
					zdet[ "terms" ] = per_pattern( r[ "paper" ][ zone ] , axis )
		dump = [ {
			"doi"   : r[ "paper" ][ "doi" ] ,
			"title" : r[ "paper" ][ "title" ] ,
			"file"  : r[ "paper" ][ "path" ].name ,
			"score" : round( r[ "score" ] , 2 ) ,
			"detail": r[ "detail" ] ,
		} for r in kept ]
		args.json.write_text( json.dumps( dump , indent=1 ) , encoding="utf-8" )
		if not args.quiet:
			print( f"wrote {args.json}" , file=sys.stderr )

	return 0


if __name__ == "__main__":
	raise SystemExit( main() )
