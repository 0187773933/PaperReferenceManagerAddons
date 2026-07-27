"""
prma method-images [ keyword ... ] : fuzzy-search every extracted figure's
CAPTION for method keywords and write an HTML report of the matching
figure images.

Depends on the normal per-paper PDF suite ( yolo -> ocr -> images ->
methods -> code -> md -> modalities ) having run first -- via ` prma process ` /
` prma server --watch ` or the standalone commands -- because it reads :

  - paper[ 'yolo' ][ 'pages' ]      figure / figure_caption dets + OCR text
  - paper[ 'modalities' ]          the modality stamp ( pills + filter chips )
  - output/images/ALL/*.png        the crops ` prma images ` wrote
  - output/md , output/methods     linked from the report ( when present )

It never runs any stage inline and never snapshots -- like ` prma rollup `
it is a pure post-processing pass over what's already on disk , so it
just SKIPS ( and counts ) papers the pipeline hasn't reached yet.

Keywords come from the command line
  ` prma method-images transformer attention CNN "neural network" `
or , when none are given , one-per-line from config/method-images.txt
( '#' comments and blank lines ignored ; override with --keywords-file ).

Two TIERS , because the target is the model DESIGN figure ( architecture /
pipeline / layer-stack overview ) and a caption that merely NAMES a model is
not describing one -- "accuracy of our transformer" is a ROC curve :

  [strong]  structural vocabulary ( architecture , block diagram , schematic ,
            pipeline , encoder-decoder ) -> matches a caption on its own.
  [weak]    component / model-family vocabulary ( transformer , layer , LSTM ,
            attention ) -> only counts when --min-weak ( default 3 ) pieces of
            DISTINCT evidence co-occur in one caption ( see
            _distinct_evidence : one concept can't vote twice ).

Terms before any [section] header are strong , so a flat list still works ,
and command-line keywords are all strong ( a plain OR ).

Caption pairing : for each ` figure ` det we attach every ` figure_caption `
det using the SAME geometry src/pdf/images.extract() uses when it crops
( x-overlap + vertical gap , at CROP_DPI coords ) , so the caption we
search is the caption that's visually part of the saved PNG. Figure
sequence numbers reuse md._build_image_index , so figure N here is the
same ` {prefix}-figure-{N}.png ` on disk.

Fuzzy matching ( rapidfuzz ) : single-word keywords take the best
token-level ratio ( 'CNN' still hits 'CNNs' ) ; phrases and longer words
also try a best-local-alignment ( partial_ratio ) against the whole
caption ( 'self-attention' , 'neural network' ). --threshold ( default
85 ) is the score cutoff.

Output : output/method-images/report.html ( --out to move it ) -- one
section per matched paper ( PDF / .md / methods .txt / montage / DOI / library
proxy links + a publication-date moniker ) , one card per matched figure
( crop image , highlighted caption ,
matched-keyword tags ) , with clickable per-keyword filter chips up top.
Overwritten on each run.

Papers are emitted MOST-RECENTLY-ADDED first ( paper[ 'created_at' ] , when it
entered our library ) , so a paper you just added lands at the top ; the page's
sort control ( see method_images.html ) can re-sort to publication date or back
to the relevance ranking ( 'Best match' ) using the data-added / data-published /
data-rank attributes each section carries. When served by ` prma server ` the
page also polls /api/method-images/version and refreshes itself once the report
is rebuilt , so newly processed papers appear without a manual reload.

The PAGE -- its style , markup skeleton and in-browser behaviour -- is NOT
in this module : it lives in src/dashboard/method_images.html , alongside
the other dashboard pages , and is edited there. This module generates only
what has to come from the data ( the meta line , the chips , the paper
sections ) and drops those into the template's slots ( see _fill ). Layout
tweaks are an HTML edit ; nothing here needs to change for them.

Under each paper title we show the MODALITIES the study used ( fMRI / EEG /
ECoG / fNIRS / MEG / PET / EMG ) by READING paper[ 'modalities' ] -- the stamp
the ` modalities ` suite stage pins on each record ( see tasks/modalities.py ,
which runs code.paper_methods : title + OpenAlex abstract + isolated Methods
section , never the full OCR , with the same vocabulary ` prma code ` columns
its workbook by ). A pill means the study itself used it and a review /
behavioural paper honestly shows none ; dashed pills were inferred from the
rendered md body because the tagger was blind ( no abstract , no Methods ).

Reading the stamp instead of re-tagging is what keeps a rebuild cheap -- the
one exception to this task's otherwise read-only nature : a paper with no
stamp ( processed before the stage existed ) or a stale one ( the methods.py
vocabulary changed ) is tagged and STAMPED here once , so the first rebuild
migrates old libraries and the following ones are pure reads again.

The filter chips are multi-select , with the usual facet semantics : OR inside
a row ( fMRI + EEG = studies using either ) , AND across rows ( that , narrowed
to figures matching the picked keywords , narrowed to Overviews , ... ). 'All'
clears every filter at once.

Curation lives in the page. Click anywhere on a figure to add it to a selection
( green outline ) and filter to it with the 'Selected' pill ; Export JSON / CSV
for downstream use ( each record carries the doi , title , figure + page number ,
caption , matched keywords , and absolute paths to the PNG / PDF / .md /
methods .txt ). For a fast pass , the LEFT / RIGHT arrow keys move a cursor
between papers and SPACE skips the current one -- skipped papers drop out of the
list and reappear only under the 'Skipped' pill ( to review / restore ). Floating
controls jump you back to the top or to your most recent pick.

Both collections are keyed by ids that are STABLE across re-runs of this command
-- selected figures by '<paper-key>#figure-<N>' , skipped papers by their key --
so a regenerated report re-adopts them and narrowing the keywords never drops a
pick ( ids for figures / papers absent from the current report are kept , not
pruned ).

Persistence : when the report is SERVED by ` prma server ` it reads / writes the
selection through /api/method-images/state ( stored in
output/cache/method-images-state.json ; see src/db/figure_state.py ) , so it
follows you across browsers and survives a rebuild. Opened straight off disk
over file:// -- no server to talk to -- the page falls back to this browser's
localStorage , as it always did. Either way this task never writes the selection
itself ; it only renders the report.

SIBLING REPORT : ` prma all-images ` ( /images , src/tasks/all_images.py ) is
this same page over EVERY extracted figure , with the keyword filtering removed
-- same template , same curation machinery , its own separate selection. The
pieces both reports share ( the template + its slots , the paper-section /
figure-card HTML , the publication-date lookup , the caption pairing ) live
HERE and are imported there , so the two can't drift.
"""

