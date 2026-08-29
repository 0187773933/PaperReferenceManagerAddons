"""
The /review compute : turn the two hand-curated surfaces into a screened ,
field-extracted literature review.

WHAT THIS IS. /sort is a list of papers somebody decided were worth keeping ,
and /images is a set of figures somebody decided were worth looking at. Both are
judgements , and neither says anything about what the papers CONTAIN. This reads
the union of the two , screens each paper against a fixed set of inclusion
criteria , and pulls every architecture / acquisition / preprocessing detail the
paper's own text states -- with the verbatim quote behind each one , so nothing
here has to be taken on trust.

INPUTS -- all of them already on the server , none of them exported by hand :

  the sort board      src/db/sortboard.py  ( output/cache/sort.json )
                      -> key , title , year , tags , the Notes column
  the /images picks   src/db/figure_state.py ( output/cache/images-state.json )
                      -> the selected figure ids , resolved back to their
                         captions + modality stamp through the papers DB
  output/md/<k>.md        the full paper text ` prma md ` wrote
  output/methods/<k>.txt  the isolated methods section ` prma methods ` wrote

A paper reaches the review from EITHER surface : on the sort board , or with at
least one figure picked on /images. Papers with no extracted text are dropped
( there is nothing to screen ) and counted.

INCLUSION -- all three must hold ( see classify.py , which scores each with
quotable evidence ) :

  1. fMRI/BOLD is the DOMINANT modality in the methods. Mentioning fMRI is not
     enough ; an EEG/MEG/ECoG/fNIRS paper that cites it is excluded.
  2. The paper DECODES : fMRI in , stimulus / mental content / class label out.
     Encoding models ( stimulus -> brain ) and MRI reconstruction / acceleration
     are excluded.
  3. Transformer or attention machinery sits inside the model the AUTHORS BUILT.
     Citing Vaswani in related work is not enough.

OUTPUT ( output/cache/review.json ) -- one self-describing document holding the
meta block , the corpus-mined dataset acquisition reference , every included
paper and every excluded one. It is what GET /api/review serves and what
src/dashboard/review.html renders ; ` prma review ` writes the same file.

Every extracted field is the SAME SHAPE , which is what lets the page render all
of them with one renderer :

  { "value": "40 heads" ,             what to display
    "values": [ "40 heads" , ... ] ,  ranked by how often the paper says it
    "counts": { "40 heads": 3 } ,
    "evidence": [ "...verbatim quote..." ] ,
    "source": "curated" | "auto" | "inferred-from-dataset" | "absent" ,
    "inferred_from": "inferred from NSD (n=27 papers)" }   acquisition only

A BLANK VALUE MEANS THE PAPER DOES NOT SAY IT , never that the value is zero.

Hand-verified detail lives in config/review-overrides.json , keyed by the same
paper key the boards use. Anything in there wins over extraction and flags the
row 'human-curated + auto'.

COST : a full build is a regex pass over every candidate's text -- minutes , not
seconds , on a few hundred papers. So it is built ONCE and persisted , and the
server rebuilds it in a background thread ( see src/server/server.py ) rather
than on the request that noticed it was stale.
"""

import os
import re
import time
from collections import OrderedDict

from ..db    import figure_state , papers as papers_db , sortboard
from ..utils import utils
from .       import classify as CLS
from .       import datasets as DS
from .extract import ACQ_PATTERNS , ARCH_PATTERNS , PREPROC_PATTERNS , clean , extract_block


SCHEMA_VERSION = 1

MAX_TEXT = 150_000     # guard against a runaway pdf-to-text dump
EV_QUOTES = 3          # verbatim quotes kept per field
EV_CHARS  = 600        # max chars per quote

ARCH_COLS = list( ARCH_PATTERNS.keys()    )
ACQ_COLS  = list( ACQ_PATTERNS.keys()     )
PRE_COLS  = list( PREPROC_PATTERNS.keys() )

# Task category -> priority for an inner-speech decoding project. 1 is the
# bullseye ; 5 is "an fMRI transformer paper , but not about decoding content".
PRIORITY = OrderedDict( [
	( 'inner/imagined speech'                     , 1 ) ,
	( 'language / semantic decoding'              , 2 ) ,
	( 'visual reconstruction / decoding'          , 3 ) ,
	( 'audio / music decoding'                    , 3 ) ,
	( 'brain state / cognitive task decoding'     , 4 ) ,
	( 'individual fingerprinting / trait prediction' , 5 ) ,
	( 'clinical / psychiatric classification'     , 5 ) ,
	( 'unclassified'                              , 6 ) ,
] )

