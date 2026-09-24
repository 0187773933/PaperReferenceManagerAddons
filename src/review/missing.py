"""
The /review-missing compute : the same three criteria , run over the papers you
do NOT have.

WHAT THIS IS. /review screens the two hand-curated surfaces -- the sort board
and the picked figures -- which is a few hundred papers somebody already decided
were worth keeping. This screens the OTHER pool : the dashboard's "All missing"
tab , every work connected to your library that isn't in it. That is ~140,000
papers , and nobody has judged any of them. So where /review is the read of a
judgement , this is a SEARCH for the papers that judgement has not reached yet.

INPUTS -- one , and it is already in memory :

  the dashboard index   src/dashboard/indexer.py ( output/cache/dashboard_index.json.gz )
                        -> references  : works your library cites , that you don't have
                        -> cited_by    : works that cite your library , that you don't have
                        Each carries title , doi , year , the OA citation count , how many
                        of YOUR papers connect to it , and a title+abstract haystack.

  the built /review     output/cache/review.json , read for its corpus-mined dataset
                        acquisition table ONLY , so a missing paper that names NSD can
                        still be told what NSD's TR is. An enhancement , never a
                        dependency : no review , no inferred acquisition values.

WHAT WE HAVE TO SCREEN ON. A title and an abstract -- about 1,500 characters ,
against the 50,000+ /review reads. There is no methods section , so the
full-text thresholds would reject every paper in the pool. classify_abstract
( src/review/classify.py ) reuses the same vocabulary against thresholds sized
for an abstract ; the long comment above it spells out which way each criterion
had to move and why.

WHAT THAT COSTS , measured rather than asserted. Screened against the papers the
built /review has already judged , the abstract screen comes out lopsided the
same way every time :

    of the papers it lets through , around nine in ten the full text also
      included -- it is right about what it finds
    of the papers the full text included , it finds around two in five -- it is
      wrong about most of what is there

The exact figures live in meta.screen of the built document , not in this
comment , because the recall is RE-MEASURED on every build ( measure_recall ,
below ) and the page prints what came back. A page framed on two numbers should
not be quoting one somebody wrote down once. Precision cannot be re-measured
here : it needs the papers /review threw OUT , and an excluded row carries no
abstract to re-screen , so that one is recorded with its basis instead.

The misses are not a tuning failure. 54 of the 83 are papers whose abstract
never names a transformer at all -- it says "a deep learning model" and leaves
the architecture to the methods. No threshold recovers a word that isn't there.
So THIS PAGE REPORTS CANDIDATES , NOT A REVIEW : what it finds is worth reading ,
what it misses it misses silently , and the page says both in those words.

THE EXCLUDED ARE COUNTED , NOT LISTED. /review prints every paper it dropped ,
because 329 rows is a table. Here it is ~97,000 , which is not a table -- it is
the pool again. So the document carries the tally by reason , plus the NEAR
MISSES : papers that satisfied two criteria and failed exactly one. That is the
same instinct behind /review's "borderline" flag ( where a false negative hides )
scaled to a pool this size , capped , and ranked by how connected each paper is
to your library.

OUTPUT ( output/cache/review-missing.json ) -- deliberately the SAME document
shape /review writes , down to the per-field { value , values , counts ,
evidence , source } , so src/dashboard/review-missing.html renders it with the
same one field renderer and the two pages can never drift into showing the same
fact two ways. What differs is what a row IS : no local PDF , no rendered md , no
figures and no curator notes , and instead the pool it came from , how many of
your papers connect to it , and a link to go and get it.

COST : one pass of the abstract screen over the whole pool ( minutes ) , then
extraction over the few hundred that survive ( seconds ). Like /review , it is
built ONCE and persisted , and the server rebuilds it on a background thread
rather than on the request that noticed it was stale.
"""

import os
import time
from collections import Counter , OrderedDict

from ..utils import utils
from .       import build as review_build
from .       import classify as CLS
from .       import datasets as DS
from .extract import ACQ_PATTERNS , ARCH_PATTERNS , PREPROC_PATTERNS , clean , extract_block


SCHEMA_VERSION = 1

EV_CHARS = 600      # max chars per verbatim quote , as /review
MAX_ABS  = 6000     # an OpenAlex abstract that long is a parsing accident