import html
import json
import os
import re
import threading
from datetime import datetime , timezone
from pathlib import Path
from urllib.parse import quote

from tqdm import tqdm

from ..db import papers
from ..utils import utils
from ..utils import methods as methods_vocab


# Reuse the same manager-filter logic the other per-paper tasks use.
def _resolve_managers( args ):
	name = ( getattr( args , "manager" , None ) or "zotero" ).lower()
	if getattr( args , "mendeley" , False ):
		name = "mendeley"
	elif getattr( args , "zotero" , False ):
		name = "zotero"
	if name == "all":
		return ( papers.SOURCE_ZOTERO , papers.SOURCE_MENDELEY )
	return ( name , )


def _paper_matches_managers( paper , managers ):
	if not managers:
		return True
	srcs = paper.get( "sources" ) or {}
	return any( m in srcs for m in managers )


# ---------------------------------------------------------------------------
# Keywords
# ---------------------------------------------------------------------------

_SECTION_RE = re.compile( r"^\[\s*(strong|weak)\s*\]$" , re.IGNORECASE )


def _dedupe( terms , seen ):
	"""Drop case-insensitive repeats ( across BOTH tiers -- a term listed
	strong then weak stays strong ) , preserving order + original casing."""
	out = []
	for k in terms:
		lk = _norm( k )
		if not lk or lk in seen:
			continue
		seen.add( lk )
		out.append( k )
	return out


def load_keywords( args ):
	"""CLI keywords win ( all strong -- a plain OR ) ; otherwise read the
	--keywords-file ( default config/method-images.txt ) , which may split
	terms into ` [strong] ` and ` [weak] ` sections. Lines before any header
	are strong , so a flat legacy list still works.

	Returns ( strong , weak , source-label )."""
	cli = [ k.strip() for k in ( getattr( args , "method_images_keywords" , None ) or [] ) if k and k.strip() ]
	if cli:
		return _dedupe( cli , set() ) , [] , "command line"

	path = getattr( args , "method_images_file" , None )
	path = Path( path ) if path else args.config.joinpath( "method-images.txt" )
	source = str( path )
	if not path.exists():
		return [] , [] , source

	buckets = { "strong": [] , "weak": [] }
	tier = "strong"
	for line in path.read_text( encoding="utf-8" ).splitlines():
		s = line.strip()
		if not s or s.startswith( "#" ):
			continue
		m = _SECTION_RE.match( s )
		if m:
			tier = m.group( 1 ).lower()
			continue
		buckets[ tier ].append( s )
	seen = set()
	strong = _dedupe( buckets[ "strong" ] , seen )
	weak   = _dedupe( buckets[ "weak" ]   , seen )
	return strong , weak , source


# ---------------------------------------------------------------------------
# Fuzzy caption matching
# ---------------------------------------------------------------------------

_WORD_RE = re.compile( r"[a-z0-9]+" )

# A keyword only matches a caption word that AGREES ON ITS FIRST CHARS. Without
# this , rapidfuzz's LCS-based ratio scores ` decoder ` vs ` encoder ` at 85.7
# ( they share 'ecoder' ) -- so every "encoder" caption sprouted a bogus
# 'decoder' hit , and two opposite concepts read as two independent votes. The
# fuzz is here to absorb inflections + OCR garble ( layer/layers , CNN/CNNs ,
# transformer/transformers ) , all of which agree on their opening chars.
_PREFIX_CHARS = 2


def _norm( s ):
	return re.sub( r"\s+" , " " , ( s or "" ).lower() ).strip()


def _prefix_ok( k , tok ):
	n = min( _PREFIX_CHARS , len( k ) )
	return tok[ :n ] == k[ :n ]


_MULTIWORD_RE = re.compile( r"[^a-z0-9]" )

# "Figure 3 |" / "Fig. 2." / "Supplementary Figure 4:" -- stripped before we
# measure WHERE in a caption a term appeared , so the label doesn't count.
_FIG_LABEL_RE = re.compile(
	r"^\s*(?:supplementary\s+|extended\s+data\s+)?"
	r"(?:e?fig(?:ure)?s?\.?\s*[a-z]?\d+\s*[.:|)]?\s*)" , re.IGNORECASE ,
)

# A structural term this far into the caption body still describes the figure
# ( "Schematic of the proposed GCN workflow..." ) . Later than this it's
# usually an aside in a results caption ( "...the same architecture but only
# pre-trained" ) -- measured on this library , a late strong term is ~54%
# results-plot language vs ~28% for an early one. We RANK on this rather than
# filter , so a genuine design figure that buries the word is still listed.
_LEAD_CHARS = 90


def _leads_with_structure( caption , hits ):
	"""True when a strong ( structural ) term appears in the caption's opening
	-- i.e. the figure IS the architecture / pipeline , rather than merely
	mentioning one on its way to reporting a score."""
	spans = [ h[ "span" ][ 0 ] for h in hits if h[ "tier" ] == "strong" and h.get( "span" ) ]
	if not spans:
		return False
	cap = _norm( caption )
	label_len = len( cap ) - len( _FIG_LABEL_RE.sub( "" , cap ) )
	return ( min( spans ) - label_len ) <= _LEAD_CHARS


def _score_terms( cap , tokens , terms , threshold , tier ):
	"""Fuzzy-match `terms` against one already-normalized caption. Returns a
	list of { kw , score , tok , span , tier } for terms scoring >= threshold.
	`span` is the ( start , end ) region of `cap` the keyword matched , which
	match_keywords uses to tell independent evidence from the same word
	counted twice ; `tok` is that region's text , for highlighting.

	Plain single words match against caption TOKENS ( so a keyword can't hide
	inside a longer word : ` decoder ` must not match 'autoencoder' ) . Keywords
	with a space or hyphen ( 'self-attention' , 'neural network' , 'end-to-end' )
	can't be a token , so they use a local alignment over the whole caption ,
	constrained to start on a word boundary with the same opening chars."""
	from rapidfuzz import fuzz

	hits = []
	for kw in terms:
		k = _norm( kw )
		if not k:
			continue
		best , best_span = 0.0 , None
		if not _MULTIWORD_RE.search( k ):
			for tok , s , e in tokens:
				if not _prefix_ok( k , tok ):
					continue
				r = fuzz.ratio( k , tok )
				if r > best:
					best , best_span = r , ( s , e )
		else:
			al = fuzz.partial_ratio_alignment( k , cap , score_cutoff=threshold )
			if al:
				ds = al.dest_start
				at_boundary = ds == 0 or not cap[ ds - 1 ].isalnum()
				if at_boundary and _prefix_ok( k , cap[ ds : ds + _PREFIX_CHARS ] ):
					best , best_span = al.score , ( ds , al.dest_end )
		if best >= threshold and best_span:
			hits.append( {
				"kw":    kw ,
				"score": best ,
				"tok":   cap[ best_span[ 0 ] : best_span[ 1 ] ] ,
				"span":  best_span ,
				"tier":  tier ,
			} )
	return hits