CRITERIA = [
	"fMRI/BOLD is the dominant modality in the methods section "
	"( EEG/MEG/ECoG/fNIRS/intracortical-dominant papers are excluded even when they mention fMRI )." ,
	"The paper performs a DECODING task : fMRI in , stimulus / mental content / class label out. "
	"Encoding models ( stimulus -> brain ) and MRI reconstruction / acceleration are excluded." ,
	"Transformer or attention machinery sits inside the model the AUTHORS BUILT -- self / cross-attention , "
	"a transformer backbone , or a pretrained transformer ( CLIP / GPT / LLaMA / ViT / Whisper ) inside the "
	"decoding pipeline. Papers that only cite transformers in related work are excluded." ,
]


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def review_path( args ):
	"""Where the built review lands. Next to the other derived documents in
	output/cache/ , because it IS derived -- delete it and ` prma review `
	rebuilds it from the boards."""
	return args.output.joinpath( "cache" , "review.json" )


def overrides_path( args ):
	"""The hand-verified detail file. Alongside config/method-images.txt , for
	the same reason : it is content somebody types , not code."""
	return args.config.joinpath( "review-overrides.json" )


def load_overrides( args , path=None ):
	"""{ paper key : { field : value } }. Missing / unreadable reads as empty --
	overrides are an enhancement , never a dependency. The '_README' key inside
	the file documents it to whoever opens it and is not a paper."""
	p = path or overrides_path( args )
	try:
		data = utils.read_json( p ) or {}
	except Exception as e:
		print( f"review :: {os.path.basename( str( p ) )} could not be read ( {e} ) -- continuing without overrides" )
		return {}
	return { k: v for k , v in data.items()
		if isinstance( v , dict ) and not k.startswith( "_" ) }


# ---------------------------------------------------------------------------
# Candidates : the union of the two curated surfaces
# ---------------------------------------------------------------------------

def modalities_read( args , paper ):
	"""The modality stamp ` prma modalities ` pinned on a record , or None. READ
	ONLY -- a paper the stage never reached simply arrives without modalities ,
	rather than being stamped from under a page request."""
	from ..tasks import modalities as modalities_task
	return modalities_task.read( args , paper )


def record_facts( args , paper ):
	"""The identity + ORDERING facts one paper record carries : what it is , when
	it entered the library , when it was published , and how often it has been
	cited.

	These are what the page sorts by , and they are the same three every other
	prma surface offers ( the figure reports' Recently added / Publication date ,
	the sort board's citations / when added ) -- so a list ordered here is
	ordered the way the rest of the project orders lists.

	The modality stamp is READ , never written : a paper ` prma modalities ` never
	reached simply arrives without pills rather than being stamped from under a
	page request."""
	from ..tasks import method_images as mi
	oa  = mi._oa_meta( args , paper.get( "doi" ) )
	pdf = paper.get( "pdf_path" ) or ""
	try:
		mods = modalities_read( args , paper ) or {}
	except Exception:
		mods = {}
	return dict(
		title      = ( paper.get( "title" ) or "" ).strip() ,
		doi        = utils.normalize_doi( paper.get( "doi" ) ) or "" ,
		pdf        = pdf if pdf and os.path.exists( pdf ) else "" ,
		modalities = list( mods.get( "used" ) or [] ) ,
		# When YOU added it to the manager -- the figure reports' "Recently added".
		added      = paper.get( "created_at" ) or "" ,
		# When it was PUBLISHED : OpenAlex first , the manager's own date field
		# after , always as a sortable YYYY-MM-DD.
		published  = mi._publication_date( args , paper ) ,
		cited_by   = oa.get( "cited_by_count" ) ,
	)


def _blank( k ):
	return dict( key=k , doi="" , title="" , year="" , tags=set() , modalities=set() ,
		sources=set() , curator_notes=set() , figure_captions=[] , figures=[] ,
		pdf="" , prefix="" , md_path="" , methods_path="" , montage_path="" ,
		added="" , published="" , cited_by=None )