# How many rows the document is allowed to carry. The pool is ~140,000 papers ;
# a page is not. Both lists are RANKED before they are cut ( see build ) , so a
# cap drops the least-connected rows rather than an arbitrary slice , and the
# meta block says how many were cut so the number is never silently wrong.
# A record runs ~4 KB with the empty fields left out ( see paper_record ) , so a
# full house here is a payload a browser can still hold.
MAX_PAPERS    = 4000    # screened in
MAX_NEARMISS  = 400     # failed exactly one criterion

ARCH_COLS = list( ARCH_PATTERNS.keys()    )
ACQ_COLS  = list( ACQ_PATTERNS.keys()     )
PRE_COLS  = list( PREPROC_PATTERNS.keys() )

PRIORITY = review_build.PRIORITY

# The same three criteria as /review , worded for what an abstract can show.
# Different words for the same test , deliberately : a reader who has both pages
# open should be able to see that the bar moved and where.
CRITERIA = [
	"The abstract names fMRI/BOLD at least as often as any rival modality "
	"( EEG/MEG/ECoG/fNIRS ). An abstract states its modality once if at all , so one "
	"mention counts -- and an abstract that never names it cannot be screened in." ,
	"The abstract describes a DECODING task : brain in , stimulus / mental content / "
	"class label out. Encoding models and MRI reconstruction / acceleration are excluded." ,
	"The abstract names transformer or attention machinery. One mention counts here , "
	"where the full text needs two : an abstract has no related-work section , so a "
	"transformer named in it is almost always the authors' own." ,
]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def missing_path( args ):
	"""Where the built document lands -- beside review.json , for the same
	reason : it is derived , and deleting it costs one rebuild."""
	return args.output.joinpath( "cache" , "review-missing.json" )


# ---------------------------------------------------------------------------
# Candidates : the two external pools , merged
# ---------------------------------------------------------------------------

def split_haystack( title , hay ):
	"""The abstract out of a dashboard index entry.

	The index stores ONE searchable string per work -- ` ( title + " " + abstract
	).lower() ` ( indexer._make_haystack ) -- so the abstract is what is left
	after the title , and it arrives lowercased. That costs nothing here : every
	pattern classify_abstract matches is case-insensitive , and screening 377
	known papers with their abstracts lowercased changed exactly zero verdicts.
	It costs something on the PAGE , which is why a survivor's abstract is
	re-read verbatim where it can be ( see verbatim_abstract )."""
	h = ( hay or "" ).strip()
	t = ( title or "" ).strip().lower()
	if t and h.startswith( t ):
		return h[ len( t ): ].strip()
	return h


def verbatim_abstract( args , wid ):
	"""The abstract as OpenAlex punctuated it , or "" .

	Only worth reading for a paper that got through -- so this is called on the
	few hundred survivors , never on the pool. Works your library CITES have
	their own file ( ` prma missing ` wrote one per reference ) ; works that cite
	YOU do not -- they arrive inlined in the citing library paper's own OpenAlex
	JSON , which averages 2.4 MB , and opening a few hundred of those to fix some
	capitalisation is not a trade worth making. Those rows keep the index text
	and say so."""
	fp = args.output.joinpath( "cache" , "openalex" , "references" , f"{wid}.json" )
	if not fp.exists():
		return ""
	try:
		meta = utils.read_json( fp ) or {}
	except Exception:
		return ""
	from ..dashboard.indexer import _reconstruct_abstract
	return ( _reconstruct_abstract( meta.get( "abstract_inverted_index" ) ) or "" )[ :MAX_ABS ]


def load_pool( args , pools=None ):
	"""Every work in either external pool , merged and keyed by OpenAlex work id.

	`pools` is the servable dict the dashboard index materialises. The server
	already holds it in memory ( DashboardData ) and passes it in ; the CLI has
	no server , so it loads it off disk. Same rows either way.

	A work reachable BOTH ways -- your papers cite it AND it cites your papers --
	is one candidate carrying both pool names , the way a /review paper on both
	curated surfaces is one row carrying both. Its connection count is the sum ,
	because the two tallies count different relationships and both are reasons
	the paper is near you."""
	if pools is None:
		from ..dashboard import indexer
		pools = indexer.load_pools( args ) or {}

	out = OrderedDict()
	for pool_name in ( "references" , "cited_by" ):
		for r in ( pools.get( pool_name ) or [] ):
			wid = r.get( "wid" ) or r.get( "key" )
			if not wid:
				continue
			e = out.get( wid )
			if e is None:
				title = ( r.get( "title" ) or "" ).strip()
				e = out[ wid ] = dict(
					key       = wid ,
					wid       = wid ,
					title     = title ,
					doi       = utils.normalize_doi( r.get( "doi" ) ) or ( r.get( "doi" ) or "" ) ,
					year      = str( r.get( "year" ) or "" ).strip() ,
					published = r.get( "pubdate" ) or "" ,
					cited_by  = r.get( "cited_by" ) ,
					pdf       = r.get( "pdf" ) or "" ,       # a REMOTE open-access url
					authors   = r.get( "authors" ) or [] ,
					abstract  = split_haystack( title , r.get( "hay" ) ) ,
					pools     = [] ,
					lib_cites = 0 ,
				)
			e[ "pools" ].append( pool_name )
			e[ "lib_cites" ] += ( r.get( "lib_cites" ) or 0 )
	return out