def _stem( s ):
	"""Crude plural strip , enough to see 'blocks' and 'block' as one word."""
	out = []
	for w in ( s or "" ).split():
		if len( w ) > 3 and w.endswith( "s" ) and not w.endswith( "ss" ):
			w = w[ :-1 ]
		out.append( w )
	return " ".join( out )


def _overlaps( a , b ):
	return a[ 0 ] < b[ 1 ] and b[ 0 ] < a[ 1 ]


def _distinct_evidence( hits ):
	"""How many INDEPENDENT things in the caption did these hits find?

	Two hits are the SAME evidence when they either land on overlapping text
	( 'attention' + 'self-attention' + 'attention mechanism' all firing on
	"self attention mechanism" ) or name the same word ( 'block' + 'blocks' ,
	even where the caption says each in a different sentence ). Without both
	rules a single concept votes two or three times and trivially clears
	min_weak -- which is how "Results for vowel production task" got in on
	[ block , blocks ] ."""
	units = []
	for h in hits:
		if not h.get( "span" ):
			continue
		stem , span = _stem( h[ "tok" ] ) , h[ "span" ]
		home = None
		for u in units:
			if u.get( "dead" ):
				continue
			if stem in u[ "stems" ] or any( _overlaps( span , s ) for s in u[ "spans" ] ):
				if home is None:
					u[ "spans" ].append( span )
					u[ "stems" ].add( stem )
					home = u
				else:
					# This hit bridges two existing units -> fold them together.
					home[ "spans" ].extend( u[ "spans" ] )
					home[ "stems" ] |= u[ "stems" ]
					u[ "dead" ] = True
		if home is None:
			units.append( { "spans": [ span ] , "stems": { stem } } )
	return sum( 1 for u in units if not u.get( "dead" ) )


def match_keywords( caption , strong , weak , threshold , min_weak=2 ):
	"""Decide whether one caption describes a model DESIGN , and why.

	A caption qualifies when it carries EITHER any `strong` ( structural )
	term -- "architecture" / "block diagram" alone settles it -- OR at least
	`min_weak` pieces of DISTINCT `weak` evidence , since component /
	model-family words name a model as readily as they describe one
	( "accuracy of our transformer" is a ROC curve , but "encoder" + "block" +
	"LSTM" together is a design ). Distinct means landing on different words :
	'layer' + 'layers' hitting the same "layers" is one vote , not two.

	Returns every hit ( strong AND weak , so the report can tag / highlight
	all of them ) when it qualifies , else [] ."""
	cap = _norm( caption )
	if not cap:
		return []
	tokens = [ ( m.group( 0 ) , m.start() , m.end() ) for m in _WORD_RE.finditer( cap ) ]
	s_hits = _score_terms( cap , tokens , strong or [] , threshold , "strong" )
	w_hits = _score_terms( cap , tokens , weak   or [] , threshold , "weak"   )
	if not s_hits and _distinct_evidence( w_hits ) < min_weak:
		return []
	# Strong first so the report's tags lead with the reason it matched.
	return s_hits + w_hits


# ---------------------------------------------------------------------------
# Figure <-> caption pairing ( same geometry prma images crops with )
# ---------------------------------------------------------------------------

def _page_figures_with_captions( page_dets , scale , images_mod , PP , md_mod ):
	"""For one yolo page , return [ ( det_idx , caption-text ) ] for every
	` figure ` det. Captions attach with the exact rule images.extract()
	uses for its crop unions ( x-overlap + vertical gap at CROP_DPI
	coords ) , so the text we search is the text inside the saved PNG.
	When NOTHING attaches and the page has exactly one figure , fall back
	to every caption on the page ( OCR boxes occasionally sit farther from
	the anchor than the crop heuristic allows ; with one figure there's no
	ambiguity )."""
	figs , caps = [] , []
	for det_idx , det in enumerate( page_dets or [] ):
		if not isinstance( det , dict ):
			continue
		t = det.get( "type" )
		if t not in ( "figure" , "figure_caption" ):
			continue
		bbox = det.get( "bbox" )
		if not bbox:
			continue
		# Huge w/h : we only need the relative geometry , not page clamping.
		sb = images_mod._scale_bbox( bbox , scale , images_mod.PADDING , 10 ** 9 , 10 ** 9 )
		if t == "figure":
			figs.append( ( det_idx , sb ) )
		else:
			caps.append( ( sb , md_mod._clean_text( PP._text_for_det( det ) ) ) )
	out = []
	for det_idx , fb in figs:
		texts = [ txt for cb , txt in caps if txt and images_mod._is_attached( fb , cb ) ]
		if not texts and len( figs ) == 1:
			texts = [ txt for _ , txt in caps if txt ]
		# De-dupe ( two caption dets sometimes OCR the same line ).
		seen , uniq = set() , []
		for txt in texts:
			if txt in seen:
				continue
			seen.add( txt )
			uniq.append( txt )
		out.append( ( det_idx , " ".join( uniq ).strip() ) )
	return out


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

def _href( target , out_dir ):
	"""Relative , URL-quoted href from the report's directory to `target`
	( DOIs produce filenames with '(' / ':' etc. , which must be quoted )."""
	return quote( os.path.relpath( str( target ) , str( out_dir ) ) , safe="/" )


def _highlight( text , terms ):
	"""HTML-escape `text` , wrapping every case-insensitive occurrence of
	any term in <mark>. Escaping happens per-fragment so the regex never
	sees ( or breaks ) an entity."""
	terms = [ t for t in dict.fromkeys( terms ) if t ]
	if not terms:
		return html.escape( text )
	rx = re.compile(
		"(" + "|".join( re.escape( t ) for t in sorted( terms , key=len , reverse=True ) ) + ")" ,
		re.IGNORECASE ,
	)
	out , last = [] , 0
	for m in rx.finditer( text ):
		out.append( html.escape( text[ last : m.start() ] ) )
		out.append( "<mark>" + html.escape( m.group( 0 ) ) + "</mark>" )
		last = m.end()
	out.append( html.escape( text[ last: ] ) )
	return "".join( out )