def load_candidates( args ):
	"""Every paper either curated surface names , keyed by the same paper key
	both of them use ( a normalized DOI , or a synthetic nodoi-... key ).

	The file stems under output/md and output/methods come from
	utils.doi_to_filename -- the function that WROTE them -- so a DOI carrying
	characters a stricter slug would have flattened ( '10.1016/S0079-6123(09)…' )
	still finds its text."""
	c = OrderedDict()

	# ---- the sort board : the list , not the staging shelf. A row on the shelf
	# is a bibliography entry nobody has placed yet , which is exactly the
	# judgement this whole page is built on top of.
	doc = sortboard.load( args )
	for row in ( doc.get( "items" ) or [] ):
		k = ( row.get( "key" ) or "" ).strip()
		if not k:
			continue
		e = c.setdefault( k , _blank( k ) )
		e[ "sources" ].add( "sort board" )
		e[ "title" ] = e[ "title" ] or ( row.get( "title" ) or "" ).strip()
		e[ "doi"   ] = e[ "doi"   ] or ( row.get( "doi"   ) or "" ).strip()
		if not e[ "year" ] and row.get( "year" ):
			e[ "year" ] = str( row[ "year" ] ).strip()
		e[ "tags" ].update( t for t in ( row.get( "tags" ) or [] ) if t )
		# Every free-text column the board carries , not just Notes : the column
		# set is the user's , so a second one they added is curator detail too.
		for v in ( row.get( "fields" ) or {} ).values():
			if isinstance( v , str ) and v.strip():
				e[ "curator_notes" ].add( v.strip() )

	# ---- the /images picks
	seen_record = set()
	for key , fig in _selected_figures( args ).items():
		e = c.setdefault( key , _blank( key ) )
		seen_record.add( key )
		e[ "sources" ].add( "selected figures" )
		e[ "title" ] = e[ "title" ] or fig[ "title" ]
		e[ "doi"   ] = e[ "doi"   ] or fig[ "doi"   ]
		e[ "modalities" ].update( fig[ "modalities" ] )
		e[ "pdf" ] = e[ "pdf" ] or fig[ "pdf" ]
		for f in ( "added" , "published" , "cited_by" ):
			e[ f ] = e[ f ] if e[ f ] not in ( "" , None ) else fig[ f ]
		e[ "figures" ] = fig[ "figures" ]
		e[ "figure_captions" ] = [
			f"[Fig {f[ 'figure' ]}] {clean( f[ 'caption' ] )[ :600 ]}"
			for f in fig[ "figures" ] if f[ "caption" ] ]

	# A row that came ONLY off the sort board has never had its record read , so
	# it doesn't yet know whether it has a PDF or which modalities it was stamped
	# with. One read each fills that in ; the figure rows above already did it.
	for k , e in c.items():
		if k in seen_record:
			continue
		paper = papers_db.load( args , k )
		if paper is None:
			continue
		facts = record_facts( args , paper )
		e[ "title" ] = e[ "title" ] or facts[ "title" ]
		e[ "doi"   ] = e[ "doi"   ] or facts[ "doi"   ]
		e[ "pdf"   ] = facts[ "pdf" ]
		e[ "modalities" ].update( facts[ "modalities" ] )
		for f in ( "added" , "published" , "cited_by" ):
			e[ f ] = facts[ f ]

	for k , e in c.items():
		# The sort board carries a year ; a paper that arrived only through a
		# picked figure has none , and half a list of blank years is not a list
		# you can order by year. The publication date fills those in.
		if not e[ "year" ] and e[ "published" ][ :4 ].isdigit():
			e[ "year" ] = e[ "published" ][ :4 ]
		prefix = utils.doi_to_filename( k ) or ""
		e[ "prefix" ] = prefix
		md = args.output.joinpath( "md"      , f"{prefix}.md"  )
		me = args.output.joinpath( "methods" , f"{prefix}.txt" )
		# The contact sheet ` prma images ` writes : every figure in the paper on
		# one page. Same file the dashboard and both boards link as "Figures".
		mo = args.output.joinpath( "images"  , f"{prefix}-Figures.png" )
		e[ "md_path"      ] = str( md ) if prefix and md.exists() else ""
		e[ "methods_path" ] = str( me ) if prefix and me.exists() else ""
		e[ "montage_path" ] = str( mo ) if prefix and mo.exists() else ""
	return c