def first_author( authors ):
	"""'Kamitani et al.' off the index's ( id , name , orcid ) triples. A pool
	this size is browsed , and a list of 4,000 titles with no author on any of
	them is a list you cannot recognise anything in."""
	names = [ a[ 1 ] for a in ( authors or [] ) if len( a ) > 1 and a[ 1 ] ]
	if not names:
		return ""
	return names[ 0 ] + ( " et al." if len( names ) > 1 else "" )


# ---------------------------------------------------------------------------
# Screening
# ---------------------------------------------------------------------------

def screen( entry ):
	"""One candidate , screened. Cheap by construction : classify_abstract does
	no evidence extraction for a paper it rejects , which is ~99% of them."""
	return CLS.classify_abstract( entry[ "title" ] , entry[ "abstract" ] )


# Which criterion failed -> what to call that on the page. The wording says what
# the ABSTRACT did , not what the paper is : "names no architecture" is a fact
# about 1,500 characters of text , where "no transformer" would be a claim about
# a paper nobody here has read.
MISS_LABEL = {
	"attention" : "names no architecture" ,
	"decoding"  : "no decoding verb" ,
	"fmri"      : "modality contested" ,
}


def near_miss( cl ):
	"""Which single criterion a rejected paper failed , when the other two held --
	or "" when it failed more than one.

	This is /review's `borderline` flag scaled to a pool this size : there , a
	false negative hid among the papers dropped only for weak attention evidence ;
	here the same is true , and the calibration says so out loud -- 54 of the 83
	papers this screen misses are papers whose abstract simply never names an
	architecture. Those are exactly the rows this picks out.

	Read off cl[ 'criteria' ] , never off the exclusion reason. The reason is the
	FIRST test that failed , and the cascade stops there : a paper rejected for
	naming no transformer was never asked whether it decodes , so calling it a
	near miss on that basis fills the list with resting-state parcellation papers
	that failed two criteria and were only ever told about one."""
	c = cl.get( "criteria" ) or {}
	if not c.get( "primary" ):
		return ""                       # a survey is not a near miss , it is a survey
	failed = [ k for k in ( "fmri" , "attention" , "decoding" ) if not c.get( k ) ]
	return MISS_LABEL[ failed[ 0 ] ] if len( failed ) == 1 else ""


# ---------------------------------------------------------------------------
# Serialisation -- the SAME shapes /review writes
# ---------------------------------------------------------------------------

def field_json( f ):
	"""One extracted field , in the shape review.html's single field renderer
	keys off. No curated variant : config/review-overrides.json is keyed by the
	paper keys the BOARDS use , and nothing in this pool is on a board."""
	out = {
		"value"    : f.values[ 0 ] if f else "" ,
		"values"   : list( f.values ) if f else [] ,
		"counts"   : dict( f.counts ) if f else {} ,
		"evidence" : [ q[ :EV_CHARS ] for q in list( f.spans.values() )[ :3 ] ] ,
		"source"   : "auto" if f else "absent" ,
	}
	if f and len( f.values ) > 1:
		out[ "all_values" ] = " | ".join( f.values )
	return out


def _stated( block , cols ):
	"""{ field : field_json } for the fields the text actually stated. See the
	note in paper_record for why the absent ones are dropped rather than carried
	as fifty "absent" entries per paper."""
	return { c: field_json( block[ c ] ) for c in cols if block[ c ] }