# How many keyword chips to show before the "+N more" toggle.
_CHIP_HEAD = 18

# The page itself -- style , markup skeleton and behaviour -- lives in
# src/dashboard/method_images.html , next to the other dashboard pages , and is
# edited there like any other HTML. This module only generates the fragments
# that MUST come from the data ( see _fill ).
#
# The SAME template backs ` prma all-images ` ( /images ) : the two reports differ
# only in which figures they carry and which chips sit above them , which is data ,
# so they share one page rather than forking it. The ` mode ` slot lands on <body>
# as data-mode and is what the page keys its per-report bits off ( heading , footer
# legend , /api/<mode>/state , localStorage namespace , export filenames ).
TEMPLATE_PATH = Path( __file__ ).resolve().parent.parent.joinpath(
	"dashboard" , "method_images.html" )

# data-mode values ( == the figure_state collection == the /api/<mode>/… prefix ).
MODE = "method-images"


def _fill( parts ):
	"""Render the template with `parts` ( slot-name -> HTML fragment ).

	Plain token replacement rather than str.format / f-string ON PURPOSE : the
	template is mostly CSS and JS , which are all braces , and a formatter would
	choke on ( or silently mangle ) every one of them.

	Each slot must appear EXACTLY once. Zero means the template lost it and the
	report would render fine while quietly missing its papers ; more than one
	means a stray copy ( e.g. a slot named in a comment ) would be filled with a
	second few-MB copy of everything. Both are the template's bug , so say so
	here rather than writing the damage to disk."""
	tpl = TEMPLATE_PATH.read_text( encoding="utf-8" )
	for name , value in parts.items():
		token = "<!--{ " + name + " }-->"
		n = tpl.count( token )
		if n != 1:
			raise ValueError(
				f"{TEMPLATE_PATH} has {n} {token} slots -- expected exactly 1"
			)
		tpl = tpl.replace( token , value )
	return tpl