def _selected_figures( args ):
	"""The /images selection , resolved back into detail.

	The store keeps only figure IDS ( '<paper-key>#figure-<N>' ) -- ordered , and
	deliberately thin , so a regenerated report re-adopts them. The caption ,
	page and modality stamp behind each one live on the paper record , so this
	re-does the SAME figure <-> caption pairing ` prma images ` cropped with
	( shared from src/tasks/method_images.py , so the text here is the text
	inside the crop and on the /images card ).

	Read-only : a paper the ` modalities ` stage never stamped simply arrives
	without modalities , rather than being stamped from under a page request."""
	from ..pdf   import images as images_mod , md as md_mod , pdf as pdf_mod , preprocess as PP
	from ..tasks import method_images as mi

	selected , _skipped = figure_state.load( args , figure_state.ALL_IMAGES )
	want = OrderedDict()
	for fid in selected:
		key , sep , seq = str( fid ).rpartition( "#figure-" )
		if not sep or not key or not seq.isdigit():
			continue
		want.setdefault( key , [] ).append( int( seq ) )

	out = OrderedDict()
	for key , seqs in want.items():
		paper = papers_db.load( args , key )
		if paper is None:
			# A pick whose paper has since left the library. Keep the key -- the
			# sort board may still carry it , and the text may still be on disk.
			out[ key ] = dict( title="" , doi="" , pdf="" , modalities=[] , figures=[] ,
				added="" , published="" , cited_by=None )
			continue
		yolo  = paper.get( "yolo" ) or {}
		pages = yolo.get( "pages" ) or []
		dpi   = ( yolo.get( "meta" ) or {} ).get( "dpi" ) or pdf_mod.DPI
		scale = images_mod.CROP_DPI / float( dpi )
		figure_idx , _tables = md_mod._build_image_index( pages )

		caps = {}
		for page_idx , page_dets in enumerate( pages ):
			for det_idx , caption in mi._page_figures_with_captions(
					page_dets , scale , images_mod , PP , md_mod ):
				seq = figure_idx.get( ( page_idx , det_idx ) )
				if seq is not None:
					caps[ seq ] = ( page_idx , caption )

		figs = []
		for seq in sorted( set( seqs ) ):
			page , caption = caps.get( seq , ( None , "" ) )
			figs.append( { "figure": seq , "page": page , "caption": caption } )
		out[ key ] = dict( record_facts( args , paper ) , figures=figs )
	return out


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def read_text( path , cap=MAX_TEXT ):
	if not path:
		return ""
	try:
		with open( path , encoding="utf-8" , errors="ignore" ) as fh:
			return fh.read( cap )
	except OSError:
		return ""


def strip_refs( md ):
	"""Drop everything from the bibliography on : a reference list is a list of
	OTHER papers' titles , and every one of them would score as evidence."""
	m = re.search( r'^#{1,4}\s*(references|bibliography|works cited)\s*$' , md , re.I | re.M )
	return md[ :m.start() ] if m else md


def get_abstract( md ):
	m = re.search( r'^#{1,4}\s*abstract\s*$' , md , re.I | re.M )
	return md[ m.start(): m.start() + 3000 ] if m else md[ :2500 ]


def unlabel( text ):
	"""Strip the LABEL lines off the front of an abstract. The slice above starts
	AT the '## Abstract' heading , and the PDF text underneath very often carries
	its own second one , so without this every abstract on the page opens by
	saying "Abstract" twice. Display only -- the screener is handed the raw slice
	( the word "abstract" scores nothing either way )."""
	lines = ( text or "" ).split( "\n" )
	while lines and ( re.match( r'^\s{0,3}#{1,6}\s' , lines[ 0 ] )
			or re.match( r'^\s*abstract\s*[:.]?\s*$' , lines[ 0 ] , re.I )
			or not lines[ 0 ].strip() ):
		lines.pop( 0 )
	return "\n".join( lines )


# ---------------------------------------------------------------------------
# Per-paper screening + extraction
# ---------------------------------------------------------------------------

def process( entry , curated ):
	"""Screen one paper and pull every field out of its text.

	Methods section first ( the most reliable place a paper states a number ) ,
	then the whole paper. The markdown already CONTAINS the methods prose , so
	nothing re-appends section slices on top -- that only tripled the regex
	workload without finding anything new."""
	md       = strip_refs( read_text( entry[ "md_path" ] ) )
	methods  = read_text( entry[ "methods_path" ] )
	abstract = get_abstract( md )
	scan     = ( methods + "\n" + md )[ :MAX_TEXT ]

	cl = CLS.classify( entry[ "title" ] , abstract , methods , md ,
		entry[ "tags" ] , entry[ "modalities" ] )

	return dict(
		entry    = entry ,
		cl       = cl ,
		arch     = extract_block( scan , ARCH_PATTERNS    ) ,
		acq      = extract_block( scan , ACQ_PATTERNS     ) ,
		pre      = extract_block( scan , PREPROC_PATTERNS ) ,
		datasets = DS.detect( md ) ,
		curated  = curated.get( entry[ "key" ] , {} ) ,
		has_text = bool( md ) ,
		n_md     = len( md ) ,
		n_methods= len( methods ) ,
		abstract = clean( unlabel( abstract ) )[ :4000 ] ,
	)