def paper_record( n , e , cl , text , ds_index ):
	"""One screened-in paper , serialised.

	The extraction battery runs over the ABSTRACT , which is the honest thing to
	do and mostly comes back empty : an abstract states a backbone , sometimes a
	subject count , almost never a TR. Empty is not a failure and the page prints
	it in words. What the abstract does not say about acquisition can still be
	filled from the dataset it names , exactly as on /review and marked the same
	way."""
	arch = extract_block( text , ARCH_PATTERNS    )
	acq  = extract_block( text , ACQ_PATTERNS     )
	pre  = extract_block( text , PREPROC_PATTERNS )
	# One mention is enough here. /review wants two before it believes a paper
	# uses a dataset , because it is reading 50,000 characters in which "HCP"
	# appears in passing ; an abstract names its data once and moves on.
	used = DS.detect( text , min_hits=1 )

	acq_json = {}
	for c in ACQ_COLS:
		own  = acq[ c ].values[ 0 ] if acq[ c ] else ""
		val , prov = review_build.resolve_acq( c , own , used , ds_index )
		fj = field_json( acq[ c ] )
		if not fj[ "value" ] and val:
			fj[ "value" ] , fj[ "values" ] , fj[ "source" ] = val , [ val ] , "inferred-from-dataset"
			fj[ "inferred_from" ] = prov
		elif prov and prov.startswith( "multiple" ):
			fj[ "note" ] = prov
		if fj[ "value" ] or fj.get( "note" ):
			acq_json[ c ] = fj

	return {
		"n"              : n ,
		"key"            : e[ "key" ] ,
		"wid"            : e[ "wid" ] ,
		"doi"            : e[ "doi" ] ,
		"title"          : e[ "title" ] ,
		"year"           : e[ "year" ] ,
		"published"      : e[ "published" ] ,
		"added"          : "" ,                 # it is not in your library ; that is the point
		"cited_by"       : e[ "cited_by" ] ,
		"authors"        : first_author( e[ "authors" ] ) ,
		# WHY this paper is near you , which is the only ranking signal this pool
		# has that /review does not.
		"lib_cites"      : e[ "lib_cites" ] ,
		"pools"          : list( e[ "pools" ] ) ,
		"pdf"            : e[ "pdf" ] ,          # remote OA pdf , or ""
		"priority"       : PRIORITY.get( cl[ "task_category" ] , 6 ) ,
		"task_category"  : cl[ "task_category" ] ,
		"model_name"     : review_build.model_name( e , {} , text ) ,
		"attention_role" : cl[ "attention_role" ] ,
		"detail_source"  : "abstract-only" ,
		"curated"        : False ,
		"datasets_used"  : list( used ) ,
		"abstract"       : clean( text )[ :4000 ] ,
		"abstract_source": e[ "abstract_source" ] ,
		# STATED FIELDS ONLY. /review serialises all fifty-odd for every paper ,
		# because there a blank is a fact : somebody's extractor read the whole
		# paper and the number was not in it. Here a blank means the number was
		# never going to be in an abstract , which is not news about the paper --
		# and 50 of them per row is over half the document's bytes , for entries
		# the page does not draw. The GROUP still knows its full column list
		# ( meta.field_groups ) , so "1 of 27 stated" is still countable.
		"architecture"   : _stated( arch , ARCH_COLS ) ,
		"acquisition"    : acq_json ,
		"preprocessing"  : _stated( pre  , PRE_COLS  ) ,
		"screening"      : {
			"fmri_evidence_methods"    : cl[ "fmri_evidence_methods" ] ,
			"rival_modality_methods"   : cl[ "rival_modality_methods" ] ,
			"multimodal"               : bool( cl[ "multimodal" ] ) ,
			"attention_hits_methods"   : cl[ "attn_methods" ] ,
			"pretrained_transformer_hits" : cl[ "attn_pretrained" ] ,
			"attention_attributed_to_authors" : bool( cl[ "attn_attributed_to_authors" ] ) ,
			"decoding_score"           : cl[ "decode_score" ] ,
			"attention_evidence"       : cl[ "attn_evidence" ][ :1500 ] ,
			"task_evidence"            : cl[ "task_evidence" ][ :1500 ] ,
			"abstract_chars"           : cl[ "abstract_chars" ] ,
		} ,
	}