def render_paper_sections( matched , out_dir ):
	"""The <section class="paper"> blocks -- the body of BOTH figure reports
	( /method-images and /images ) , which is why it lives here rather than
	inside _render_report.

	`matched` is the list of per-paper dicts run() assembles ( key / title / doi /
	added / published / rank / mods / pdf / md / methods / montage + a 'figures'
	list ) ; `out_dir` is the report's own directory , which every relative href
	is written against.

	Each figure carries seq / page / caption / png , plus 'hits' -- the matched
	keywords. ` prma all-images ` has no keywords , so it passes an EMPTY hits
	list and the card comes out without tags , without caption highlighting and
	with data-lead="0" ; everything else about the card is identical , so a pick
	made on one page is describable by the other ( same data-* , same export ).

	DEFERRED CARDS. A paper's <div class="grid"> is emitted EMPTY , and its cards
	go into a <script type="text/html" class="figs"> beside it ; the page mounts a
	paper's grid only while it's near the viewport and empties it again once it's
	well past ( see "batched rendering" in method_images.html ). ` prma all-images `
	renders 15k+ figures across 2000+ papers -- built into the document up front
	that's ~150k nodes and 15k <img> elements , which is what made the report crawl.
	Inside a script element the same bytes are TEXT : the parser skips them , and
	only the window you're looking at is ever real.

	Cards must therefore be self-describing ( they are -- every field the export
	reads is a data-* on the card ) and , because it's the page that mounts them ,
	the per-paper markup and the per-paper index rows below have to stay in the
	SAME ORDER : the page pairs them up by position."""
	L    = []
	# Per-figure filter index , keyed by paper : [ seq , "kw|kw" , lead ] . The
	# chips , the counters and the paper cursor all have to see the WHOLE report
	# while only a window of it is mounted , so this is what they read instead of
	# the cards. Tiny beside the markup it stands in for ( ~30 bytes a figure ).
	index = {}
	for p in matched:
		title = html.escape( p[ "title" ] or p[ "key" ] )
		# Paper-level metadata rides on the section as data-* so the in-page
		# export can rebuild a full record for each picked figure without us
		# shipping a second copy of everything as JSON.
		attrs = [ f'data-title="{html.escape( p[ "title" ] or p[ "key" ] , quote=True )}"' ]
		attrs.append( f'data-key="{html.escape( p[ "key" ] , quote=True )}"' )
		attrs.append( f'data-mods="{html.escape( "|".join( p[ "mods" ] ) , quote=True )}"' )
		attrs.append( f'data-mods-inferred="{1 if p[ "mods_inferred" ] else 0}"' )
		# Sort keys for the page's sort control ( see method_images.html ) : when the
		# paper entered our library , when it was published , and its relevance order.
		attrs.append( f'data-added="{html.escape( p.get( "added" ) or "" , quote=True )}"' )
		attrs.append( f'data-published="{html.escape( p.get( "published" ) or "" , quote=True )}"' )
		attrs.append( f'data-rank="{p.get( "rank" , 0 )}"' )
		# How many cards this section holds -- /images sorts on it ( 'Most figures' ) ,
		# since with no keywords there's no relevance to rank by.
		attrs.append( f'data-nfigs="{len( p[ "figures" ] )}"' )
		if p[ "doi" ]:
			attrs.append( f'data-doi="{html.escape( p[ "doi" ] , quote=True )}"' )
		for k in ( "pdf" , "md" , "methods" ):
			if p[ k ]:
				attrs.append( f'data-{k}="{html.escape( str( p[ k ].resolve() ) , quote=True )}"' )
		L.append( f'<section class="paper" {" ".join( attrs )}>' )
		if p[ "doi" ]:
			# Canonical resolver as a no-JS fallback. Routing DOIs through a library
			# proxy is a LINK decision , not data , so it lives in the page ( the PROXY
			# constant in method_images.html , shared with the sibling dashboards ) : the
			# class marks the anchor , the JS rewrites this href from the section's
			# data-doi. Keeping it there -- not in this string -- means changing the proxy
			# is an HTML edit , which report_is_stale watches.
			L.append( f'<h2><a class="doi-link" href="https://doi.org/{quote( p[ "doi" ] )}" target="_blank">{title}</a></h2>' )
		else:
			L.append( f"<h2>{title}</h2>" )
		# What the study itself measured with. Omitted entirely when nothing was
		# detected -- an empty row would read as "unknown" when the honest answer
		# for a review / theory paper is "no modality".
		if p[ "mods" ]:
			# Inferred = read off the md body because no abstract / Methods section
			# existed. Weaker evidence , so it looks weaker and says why on hover.
			cls  = "mod inferred" if p[ "mods_inferred" ] else "mod"
			hint = (
				" title=\"Inferred from the paper's full text -- no abstract or "
				"Methods section was available , so this may be a modality the "
				"paper discusses rather than one it used\""
			) if p[ "mods_inferred" ] else ""
			pills = "".join(
				f'<span class="{cls}" data-mod="{html.escape( m , quote=True )}"{hint}>{html.escape( m )}</span>'
				for m in p[ "mods" ]
			)
			L.append( f'<p class="mods">{pills}</p>' )
		links = []
		if p[ "pdf" ]:
			# file:// works when this report is opened straight off disk , but a
			# browser refuses to follow it from an http page -- so when we're
			# served by ` prma server ` the JS below swaps it for /pdf?key=... .
			links.append(
				f'<a class="pdf-link" href="{html.escape( p[ "pdf" ].as_uri() , quote=True )}" '
				f'target="_blank">PDF</a>'
			)
		if p[ "md" ]:
			links.append( f'<a href="{_href( p[ "md" ] , out_dir )}" target="_blank">Markdown</a>' )
		if p[ "methods" ]:
			links.append( f'<a href="{_href( p[ "methods" ] , out_dir )}" target="_blank">Methods</a>' )
		if p[ "montage" ]:
			links.append( f'<a href="{_href( p[ "montage" ] , out_dir )}" target="_blank">All figures ( montage )</a>' )
		if p[ "doi" ]:
			# Two ways to the paper , after the montage : a DIRECT resolver link and a
			# PROXY link routed through your library's EZproxy. Both ship a plain
			# https://doi.org/ href ; only the proxy one carries class="doi-link" , so
			# the page's PROXY rewrite ( the shared PROXY const in method_images.html )
			# upgrades that one in place and leaves the direct link alone.
			doi_q = quote( p[ "doi" ] )
			links.append( f'<a href="https://doi.org/{doi_q}" target="_blank">DOI</a>' )
			links.append( f'<a class="doi-link" href="https://doi.org/{doi_q}" target="_blank">Proxy</a>' )
		# Publication date moniker , right after the DOI / proxy links. Omitted when
		# unknown so it never shows a fake date.
		if p.get( "published" ):
			links.append( f'<span class="pubdate">{html.escape( p[ "published" ] )}</span>' )
		L.append( f'<p class="links">{" &nbsp;·&nbsp; ".join( links )}</p>' )
		# Empty on purpose -- the page fills it from the deferred markup below and
		# empties it again on the way past. data-n is how many cards it will hold ,
		# which is what sizes the placeholder so the scrollbar stays honest.
		L.append( f'<div class="grid" data-n="{len( p[ "figures" ] )}"></div>' )
		L.append( '<script type="text/html" class="figs">' )
		rows = []
		for fig in p[ "figures" ]:
			img   = _href( fig[ "png" ] , out_dir )
			hits  = fig.get( "hits" ) or []
			kws   = "|".join( html.escape( h[ "kw" ] , quote=True ) for h in hits )
			terms = [ h[ "kw" ] for h in hits ] + [ h[ "tok" ] for h in hits if h[ "tok" ] ]
			tags  = "".join(
				f'<span class="tag {h[ "tier" ]}">{html.escape( h[ "kw" ] )}'
				f'<span class="n">{round( h[ "score" ] )}</span></span>'
				for h in hits
			)
			# Stable across re-runs ( doi + figure number , not a row index ) , so
			# a re-generated report restores the same picks from localStorage --
			# and so the two reports name the same figure the same way , which is
			# what lets /images mark what you already picked on /method-images.
			fid = html.escape( f'{p[ "key" ]}#figure-{fig[ "seq" ]}' , quote=True )
			# The index row for this card. Raw text , not escaped : it goes out as
			# JSON , and the keywords have to compare equal to the chips' data-kw
			# once the parser has un-escaped those. The id isn't stored -- the page
			# rebuilds it from the section's data-key and this seq , which is
			# exactly how the two reports agree on a figure's name.
			rows.append( [ fig[ "seq" ] , "|".join( h[ "kw" ] for h in hits ) ,
				1 if fig.get( "lead" ) else 0 ] )
			# The whole card is the control : click anywhere ( image , caption ,
			# tags ) to add it to the selection.
			L.append(
				f'<figure class="fig-card" data-id="{fid}" data-kws="{kws}" '
				f'data-lead="{1 if fig.get( "lead" ) else 0}" data-seq="{fig[ "seq" ]}" '
				f'data-page="{fig[ "page" ] + 1}" '
				f'data-abs="{html.escape( str( fig[ "png" ].resolve() ) , quote=True )}" '
				f'role="button" tabindex="0" aria-pressed="false" '
				f'title="Click to add this figure to your selection ( kept across refreshes )">'
			)
			L.append( '<div class="thumb">' )
			L.append( f'<img loading="lazy" src="{img}" alt="Figure {fig[ "seq" ]}">' )
			L.append( "</div>" )
			L.append( "<figcaption>" )
			# No keywords ( ` prma all-images ` ) -> no tag row at all , rather than an
			# empty one holding open 6px of gap on every card in the report.
			if tags:
				L.append( f'<div class="tags">{tags}</div>' )
			L.append( f'<p class="cap">{_highlight( fig[ "caption" ] , terms )}</p>' )
			L.append( f'<p class="fig-meta">Figure {fig[ "seq" ]} &nbsp;·&nbsp; page {fig[ "page" ] + 1}</p>' )
			L.append( "</figcaption>" )
			L.append( "</figure>" )
		L.append( "</script>" )
		L.append( "</section>" )
		index[ p[ "key" ] ] = rows
	# One blob for the whole report , read once at load. '</' is broken up because
	# the JSON sits inside a <script> : any literal '</script' in a caption-derived
	# string would end the element early ( escaping the slash is legal JSON and
	# parses back identically ).
	L.append( '<script type="application/json" id="figindex">'
		+ json.dumps( index , separators=( "," , ":" ) ).replace( "</" , "<\\/" )
		+ "</script>" )
	return L