def model_name( entry , curated , abstract="" ):
	"""What the authors CALL their model. A named model is the single most
	useful handle on a paper in a list this long , and papers name themselves in
	the title far more often than anywhere else."""
	if curated.get( "Model_Name" ):
		return curated[ "Model_Name" ]
	t = entry[ "title" ]
	# "MindEye2: Shared-Subject Models…" -> MindEye2
	m = re.match( r'^([A-Z][A-Za-z0-9+\-]{1,24}(?:\s?[A-Z][A-Za-z0-9]*)?)\s*[:—-]\s' , t )
	if m:
		return m.group( 1 ).strip()
	m = re.search( r'\b(?:we (?:propose|present|introduce)|call(?:ed)? it|named?)\s+([A-Z][A-Za-z0-9+\-]{2,24})\b' , t )
	if m:
		return m.group( 1 )
	# Fall back to the abstract : "we propose X , a …" / "We introduce X:"
	m = re.search( r'\b[Ww]e (?:propose|present|introduce|develop|design|build)\s+'
		r'(?:a\s+(?:novel\s+)?(?:model|framework|method)\s+(?:called|named|termed)\s+)?'
		r'([A-Z][A-Za-z0-9]*(?:[A-Z0-9][A-Za-z0-9+\-]*)+)\b' , abstract or "" )
	return m.group( 1 ) if m else ""


# ---------------------------------------------------------------------------
# Acquisition fallback : fill blanks from the corpus-mined dataset consensus
# ---------------------------------------------------------------------------

# Acquisition field -> the parameter name the dataset consensus table uses.
DS_FALLBACK = {
	"Field_Strength"       : "Field_Strength" ,
	"Scanner_Vendor_Model" : "Scanner" ,
	"TR"                   : "TR" ,
	"TE"                   : "TE" ,
	"Flip_Angle"           : "Flip_Angle" ,
	"Voxel_Size"           : "Voxel_Size" ,
	"Slices"               : "N_Slices" ,
	"Multiband_SMS"        : "Multiband" ,
}


def build_ds_index( ds_rows ):
	"""{ ( dataset , parameter ) : ( value , n_papers ) } from the consensus rows."""
	return { ( r[ "Dataset" ] , r[ "Parameter" ] ): ( r[ "Consensus_Value" ] , r[ "N_Papers_Reporting" ] )
		for r in ds_rows }


def resolve_acq( field , own_value , datasets_used , ds_index ):
	"""( value , provenance ). A value the paper states always wins. If the paper
	is silent and it uses exactly ONE dataset we have consensus data for , fall
	back to that consensus and say so ; a multi-dataset paper is left blank
	rather than guessed."""
	if own_value:
		return own_value , "stated in paper"
	param = DS_FALLBACK.get( field )
	if not param:
		return "" , ""
	hits = [ ( ds , ) + ds_index[ ( ds , param ) ] for ds in datasets_used if ( ds , param ) in ds_index ]
	if len( hits ) != 1:
		return "" , ( "multiple datasets — see the dataset reference" if len( hits ) > 1 else "" )
	ds , val , n = hits[ 0 ]
	return f"{val}" , f"inferred from {ds} (n={n} papers)"


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def field_json( f , curated_value=None , extra=None ):
	"""One extracted field , in the uniform shape the page's single renderer
	keys off ( see the module docstring )."""
	out = {
		"value"    : curated_value if curated_value else ( f.values[ 0 ] if f else "" ) ,
		"values"   : [ curated_value ] if curated_value else list( f.values ) ,
		"counts"   : {} if curated_value else dict( f.counts ) ,
		"evidence" : [ q[ :EV_CHARS ] for q in list( f.spans.values() )[ :EV_QUOTES ] ] ,
		"source"   : "curated" if curated_value else ( "auto" if f else "absent" ) ,
	}
	if not curated_value and f and len( f.values ) > 1:
		out[ "all_values" ] = " | ".join( f.values )
	if extra:
		out.update( extra )
	return out