def nearmiss_record( e , cl , miss ):
	"""One paper that failed exactly one criterion. Thin on purpose -- there are
	hundreds , they are a shortlist to eyeball rather than papers to read here ,
	and every one of them is one click from its own abstract."""
	return {
		"key"       : e[ "key" ] ,
		"wid"       : e[ "wid" ] ,
		"doi"       : e[ "doi" ] ,
		"title"     : e[ "title" ] ,
		"year"      : e[ "year" ] ,
		"cited_by"  : e[ "cited_by" ] ,
		"authors"   : first_author( e[ "authors" ] ) ,
		"lib_cites" : e[ "lib_cites" ] ,
		"pools"     : list( e[ "pools" ] ) ,
		"pdf"       : e[ "pdf" ] ,
		"missing"   : miss ,
		"exclusion_reason" : cl[ "exclusion_reason" ] ,
		"best_task_guess"  : cl[ "task_category" ] ,
		"screening" : {
			"fmri_evidence_methods"  : cl[ "fmri_evidence_methods" ] ,
			"rival_modality_methods" : cl[ "rival_modality_methods" ] ,
			"attention_hits_methods" : cl[ "attn_total_methods" ] ,
			"decoding_score"         : cl[ "decode_score" ] ,
		} ,
	}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def measure_recall( args ):
	"""Re-screen the papers /review INCLUDED , on their abstracts alone , and
	return ( found , total ) -- how much of a full-text verdict survives when you
	only get to read the abstract.

	This whole page is framed on two numbers , and a number written into a
	docstring is a number that goes stale the first time somebody moves a
	threshold. So the recall is MEASURED on every build , against the review
	sitting on disk , and the page prints what came back rather than what this
	file once believed.

	Only recall. Precision needs the papers /review threw OUT , and the review
	document does not persist an abstract for those ( there is nothing on an
	excluded row to persist one on ) -- so that one stays a measured constant
	with its basis named beside it. Cheap either way : a few hundred papers at
	~2 ms each , against the minutes the pool itself costs.

	Returns ( 0 , 0 ) when there is no review to measure against , which the page
	reads as "not measured" rather than as a score of zero."""
	papers = ( review_build.load( args ) or {} ).get( "papers" ) or []
	pairs  = [ ( p.get( "title" ) or "" , p.get( "abstract" ) or "" )
		for p in papers if ( p.get( "abstract" ) or "" ).strip() ]
	if not pairs:
		return 0 , 0
	found = sum( 1 for t , a in pairs if CLS.classify_abstract( t , a )[ "include" ] )
	return found , len( pairs )


def _rank( r ):
	"""How near this paper is to your library , then how much the field cites it ,
	then how new. Connectivity leads because it is the one thing this pool knows
	that a literature search doesn't : forty of your papers cite this and you
	have never read it."""
	return ( -( r.get( "lib_cites" ) or 0 ) ,
	         -( r.get( "cited_by" )  or 0 ) ,
	         -( int( r[ "year" ] ) if str( r.get( "year" ) or "" ).isdigit() else 0 ) )