def _render_report( out_path , strong , weak , matched , stats ):
	"""Build the full HTML document string : the data-driven fragments here ,
	everything else from the template ( see TEMPLATE_PATH / _fill ). `matched`
	is the list of per-paper dicts assembled in run() ( each with a 'figures'
	list ) ; `stats` is the counters dict used for the summary line."""
	out_dir = out_path.parent
	now     = datetime.now( timezone.utc ).replace( microsecond=0 ).isoformat()

	# Per-keyword card counts for the filter chips.
	tier_of   = { k: "strong" for k in strong }
	tier_of.update( { k: "weak" for k in weak } )
	kw_counts = { k: 0 for k in list( strong ) + list( weak ) }
	for p in matched:
		for fig in p[ "figures" ]:
			for h in fig[ "hits" ]:
				kw_counts[ h[ "kw" ] ] = kw_counts.get( h[ "kw" ] , 0 ) + 1

	# Busiest chips first , and drop keywords nothing matched -- an 80-term
	# list would otherwise bury the useful filters behind dead ones.
	chips = sorted(
		( ( n , kw ) for kw , n in kw_counts.items() if n ) ,
		key = lambda t: ( -t[ 0 ] , t[ 1 ].lower() ) ,
	)
	n_figs = sum( len( p[ "figures" ] ) for p in matched )

	rule = (
		f'{len( strong )} structural terms match alone ; '
		f'{len( weak )} component terms need {stats[ "min_weak" ]}+ per caption'
	) if weak else f'{len( strong )} terms , any one matches'
	meta = (
		f'<p class="meta">Generated {html.escape( now )} &nbsp;·&nbsp; '
		f'{n_figs} matching figures across {len( matched )} papers '
		f'( scanned {stats[ "figures_scanned" ]} figures in {stats[ "papers_scanned" ]} papers ; '
		f'threshold {stats[ "threshold" ]} ; {rule} ; '
		f'keywords from {html.escape( stats[ "source" ] )} )</p>'
	)

	# An 80-term list makes 80 chips , which buries the report below the fold.
	# Show the busiest _CHIP_HEAD and tuck the rest behind a toggle.
	n_lead = sum( 1 for p in matched for f in p[ "figures" ] if f[ "lead" ] )
	C = []
	C.append( f'<button class="chip active" id="chip-all">All<span class="n">{n_figs}</span></button>' )
	# The captions that OPEN with a structural term -- the architecture /
	# pipeline overviews , as opposed to a results plot that name-drops one.
	C.append(
		f'<button class="chip lead-toggle" id="chip-lead" '
		f'title="Captions that open with a structural term -- the model overview diagrams">'
		f'Overviews only<span class="n">{n_lead}</span></button>'
	)
	C.append(
		'<button class="chip sel-toggle" id="chip-selected" '
		'title="Show only the figures you picked">'
		'Selected<span class="n" id="sel-count">0</span></button>'
	)
	# Skip is per-PAPER triage : skipped papers drop out of the default view ; this
	# pill flips that to show ONLY them ( to review / restore ). Count starts at 0
	# and the page fills it from the stored skip list ( like Selected ).
	C.append(
		'<button class="chip skip-toggle" id="chip-skipped" '
		'title="Show only the papers you skipped ( otherwise hidden from the list )">'
		'Skipped<span class="n" id="skip-count">0</span></button>'
	)
	# Modality filters , straight after Selected. A fixed , small set from
	# < --config >/methods.py , so they're never collapsed behind '+N more'.
	# Counts are FIGURES ( like every other chip ) , not papers.
	for m in stats[ "mod_labels" ]:
		n = stats[ "mod_counts" ].get( m , 0 )
		if not n:
			continue
		em = html.escape( m , quote=True )
		C.append(
			f'<button class="chip mod-toggle" data-modf="{em}" '
			f'title="Only figures from studies using {em}">'
			f'{html.escape( m )}<span class="n">{n}</span></button>'
		)
	for i , ( n , kw ) in enumerate( chips ):
		ek   = html.escape( kw , quote=True )
		rest = " rest" if i >= _CHIP_HEAD else ""
		C.append(
			f'<button class="chip{rest}" data-kw="{ek}" data-tier="{tier_of.get( kw , "strong" )}">'
			f'{html.escape( kw )}<span class="n">{n}</span></button>'
		)
	if len( chips ) > _CHIP_HEAD:
		C.append( f'<button class="chip more" id="chip-more">+{len( chips ) - _CHIP_HEAD} more</button>' )

	L = render_paper_sections( matched , out_dir )

	if not matched:
		L.append( "<p>No figure captions matched. Lower <code>--threshold</code> , lower <code>--min-weak</code> , broaden the keywords , or check that the pipeline ( yolo / ocr / images ) has run.</p>" )

	return _fill( {
		"mode":     MODE ,
		"meta":     meta ,
		"chips":    "\n".join( C ) ,
		"papers":   "\n".join( L ) ,
		"min_weak": str( stats[ "min_weak" ] ) ,
	} )


# ---------------------------------------------------------------------------
# Per-paper sort keys ( added-to-manager + publication date )
# ---------------------------------------------------------------------------
# The page sorts papers client-side ( Recently added / Publication date / Best
# match -- see method_images.html ) , so each section carries these as data-* :
#   added     : paper[ 'created_at' ] , when the paper first entered our unified
#               DB ( i.e. when we added it to the manager and snapshot picked it
#               up ) -- the DEFAULT order , newest first.
#   published : the paper's publication date , OpenAlex first ( clean YYYY-MM-DD )
#               then the manager's own date string.
# Both are plain strings the page compares lexicographically ; '' sorts last.

_YEAR_RE = re.compile( r"(?:19|20)\d{2}" )


def _oa_meta( args , doi ):
	"""The cached OpenAlex work meta for a DOI ( output/cache/openalex/… ) , or
	{} on any miss. Same file the OpenAlex / library-search pipelines read."""
	if not doi:
		return {}
	try:
		b64 = utils.base64_encode( doi )
		if not b64:
			return {}
		fp = args.output.joinpath( "cache" , "openalex" , f"{b64}.json" )
		return utils.read_json( fp ) or {} if fp.exists() else {}
	except Exception:
		return {}