def build_records( included , excluded , ds_rows ):
	"""Processed papers -> the JSON-ready `papers` and `excluded` lists."""
	ds_index = build_ds_index( ds_rows )
	papers = []
	for n , r in enumerate( included , 1 ):
		e , cl , ov = r[ "entry" ] , r[ "cl" ] , r[ "curated" ]

		arch = { c: field_json( r[ "arch" ][ c ] , ov.get( c ) ) for c in ARCH_COLS }
		pre  = { c: field_json( r[ "pre"  ][ c ] , ov.get( c ) ) for c in PRE_COLS  }

		acq = {}
		for c in ACQ_COLS:
			own = ov.get( c ) or ( r[ "acq" ][ c ].values[ 0 ] if r[ "acq" ][ c ] else "" )
			val , prov = resolve_acq( c , own , r[ "datasets" ] , ds_index )
			extra = {}
			if prov and prov != "stated in paper":
				extra[ "inferred_from" ] = prov
			fj = field_json( r[ "acq" ][ c ] , ov.get( c ) , extra )
			if not fj[ "value" ] and val:
				fj[ "value" ]  = val
				fj[ "values" ] = [ val ]
				fj[ "source" ] = "inferred-from-dataset"
			elif prov and prov.startswith( "multiple" ):
				fj[ "note" ] = prov
			acq[ c ] = fj

		papers.append( {
			"n"             : n ,
			"key"           : e[ "key" ] ,
			"doi"           : e[ "doi" ] or _doi_from_key( e[ "key" ] ) ,
			"title"         : e[ "title" ] ,
			"year"          : e[ "year" ] ,
			# What the page offers to order by , alongside priority : the same
			# three keys the rest of prma sorts on.
			"added"         : e[ "added" ] ,
			"published"     : e[ "published" ] ,
			"cited_by"      : e[ "cited_by" ] ,
			"priority"      : PRIORITY.get( cl[ "task_category" ] , 6 ) ,
			"task_category" : cl[ "task_category" ] ,
			"model_name"    : model_name( e , ov , r[ "abstract" ] ) ,
			"decoding_target": ov.get( "Decoding_Target" , "" ) ,
			"attention_role": ov.get( "Attention_Role" , cl[ "attention_role" ] ) ,
			"detail_source" : "human-curated + auto" if ov else "auto-extracted" ,
			"curated"       : bool( ov ) ,
			"datasets_used" : list( r[ "datasets" ] ) ,
			"tags"          : sorted( e[ "tags" ] ) ,
			"modalities"    : sorted( e[ "modalities" ] ) ,
			"curator_notes" : sorted( e[ "curator_notes" ] ) ,
			"curated_lists" : sorted( e[ "sources" ] ) ,
			"abstract"      : r[ "abstract" ] ,
			"summary"       : {
				"architecture_summary" : ov.get( "Architecture_Summary" , "" ) ,
				"pipeline_stages"      : ov.get( "Pipeline_Stages" , "" ) ,
				"input_representation" : ov.get( "Input_Representation" , "" ) ,
			} ,
			"architecture"  : arch ,
			"acquisition"   : acq ,
			"preprocessing" : pre ,
			"figures"       : list( e[ "figure_captions" ] ) ,
			"screening"     : {
				"fmri_evidence_total"    : cl[ "fmri_score" ] ,
				"fmri_evidence_methods"  : cl[ "fmri_evidence_methods" ] ,
				"rival_modality_methods" : cl[ "rival_modality_methods" ] ,
				"multimodal"             : bool( cl[ "multimodal" ] ) ,
				"attention_hits_methods" : cl[ "attn_methods" ] ,
				"pretrained_transformer_hits" : cl[ "attn_pretrained" ] ,
				"attention_attributed_to_authors" : bool( cl[ "attn_attributed_to_authors" ] ) ,
				"decoding_score"         : cl[ "decode_score" ] ,
				"attention_evidence"     : cl[ "attn_evidence" ][ :1500 ] ,
				"task_evidence"          : cl[ "task_evidence" ][ :1500 ] ,
				"md_chars"               : r[ "n_md" ] ,
				"methods_chars"          : r[ "n_methods" ] ,
			} ,
			"files"         : _files( e ) ,
		} )

	dropped = []
	for r in sorted( excluded , key=lambda r: ( r[ "cl" ][ "exclusion_reason" ] ,
			r[ "entry" ][ "title" ].lower() ) ):
		cl = r[ "cl" ]
		dropped.append( {
			"key"      : r[ "entry" ][ "key" ] ,
			"doi"      : r[ "entry" ][ "doi" ] or _doi_from_key( r[ "entry" ][ "key" ] ) ,
			"title"    : r[ "entry" ][ "title" ] ,
			"year"     : r[ "entry" ][ "year" ] ,
			"added"    : r[ "entry" ][ "added" ] ,
			"published": r[ "entry" ][ "published" ] ,
			"cited_by" : r[ "entry" ][ "cited_by" ] ,
			"exclusion_reason" : cl[ "exclusion_reason" ] or (
				"no paper text found" if not r[ "has_text" ] else "" ) ,
			# Worth a human look : excluded ONLY for weak attention evidence , and
			# it did have some. This is where a false negative hides.
			"borderline" : bool( cl[ "exclusion_reason" ].startswith( "no transformer" ) and
				1 <= cl[ "attn_total_methods" ] <= 2 ) ,
			"best_task_guess" : cl[ "task_category" ] ,
			"tags"      : sorted( r[ "entry" ][ "tags" ] ) ,
			"curated_lists" : sorted( r[ "entry" ][ "sources" ] ) ,
			"screening" : {
				"fmri_evidence_methods"  : cl[ "fmri_evidence_methods" ] ,
				"rival_modality_methods" : cl[ "rival_modality_methods" ] ,
				"attention_hits_methods" : cl[ "attn_total_methods" ] ,
				"decoding_score"         : cl[ "decode_score" ] ,
			} ,
			"files"     : _files( r[ "entry" ] ) ,
		} )
	return papers , dropped