def build( args , pools=None , progress=None ):
	"""Screen the whole external pool and return the document.

	`pools` is the dashboard index's servable pools ; the server passes what it
	already holds , the CLI leaves it None and it is loaded from disk.
	`progress( stage , done , total )` is forwarded exactly as /review's is , so
	the page's progress bar is the same code."""
	def tick( stage , done , total ):
		if progress:
			try:
				progress( stage , done , total )
			except Exception:
				pass

	tick( "loading the dashboard index" , 0 , 0 )
	cands = load_pool( args , pools )
	total = len( cands )

	# The corpus-mined acquisition table off the built /review. An enhancement :
	# without a review there is simply no dataset fallback , and every acquisition
	# field falls back to what the abstract says , which is usually nothing.
	tick( "reading the dataset reference" , 0 , 0 )
	ds_rows  = ( review_build.load( args ) or {} ).get( "dataset_reference" ) or []
	ds_index = review_build.build_ds_index( ds_rows )

	# What this screen costs , re-measured now rather than quoted from a comment.
	tick( "measuring the screen against /review" , 0 , 0 )
	rec_found , rec_total = measure_recall( args )

	kept , misses = [] , []
	reasons  = Counter()
	no_abs   = 0
	n_screen = 0
	for i , e in enumerate( cands.values() , 1 ):
		if not e[ "abstract" ]:
			# Title only : OpenAlex has no abstract for this work , which is true
			# of about a third of the pool -- older papers and closed-access
			# publishers , mostly. There is nothing to screen , so it is counted
			# rather than judged , exactly as /review counts a paper the pipeline
			# never extracted text for. It is not "excluded" , and the page keeps
			# the two numbers apart.
			no_abs += 1
		else:
			n_screen += 1
			cl = screen( e )
			if cl[ "include" ]:
				kept.append( ( e , cl ) )
			else:
				reasons[ cl[ "exclusion_reason" ] ] += 1
				m = near_miss( cl )
				if m:
					misses.append( ( e , cl , m ) )
		if i % 2000 == 0 or i == total:
			tick( "screening the pool" , i , total )

	# Rank BEFORE the cap , so what a cap drops is the least connected rather
	# than whatever came last out of a dict.
	kept.sort(   key=lambda t: _rank( t[ 0 ] ) )
	misses.sort( key=lambda t: _rank( t[ 0 ] ) )
	n_kept_all , n_miss_all = len( kept ) , len( misses )
	kept , misses = kept[ :MAX_PAPERS ] , misses[ :MAX_NEARMISS ]

	# Only now , on the few hundred that survived , is it worth going back to
	# disk for the abstract as OpenAlex punctuated it.
	tick( "extracting from the survivors" , 0 , len( kept ) )
	papers = []
	for n , ( e , cl ) in enumerate( kept , 1 ):
		v = verbatim_abstract( args , e[ "wid" ] )
		e[ "abstract_source" ] = "openalex" if v else "index"
		papers.append( paper_record( n , e , cl , v or e[ "abstract" ] , ds_index ) )
		if n % 50 == 0 or n == len( kept ):
			tick( "extracting from the survivors" , n , len( kept ) )

	tick( "serialising" , 0 , 0 )
	nearmiss = [ nearmiss_record( e , cl , m ) for e , cl , m in misses ]

	stats = {
		"candidates"   : total ,                       # everything in both pools
		"with_text"    : n_screen ,                    # …that had an abstract to read
		"no_abstract"  : no_abs ,
		"included"     : len( papers ) ,
		"included_all" : n_kept_all ,                  # before MAX_PAPERS
		"excluded"     : sum( reasons.values() ) ,
		"borderline"   : n_miss_all ,                  # failed exactly one criterion
		"from_references" : sum( 1 for e in cands.values() if "references" in e[ "pools" ] ) ,
		"from_cited_by"   : sum( 1 for e in cands.values() if "cited_by"   in e[ "pools" ] ) ,
		"from_both"       : sum( 1 for e in cands.values() if len( e[ "pools" ] ) > 1 ) ,
	}

	return {
		"meta": {
			"generated"      : time.strftime( "%Y-%m-%dT%H:%M:%S" , time.localtime() ) ,
			"generator"      : "src/review/missing.py" ,
			"schema_version" : SCHEMA_VERSION ,
			"criteria"       : CRITERIA ,
			"counts"         : stats ,
			"caps"           : { "papers": MAX_PAPERS , "near_misses": MAX_NEARMISS } ,
			"exclusion_tally": dict( reasons.most_common() ) ,
			"sources"        : {
				"dashboard_index" : str( _index_path( args ) ) ,
				"review"          : str( review_build.review_path( args ) ) ,
			} ,
			"priority_legend": { str( v ): k for k , v in PRIORITY.items() } ,
			"field_groups"   : {
				"architecture"  : ARCH_COLS ,
				"acquisition"   : ACQ_COLS ,
				"preprocessing" : PRE_COLS ,
			} ,
			# What this page is , said where the document itself can be read.
			# These numbers are measured ( see the module docstring ) , not claimed.
			"screen": {
				"text"      : "title + abstract" ,
				# MEASURED on this build : the same screen re-run over the papers
				# /review included , on their abstracts alone.
				"recall"        : ( round( rec_found / rec_total , 3 ) if rec_total else None ) ,
				"recall_found"  : rec_found ,
				"recall_total"  : rec_total ,
				"recall_basis"  : ( f"re-screened the {rec_total} papers /review included , "
				                    "on their abstracts alone , during this build" if rec_total
				                    else "no built /review to measure against" ) ,
				# Measured once , and NOT recomputable here : precision needs the
				# papers /review threw out , and an excluded row carries no abstract
				# to re-screen. Recorded with its basis so it can be re-derived.
				"precision"       : 0.94 ,
				"precision_basis" : "measured over the 377 papers of the built /review that have an "
				                    "OpenAlex abstract : 52 of the 55 this screen let through were "
				                    "also included by the full-text screen" ,
				"caveat"    : "An abstract-only screen. Most of what it misses is papers whose "
				              "abstract never names an architecture at all -- there is no threshold "
				              "that recovers a word the abstract does not contain. Read this as a "
				              "shortlist of candidates , never as a review." ,
			} ,
			"notes": {
				"blank_cells" : "An empty value means the ABSTRACT does not state it. Most of "
				                "these papers state their scanner settings in a methods section "
				                "nobody here has read , so most acquisition fields are blank and "
				                "that says nothing about the paper." ,
				"inferred"    : "Acquisition fields may carry source=\"inferred-from-dataset\" -- "
				                "the consensus value the built /review mined from your own corpus "
				                "for the public dataset this abstract names." ,
				"evidence"    : "Every extracted field carries the verbatim quote it was parsed from." ,
				"excluded"    : "The excluded are tallied , not listed : there are too many to be a "
				                "table. What is listed is the near misses -- papers that satisfied "
				                "two criteria and failed exactly one." ,
			} ,
		} ,
		"dataset_reference" : ds_rows ,
		"papers"            : papers ,
		"near_misses"       : nearmiss ,
	}