def _publication_date( args , paper ):
	"""Best-available publication date as a sortable string ( 'YYYY-MM-DD' ,
	'YYYY-01-01' , or '' when unknown ). OpenAlex is preferred ( it gives a clean
	full date for most library papers ) ; otherwise fall back to the manager's own
	date field ( Zotero '2020-12-00 2020-12' , Mendeley an int year ) , from which
	we keep a real YYYY-MM-DD if present and else synthesise YYYY-01-01."""
	oa = _oa_meta( args , paper.get( "doi" ) )
	pd = oa.get( "publication_date" )
	if pd:
		return str( pd )
	py = oa.get( "publication_year" )
	if py:
		return f"{py}-01-01"
	for src in ( "zotero" , "mendeley" ):
		d = ( ( paper.get( "sources" ) or {} ).get( src ) or {} ).get( "date" )
		if d is None:
			continue
		s = str( d )
		dm = re.match( r"\s*(\d{4}-\d{2}-\d{2})" , s )
		if dm:
			return dm.group( 1 )
		m = _YEAR_RE.search( s )
		if m:
			return f"{m.group( 0 )}-01-01"
	return ""


# ---------------------------------------------------------------------------
# Task entry
# ---------------------------------------------------------------------------

def report_path( args ):
	"""Where the report lands by default. Shared with ` prma server ` , which
	serves this exact file at /method-images ( and rebuilds it after the watch
	worker consumes new papers ) -- one artifact , not a second renderer."""
	return args.output.joinpath( "method-images" , "report.html" )


def report_is_stale( args ):
	"""True when the report needs rebuilding because one of its DEFINITION inputs
	changed : it's missing , or the page template ( src/dashboard/method_images.html ) ,
	the keyword list , or THIS module ( the renderer ) is newer than the built report.
	( A change in the DATA -- newly processed papers -- is handled separately , by the
	--watch worker's post-process refresh_reports. ) This is what lets ` prma server `
	notice you edited the page OR this module and rebuild on the next start , rather
	than serving a stale artifact forever on an already-processed library.

	The module ( __file__ ) is watched because the report is written FROM it : an edit
	to the fragments _render_report builds -- the DOI links , the chips , the paper
	sections -- must invalidate the artifact just as a template edit does , or a code
	change silently doesn't show until new papers happen to land.

	Best-effort : any stat error resolves to 'not stale' , so a filesystem hiccup
	never forces a rebuild loop."""
	try:
		rep = report_path( args )
		if not rep.exists():
			return True
		rep_m = rep.stat().st_mtime
		kf = getattr( args , "method_images_file" , None )
		kf = Path( kf ) if kf else args.config.joinpath( "method-images.txt" )
		for src in ( TEMPLATE_PATH , kf , Path( __file__ ) ):
			try:
				if src and src.exists() and src.stat().st_mtime > rep_m:
					return True
			except Exception:
				pass
		return False
	except Exception:
		return False


# Serializes in-process rebuilds so a startup staleness rebuild and the --watch
# worker's post-batch refresh can't write report.html on top of each other.
_REBUILD_LOCK = threading.Lock()


def rebuild( args ):
	"""Regenerate the report because an input changed -- new papers ( process.run_suite ,
	and the server's --watch worker once per BATCH ) or an edited page / keyword
	list ( the server's startup staleness check ). It's a whole-library pass , so
	callers batch it rather than run it per-paper. Never raises : a failed report
	must not take a processing run or the server down with it."""
	prev , args.only_keys = getattr( args , "only_keys" , None ) , None
	try:
		with _REBUILD_LOCK:
			run( args )
	except Exception as e:
		print( f"METHOD-IMAGES :: report rebuild failed ( {e} ) ; leaving the last one in place" )
	finally:
		args.only_keys = prev