def _doi_from_key( key ):
	"""A paper key IS its DOI unless it is one of the synthetic nodoi-… ones."""
	return "" if papers_db.is_synthetic_key( key ) else key


def _files( e ):
	"""WHICH text a paper has -- not where to link it. Composing URLs is the
	page's job ( review.html builds them next to its PROXY constant ) , so this
	carries the facts and the file stem the /md and /methods routes take."""
	return {
		"prefix"  : e[ "prefix" ] ,
		"md"      : bool( e[ "md_path"      ] ) ,
		"methods" : bool( e[ "methods_path" ] ) ,
		"montage" : bool( e[ "montage_path" ] ) ,
		"pdf"     : bool( e[ "pdf"          ] ) ,
	}


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build( args , min_year=0 , priority_only=False , overrides=None , progress=None ):
	"""Read both boards , screen and extract , and return the review document.

	`progress` , when given , is called as progress( stage , done , total ) so a
	caller with somewhere to show it ( the server's background rebuild ) can.
	"""
	def tick( stage , done , total ):
		if progress:
			try:
				progress( stage , done , total )
			except Exception:
				pass

	curated = load_overrides( args ) if overrides is None else overrides

	tick( "loading the boards" , 0 , 0 )
	cands = load_candidates( args )
	total = len( cands )

	recs = []
	for i , ( _k , e ) in enumerate( cands.items() , 1 ):
		recs.append( process( e , curated ) )
		if i % 10 == 0 or i == total:
			tick( "screening + extracting" , i , total )

	tick( "mining dataset acquisition consensus" , 0 , 0 )
	corpus  = [ ( r[ "entry" ][ "key" ] , read_text( r[ "entry" ][ "md_path" ] ) )
		for r in recs if r[ "entry" ][ "md_path" ] ]
	ds_rows = DS.consensus_rows( DS.mine_corpus( corpus ) , min_papers=2 , top_k=3 )

	included = [ r for r in recs if r[ "cl" ][ "include" ] and r[ "has_text" ] ]
	if min_year:
		included = [ r for r in included if not r[ "entry" ][ "year" ] or
			( r[ "entry" ][ "year" ].isdigit() and int( r[ "entry" ][ "year" ] ) >= min_year ) ]
	if priority_only:
		included = [ r for r in included if PRIORITY.get( r[ "cl" ][ "task_category" ] , 6 ) <= 4 ]
	# Bullseye first , then newest , then alphabetical -- the order somebody
	# reading the list top-down would want.
	included.sort( key=lambda r: ( PRIORITY.get( r[ "cl" ][ "task_category" ] , 6 ) ,
		-( int( r[ "entry" ][ "year" ] ) if r[ "entry" ][ "year" ].isdigit() else 0 ) ,
		r[ "entry" ][ "title" ].lower() ) )
	excluded = [ r for r in recs if not ( r[ "cl" ][ "include" ] and r[ "has_text" ] ) ]

	tick( "serialising" , 0 , 0 )
	papers , dropped = build_records( included , excluded , ds_rows )

	stats = {
		"candidates" : len( recs ) ,
		"with_text"  : sum( 1 for r in recs if r[ "has_text" ] ) ,
		"included"   : len( included ) ,
		"excluded"   : len( excluded ) ,
		"borderline" : sum( 1 for d in dropped if d[ "borderline" ] ) ,
		"from_sort"      : sum( 1 for r in recs if "sort board"       in r[ "entry" ][ "sources" ] ) ,
		"from_figures"   : sum( 1 for r in recs if "selected figures" in r[ "entry" ][ "sources" ] ) ,
		"from_both"      : sum( 1 for r in recs if len( r[ "entry" ][ "sources" ] ) > 1 ) ,
	}

	return {
		"meta": {
			"generated"      : time.strftime( "%Y-%m-%dT%H:%M:%S" , time.localtime() ) ,
			"generator"      : "src/review/build.py" ,
			"schema_version" : SCHEMA_VERSION ,
			"criteria"       : CRITERIA ,
			"counts"         : stats ,
			"sources"        : {
				"sort_board"       : str( sortboard.sort_path( args ) ) ,
				"selected_figures" : str( figure_state.state_path( args , figure_state.ALL_IMAGES ) ) ,
				"md_dir"           : str( args.output.joinpath( "md" ) ) ,
				"methods_dir"      : str( args.output.joinpath( "methods" ) ) ,
				"overrides"        : str( overrides_path( args ) ) ,
			} ,
			"filters"        : { "min_year": min_year or 0 , "priority_only": bool( priority_only ) } ,
			"priority_legend": { str( v ): k for k , v in PRIORITY.items() } ,
			"field_groups"   : {
				"architecture"  : ARCH_COLS ,
				"acquisition"   : ACQ_COLS ,
				"preprocessing" : PRE_COLS ,
			} ,
			"notes": {
				"blank_cells" : "An empty value means the paper does not state it , not that the value is zero." ,
				"inferred"    : "Acquisition fields may carry source=\"inferred-from-dataset\" with an "
				                "inferred_from string naming the public dataset the value came from. "
				                "Papers using several datasets are left blank rather than guessed." ,
				"evidence"    : "Every auto-extracted field carries the verbatim quotes it was parsed from." ,
				"ranking"     : "values[] is ordered by how often the paper states each value." ,
			} ,
		} ,
		"dataset_reference" : ds_rows ,
		"papers"            : papers ,
		"excluded"          : dropped ,
	}