# ---------------------------------------------------------------------------
# Persistence + staleness -- the same five functions ReviewState drives /review
# through , so the server holds both surfaces with one class.
# ---------------------------------------------------------------------------

def _index_path( args ):
	from ..dashboard import indexer
	return indexer.store_path( args )


def save( args , doc ):
	p = missing_path( args )
	p.parent.mkdir( parents=True , exist_ok=True )
	utils.write_json( p , doc )
	return p


def load( args ):
	p = missing_path( args )
	if not p.exists():
		return None
	try:
		return utils.read_json( p )
	except Exception as e:
		print( f"review-missing :: {p.name} could not be read ( {e} )" )
		return None


def signature( args ):
	"""'Have the inputs moved' , as a few stat() calls.

	The pool is the dashboard index , so its mtime is the whole story : a
	` prma reindex ` that finds new references is exactly what makes this stale.
	The built review is in here because its dataset table fills the acquisition
	blanks , and the modules because a pattern added to any of them changes every
	value on the page -- the same reasoning as review_build.signature."""
	def mtime( p ):
		try:
			return f"{os.stat( p ).st_mtime:.3f}"
		except Exception:
			return "0"
	here = os.path.dirname( os.path.abspath( __file__ ) )
	return ":".join( [
		mtime( _index_path( args ) ) ,
		mtime( review_build.review_path( args ) ) ,
	] + [ mtime( os.path.join( here , f ) ) for f in
		( "missing.py" , "classify.py" , "extract.py" , "datasets.py" ) ] )


def is_stale( args , doc=None ):
	doc = load( args ) if doc is None else doc
	if not doc:
		return True
	meta = doc.get( "meta" ) or {}
	if meta.get( "schema_version" ) != SCHEMA_VERSION:
		return True
	return meta.get( "input_signature" ) != signature( args )


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def run( args ):
	"""` prma review-missing ` : screen the pool and persist the document."""
	print( "REVIEW-MISSING :: reading the dashboard index …" )
	last = [ 0.0 ]
	def progress( stage , done , total ):
		now = time.time()
		if total and done < total and now - last[ 0 ] < 1.0:
			return
		last[ 0 ] = now
		print( f"REVIEW-MISSING :: {stage}" + ( f"  {done}/{total}" if total else "" ) )

	doc = build( args , progress=progress )
	doc[ "meta" ][ "input_signature" ] = signature( args )
	p = save( args , doc )

	c = doc[ "meta" ][ "counts" ]
	cut = c[ "included_all" ] - c[ "included" ]
	print(
		f"REVIEW-MISSING :: {p}\n"
		f"          {c[ 'candidates' ]} in the pool "
		f"( {c[ 'from_references' ]} cited by your papers , {c[ 'from_cited_by' ]} citing them , "
		f"{c[ 'from_both' ]} both ) ; {c[ 'with_text' ]} with an abstract , "
		f"{c[ 'no_abstract' ]} without\n"
		f"          CANDIDATES {c[ 'included' ]}"
		+ ( f" ( of {c[ 'included_all' ]} -- {cut} past the cap )" if cut else "" ) +
		f"   excluded {c[ 'excluded' ]}   {c[ 'borderline' ]} near misses\n"
		f"          served at /review-missing by ` prma server `"
	)
	return doc