def run( args ):
	from ..pdf import images     as images_mod
	from ..pdf import md         as md_mod
	from ..pdf import pdf        as pdf_mod
	from ..pdf import preprocess as PP
	from . import modalities     as modalities_task

	strong , weak , source = load_keywords( args )
	if not strong and not weak:
		print(
			f"METHOD-IMAGES :: no keywords. Pass them on the command line "
			f"( ` prma method-images transformer attention CNN ` ) or list them "
			f"one-per-line in {source}"
		)
		return

	threshold     = getattr( args , "method_images_threshold" , 85 )
	min_weak      = max( 1 , getattr( args , "method_images_min_weak" , 3 ) or 1 )
	managers      = _resolve_managers( args )
	manager_label = " + ".join( managers ) if managers else "all"

	all_dir     = args.output.joinpath( "images"  , "ALL" )
	md_dir      = args.output.joinpath( "md"      )
	methods_dir = args.output.joinpath( "methods" )
	images_dir  = args.output.joinpath( "images"  )

	out_path = getattr( args , "method_images_out" , None )
	out_path = Path( out_path ) if out_path else report_path( args )

	# Plan : every paper that made it through the pipeline far enough to
	# have yolo pages ( ocr text rides on those dets ). This task never
	# runs a stage inline -- unprocessed papers are counted and skipped.
	jobs = []
	skip_not_processed , skip_other = 0 , 0
	for key , paper in papers.iter_all( args ):
		if not _paper_matches_managers( paper , managers ):
			skip_other += 1
			continue
		prefix = utils.doi_to_filename( key )
		if not prefix:
			continue
		if not ( ( paper.get( "yolo" ) or {} ).get( "pages" ) ):
			skip_not_processed += 1
			continue
		jobs.append( ( key , prefix ) )

	rule = f"{len( strong )} strong + {len( weak )} weak ( {min_weak}+ weak per caption )" if weak else f"{len( strong )} keywords"
	print(
		f"METHOD-IMAGES :: ({manager_label})  {len( jobs )} processed papers to scan "
		f"( {rule} from {source} ; threshold {threshold} ; "
		f"skipped: not-processed={skip_not_processed} other-manager={skip_other} )"
	)
	if not jobs:
		print(
			"METHOD-IMAGES :: nothing has been processed yet -- this command depends "
			"on the PDF suite. Run ` prma process <paper> ` / ` prma server --watch ` , "
			"or the stages directly ( prma yolo ; prma ocr ; prma images ; "
			"prma methods ; prma code ; prma md ) , then re-run."
		)
		return

	matched = []
	figures_scanned , captions_empty , missing_png = 0 , 0 , 0
	n_with_mods , n_inferred , _mod_warned = 0 , 0 , False
	for key , prefix in tqdm( jobs , desc="Papers" , unit="paper" ):
		paper = papers.load( args , key )
		if paper is None:
			continue
		yolo_data = paper.get( "yolo" ) or {}
		pages     = yolo_data.get( "pages" ) or []
		yolo_dpi  = ( yolo_data.get( "meta" ) or {} ).get( "dpi" ) or pdf_mod.DPI
		scale     = images_mod.CROP_DPI / float( yolo_dpi )
		figure_idx , _table_idx = md_mod._build_image_index( pages )

		figs = []
		for page_idx , page_dets in enumerate( pages ):
			for det_idx , caption in _page_figures_with_captions( page_dets , scale , images_mod , PP , md_mod ):
				figures_scanned += 1
				if not caption:
					captions_empty += 1
					continue
				hits = match_keywords( caption , strong , weak , threshold , min_weak )
				if not hits:
					continue
				seq = figure_idx.get( ( page_idx , det_idx ) )
				if seq is None:
					continue
				png = all_dir.joinpath( f"{prefix}-figure-{seq}.png" )
				if not png.exists():
					# Caption matched but ` prma images ` never cropped this
					# paper -- count it so the summary can say "run prma images".
					missing_png += 1
					continue
				figs.append( {
					"seq":     seq ,
					"page":    page_idx ,
					"caption": caption ,
					"hits":    hits ,
					"png":     png ,
					"lead":    _leads_with_structure( caption , hits ) ,
				} )
		if not figs:
			continue

		pdf_path = paper.get( "pdf_path" )
		pdf_path = Path( pdf_path ) if pdf_path else None
		md_path      = md_dir.joinpath( f"{prefix}.md" )
		methods_path = methods_dir.joinpath( f"{prefix}.txt" )
		montage_path = images_dir.joinpath( f"{prefix}-Figures.png" )
		# Which modalities the STUDY used ( fMRI / EEG / ... ) : READ the stamp
		# the ` modalities ` suite stage pinned on the record , don't re-tag --
		# re-tagging every paper here is what made each rebuild re-read the
		# whole library's OpenAlex cache. A paper with no stamp ( processed
		# before the stage existed ) or a stale one ( methods.py vocabulary
		# edited since ) is tagged + stamped once NOW , so the first rebuild
		# migrates the stragglers it shows and later rebuilds are pure reads.
		inferred = False
		try:
			mods = modalities_task.read( args , paper )
			if mods is None:
				mods = modalities_task.stamp( args , key , paper )
			modalities = mods.get( "used" ) or []
			inferred   = bool( mods.get( "inferred" ) )
		except Exception as e:
			if not _mod_warned:
				print( f"METHOD-IMAGES :: method tagging unavailable ( {e} ) ; continuing without it" )
				_mod_warned = True
			modalities = []
		if modalities:
			n_with_mods += 1
			n_inferred  += 1 if inferred else 0
		matched.append( {
			"key":     key ,
			"title":   ( paper.get( "title" ) or "" ).strip() ,
			"doi":     paper.get( "doi" ) ,
			# When this paper entered our library ( default sort , newest first )
			# and when it was published ( the page's Publication-date sort ).
			"added":     paper.get( "created_at" ) or "" ,
			"published": _publication_date( args , paper ) ,
			"mods":    modalities ,
			"mods_inferred": inferred ,
			"pdf":     pdf_path if ( pdf_path and pdf_path.exists() ) else None ,
			"md":      md_path      if md_path.exists()      else None ,
			"methods": methods_path if ( methods_path.exists() and methods_path.stat().st_size > 0 ) else None ,
			"montage": montage_path if montage_path.exists() else None ,
			"figures": figs ,
		} )

	# Rank so the actual architecture diagrams are what you see first : papers
	# with a figure whose caption LEADS with structure , then papers matching
	# strong terms at all , then by volume. Title breaks ties so the order is
	# stable run to run.
	def _fig_rank( f ):
		return (
			not f[ "lead" ] ,
			-sum( 1 for h in f[ "hits" ] if h[ "tier" ] == "strong" ) ,
			f[ "seq" ] ,
		)
	for p in matched:
		p[ "figures" ].sort( key=_fig_rank )
		p[ "n_lead" ]   = sum( 1 for f in p[ "figures" ] if f[ "lead" ] )
		p[ "n_strong" ] = sum(
			1 for f in p[ "figures" ] if any( h[ "tier" ] == "strong" for h in f[ "hits" ] )
		)
	# Relevance ranking -- the architecture-diagram-first order ( leads with a
	# structural caption , then any strong match , then volume ). We keep it as the
	# page's 'Best match' sort AND as the stable tiebreak for the other sorts , so
	# it's captured as each paper's data-rank BEFORE we reorder for the default view.
	matched.sort( key=lambda p: (
		-p[ "n_lead" ] , -p[ "n_strong" ] , -len( p[ "figures" ] ) ,
		( p[ "title" ] or p[ "key" ] ).lower() ,
	) )
	for i , p in enumerate( matched ):
		p[ "rank" ] = i
	# Default DOM order = most-recently-ADDED first ( what the page opens on ) , so a
	# paper you just added to the manager lands at the very top. The sort is STABLE ,
	# so papers added in the same import keep their relevance order among themselves ;
	# the page can re-sort to publication date or back to Best match client-side.
	matched.sort( key=lambda p: p.get( "added" ) or "" , reverse=True )

	# Figures per modality , for the filter chips ( a paper's figures all inherit
	# its modalities ) , in the config's canonical order.
	mod_counts = {}
	for p in matched:
		for m in p[ "mods" ]:
			mod_counts[ m ] = mod_counts.get( m , 0 ) + len( p[ "figures" ] )

	stats = {
		"papers_scanned":  len( jobs ) ,
		"figures_scanned": figures_scanned ,
		"threshold":       threshold ,
		"min_weak":        min_weak ,
		"source":          source ,
		"mod_labels":      methods_vocab.labels( args ) ,
		"mod_counts":      mod_counts ,
	}
	out_path.parent.mkdir( parents=True , exist_ok=True )
	out_path.write_text( _render_report( out_path , strong , weak , matched , stats ) , encoding="utf-8" )

	n_figs = sum( len( p[ "figures" ] ) for p in matched )
	tail = ""
	if missing_png:
		tail = f" ; {missing_png} matched captions had no cropped PNG -- run ` prma images ` to include them"
	if skip_not_processed:
		tail += f" ; {skip_not_processed} papers not processed yet ( run the PDF suite to include them )"
	print(
		f"METHOD-IMAGES :: {n_figs} matching figures across {len( matched )} papers "
		f"( scanned {figures_scanned} figures ; empty-caption={captions_empty} ; "
		f"modality-tagged {n_with_mods}/{len( matched )} papers , "
		f"{n_inferred} inferred from full text )"
		f" -> {out_path}{tail}"
	)