def save( args , doc ):
	"""Persist the built review. Returns the path written."""
	p = review_path( args )
	p.parent.mkdir( parents=True , exist_ok=True )
	utils.write_json( p , doc )
	return p


def load( args ):
	"""The persisted review , or None when it was never built / won't parse."""
	p = review_path( args )
	if not p.exists():
		return None
	try:
		return utils.read_json( p )
	except Exception as e:
		print( f"review :: {p.name} could not be read ( {e} )" )
		return None


def signature( args ):
	"""A cheap 'have the inputs moved' token : the mtimes of everything the
	review is built OUT OF -- the two curated surfaces , the hand-verified
	overrides , and the extractor modules themselves.

	The boards are written whole on every save , so their mtimes are the whole
	story : no walk , no parse , a handful of stat() calls. The MODULES are in
	here for the same reason the figure reports watch their renderer ( see
	all_images.report_is_stale ) -- add a pattern to extract.py and every value
	on the page is potentially different , which is exactly what stale means.

	Stored in the built document , so telling a current review from a stale one
	costs those stat() calls and a string compare."""
	def mtime( p ):
		try:
			return f"{os.stat( p ).st_mtime:.3f}"
		except Exception:
			return "0"
	here  = os.path.dirname( os.path.abspath( __file__ ) )
	parts = [
		mtime( sortboard.sort_path( args ) ) ,
		mtime( figure_state.state_path( args , figure_state.ALL_IMAGES ) ) ,
		mtime( overrides_path( args ) ) ,
	] + [ mtime( os.path.join( here , f ) ) for f in
		( "build.py" , "classify.py" , "extract.py" , "datasets.py" ) ]
	return ":".join( parts )


def is_stale( args , doc=None ):
	"""True when the boards have moved since the review was built , or when the
	document on disk predates the CURRENT schema -- a shape change has to count
	as stale too , or an old file quietly renders wrong in a page written for
	the new one. What the server checks before serving , and what ` prma review `
	reports."""
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
	"""` prma review ` : build the review and persist it."""
	min_year      = getattr( args , "review_min_year" , 0 ) or 0
	priority_only = bool( getattr( args , "review_priority_only" , False ) )
	out           = getattr( args , "review_out" , None )

	print( "REVIEW :: reading the sort board + the /images selection …" )
	last = [ 0.0 ]
	def progress( stage , done , total ):
		now = time.time()
		if total and done < total and now - last[ 0 ] < 1.0:
			return
		last[ 0 ] = now
		print( f"REVIEW :: {stage}" + ( f"  {done}/{total}" if total else "" ) )

	doc = build( args , min_year=min_year , priority_only=priority_only , progress=progress )
	doc[ "meta" ][ "input_signature" ] = signature( args )

	if out:
		p = args.output.joinpath( out ) if not os.path.isabs( str( out ) ) else out
		utils.write_json( p , doc )
	else:
		p = save( args , doc )

	c = doc[ "meta" ][ "counts" ]
	print(
		f"REVIEW :: {p}\n"
		f"          {c[ 'candidates' ]} candidates "
		f"( {c[ 'from_sort' ]} on the sort board , {c[ 'from_figures' ]} with picked figures , "
		f"{c[ 'from_both' ]} both ) ; {c[ 'with_text' ]} with extracted text\n"
		f"          INCLUDED {c[ 'included' ]}   excluded {c[ 'excluded' ]}   "
		f"{c[ 'borderline' ]} borderline to eyeball\n"
		f"          served at /review by ` prma server `"
	)
	return doc
