"""
Writing a CITATION for a paper you have -- the exact inverse of refparse.py ,
which reads somebody else's bibliography back into papers. Here a paper goes
out the other way : as the in-text citation and the full reference a journal
wants , rendered through a real CSL style file so the house style is a
download rather than a patch.

Two strings come out of one render :

  intext   "( Cunningham et al. , 2021 )"   -- what you drop in a sentence
  full     "Cunningham F , Allen JE ( 2021 ) Ensembl 2022. Nucleic Acids
            Research 50:D988-D995."        -- what goes in the reference list

Styles live in < --config >/citation-styles/*.csl and are named by their FILE
NAME without the extension ( 'the-journal-of-neuroscience' ) , which is also
what zotero.org/styles calls them. config.yaml's citation.style picks the
server's default ; each board can override it ( sortboard options.cite_style ).

Three things about CSL worth knowing before you touch this :

  DEPENDENT styles. Half of zotero.org/styles is stubs -- a < info > block
  whose only content is a link to the style it defers to , with no rules of
  its own. citeproc-py does not follow those links ; it builds a style with no
  layout and then dies inside the render with an AttributeError that names
  nothing. So list_styles reads every file's < info > up front and marks those
  unusable , with the parent to download instead ( see _probe ).

  ONE BIBLIOGRAPHY PER PAPER , never a batch. A CSL citation is defined
  relative to the other things cited beside it : the journal style here sets
  disambiguate-add-year-suffix and collapse="year" , so two 2021 papers
  rendered in the same bibliography come out '2021a' / '2021b' and adjacent
  years collapse. A board row is cited ALONE , so it must be rendered alone.

  It is not cheap -- about 6 ms a paper , the style parse itself being under
  one. Hence load_style's cache , and hence the server memoizing the rendered
  strings rather than re-rendering a board's worth on every page load.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from ..utils import utils


# The style shipped with the project , and the fallback whenever the configured
# one is missing or unusable.
DEFAULT_STYLE = "the-journal-of-neuroscience"

_CSL_NS = "{http://purl.org/net/xbiblio/csl}"

# OpenAlex work type -> CSL-JSON type. Only the ones that change the rendering
# are worth naming : a journal style formats a chapter and a book differently
# from an article , and a preprint has no volume or pages to print. Everything
# else is an article , which is what a library of papers mostly is.
_CSL_TYPE = {
	"article":       "article-journal" ,
	"review":        "article-journal" ,
	"letter":        "article-journal" ,
	"editorial":     "article-journal" ,
	"erratum":       "article-journal" ,
	"retraction":    "article-journal" ,
	"paratext":      "article-journal" ,
	"preprint":      "article" ,
	"book-chapter":  "chapter" ,
	"book":          "book" ,
	"monograph":     "book" ,
	"reference-entry": "entry-encyclopedia" ,
	"dissertation":  "thesis" ,
	"report":        "report" ,
	"dataset":       "dataset" ,
}

# Name particles that belong to the SURNAME , for the fallback path where all we
# have is one display string ( OpenAlex gives 'Jan van der Berg' , not a
# first / last pair ). Zotero's creators are already split , which is why they
# are preferred -- see indexer._cite_block.
_PARTICLES = ( "van" , "von" , "de" , "del" , "della" , "der" , "den" , "da" ,
	"di" , "du" , "la" , "le" , "los" , "dos" , "bin" , "ibn" , "al" , "ter" ,
	"ten" , "op" , "st" )


# ---------------------------------------------------------------------------
# The styles on disk
# ---------------------------------------------------------------------------

def styles_dir( args ):
	"""Where the .csl files live : < --config >/citation-styles . Not created
	on demand -- an empty directory and a missing one mean the same thing here
	( fall back to the bundled default ) , and the user puts files in it."""
	cfg = getattr( args , "config" , None )
	return Path( cfg ).joinpath( "citation-styles" ) if cfg else None


def _text( el ):
	return ( el.text or "" ).strip() if el is not None else ""


def _probe( path ):
	"""One .csl file -> what the picker needs to show it , WITHOUT parsing it as
	a style ( that is citeproc-py's job , and it is the expensive half ).

	`ok` false is the interesting case : a DEPENDENT style , which carries a
	< link rel="independent-parent" > and no rules of its own. Zotero resolves
	those ; citeproc-py does not , so one reaches the render as an obscure
	AttributeError. Caught here instead , with the parent named so there is
	something to go and download."""
	out = { "id": path.stem , "title": path.stem , "format": "" ,
		"ok": False , "why": "" }
	try:
		root = ET.parse( path ).getroot()
	except Exception as e:
		out[ "why" ] = f"not readable as XML ( {e} )"
		return out
	info = root.find( f"{_CSL_NS}info" )
	if info is not None:
		out[ "title" ] = _text( info.find( f"{_CSL_NS}title" ) ) or path.stem
		cat = info.findall( f"{_CSL_NS}category" )
		for c in cat:
			if c.get( "citation-format" ):
				out[ "format" ] = c.get( "citation-format" )
		parent = ""
		for ln in info.findall( f"{_CSL_NS}link" ):
			if ln.get( "rel" ) == "independent-parent":
				parent = ( ln.get( "href" ) or "" ).rstrip( "/" ).rsplit( "/" , 1 )[ -1 ]
		if parent:
			out[ "why" ] = ( f"a dependent style -- it has no rules of its own , only a "
				f"pointer to '{parent}'. Download that one from "
				f"https://www.zotero.org/styles/ and pick it instead." )
			return out
	if root.find( f"{_CSL_NS}citation" ) is None:
		out[ "why" ] = "no < citation > rules in the file , so nothing to render with"
		return out
	out[ "ok" ] = True
	return out


# dir mtime -> the listing , so the picker's options cost one stat once a file
# is dropped in rather than a parse of every style on every page load.
_LIST_CACHE = {}

def list_styles( args ):
	"""Every style in the directory , sorted by title : the picker's options.
	Unusable ones are listed too , carrying the reason -- a style you downloaded
	and cannot select is a question , and the answer belongs next to it."""
	d = styles_dir( args )
	if not d or not d.is_dir():
		return []
	try:
		token = d.stat().st_mtime
	except Exception:
		token = 0.0
	hit = _LIST_CACHE.get( str( d ) )
	if hit and hit[ 0 ] == token:
		return hit[ 1 ]
	out = [ _probe( p ) for p in sorted( d.glob( "*.csl" ) ) ]
	out.sort( key=lambda s: s[ "title" ].lower() )
	_LIST_CACHE[ str( d ) ] = ( token , out )
	return out


def default_style( args ):
	"""config.yaml's citation.style , or the bundled default. Falls back to the
	default for a name that isn't on disk , or isn't usable -- a typo in the
	config should cost you the style you wanted , not the whole row."""
	name = ""
	try:
		cfg  = utils.read_yaml( Path( args.config ).joinpath( "config.yaml" ) ) or {}
		name = str( ( cfg.get( "citation" ) or {} ).get( "style" ) or "" ).strip()
	except Exception:
		name = ""
	usable = { s[ "id" ] for s in list_styles( args ) if s[ "ok" ] }
	if name and name in usable:
		return name
	return DEFAULT_STYLE if DEFAULT_STYLE in usable else ( sorted( usable )[ 0 ] if usable else "" )


def resolve_style( args , style_id ):
	"""The style a request actually gets. `style_id` arrives from a browser and
	from the board document , and it NAMES A FILE , so it is never joined onto
	a path : it has to match an id the directory listing already produced , or
	it is simply not a style and the default stands."""
	name = str( style_id or "" ).strip()
	if not name or "/" in name or "\\" in name or ".." in name:
		return default_style( args )
	if any( s[ "id" ] == name and s[ "ok" ] for s in list_styles( args ) ):
		return name
	return default_style( args )


# ( style id , mtime ) -> the parsed style. Parsing is only ~1 ms , but a board
# renders hundreds of citations against the same style and every one of them
# would otherwise re-read the file.
_STYLE_CACHE = {}

def load_style( args , style_id ):
	"""The parsed CSL style for an ALREADY-RESOLVED id , or None."""
	d = styles_dir( args )
	if not d or not style_id:
		return None
	path = d.joinpath( f"{style_id}.csl" )
	try:
		mtime = path.stat().st_mtime
	except Exception:
		return None
	hit = _STYLE_CACHE.get( style_id )
	if hit and hit[ 0 ] == mtime:
		return hit[ 1 ]
	try:
		from citeproc import CitationStylesStyle
		# validate=False on purpose : validation wants an RNC schema we don't
		# ship , and a style that parses renders fine without it ( the one
		# failure mode that matters -- a dependent style -- is caught by _probe ).
		style = CitationStylesStyle( str( path ) , validate=False )
	except Exception as e:
		print( f"cite :: could not parse style '{style_id}' ( {e} )" )
		return None
	_STYLE_CACHE[ style_id ] = ( mtime , style )
	return style


# ---------------------------------------------------------------------------
# A paper -> CSL-JSON
# ---------------------------------------------------------------------------

def _split_name( display ):
	"""'Fiona Cunningham' -> ( 'Cunningham' , 'Fiona' ). The fallback path only :
	a display string is all OpenAlex gives , and splitting one is guesswork the
	moment a surname has a particle in it ( 'Jan van der Berg' ) or more than one
	word. Zotero's first / last pair needs none of this , which is why
	indexer._cite_block prefers it."""
	parts = [ p for p in re.split( r"\s+" , str( display or "" ).strip() ) if p ]
	if not parts:
		return ( "" , "" )
	if len( parts ) == 1:
		return ( parts[ 0 ] , "" )
	cut = len( parts ) - 1
	while cut > 1 and parts[ cut - 1 ].lower().strip( "." ) in _PARTICLES:
		cut -= 1
	return ( " ".join( parts[ cut: ] ) , " ".join( parts[ :cut ] ) )


def _names( creators , row ):
	"""The CSL `author` list. Two sources , better first :

	  the index's creators -- Zotero's , already split into family / given , and
	  a pair with an empty given is a display string that still needs splitting
	  ( the OpenAlex fallback ; see indexer._cite_block ) ;

	  failing that , the board row's own authors string -- which only a
	  reference import ever sets , and which is prose rather than names. It goes
	  in as a CSL `literal` so the style prints it verbatim instead of mangling
	  a whole sentence into initials."""
	out = []
	for pair in ( creators or [] ):
		try:
			fam , giv = str( pair[ 0 ] or "" ).strip() , str( pair[ 1 ] or "" ).strip()
		except Exception:
			continue
		if fam and not giv:
			fam , giv = _split_name( fam )
		if fam or giv:
			out.append( { "family": fam , "given": giv } if giv else { "family": fam } )
	if out:
		return out
	raw = str( ( row or {} ).get( "authors" ) or "" ).strip()
	return [ { "literal": raw[ :600 ] } ] if raw else []


def csl_json( key , lib , row=None ):
	"""One paper -> the CSL-JSON reference citeproc renders.

	`lib` is the paper's library entry off the dashboard index , which carries
	the title / doi / year at the top and the rest of what a style needs in its
	`cite` block ( see indexer._cite_block ). `row` is the board row , and it
	fills in for a paper the index has never seen -- a reference import , or one
	added before ` prma reindex ` got to it. A row has a title , usually a year
	and occasionally a journal : enough for a thin reference , not enough for a
	good one , which is the honest result for a paper we don't have."""
	lib  = lib if isinstance( lib , dict ) else {}
	row  = row if isinstance( row , dict ) else {}
	cite = lib.get( "cite" ) if isinstance( lib.get( "cite" ) , dict ) else {}
	first = lambda *vals: next( ( str( v ).strip() for v in vals if str( v or "" ).strip() ) , "" )

	ref = {
		"id":   key ,
		"type": _CSL_TYPE.get( first( cite.get( "type" ) ).lower() , "article-journal" ) ,
	}
	title = first( lib.get( "title" ) , row.get( "title" ) )
	if title:
		ref[ "title" ] = title
	authors = _names( cite.get( "creators" ) , row )
	if authors:
		ref[ "author" ] = authors
	journal = first( cite.get( "journal" ) , row.get( "journal" ) )
	if journal:
		ref[ "container-title" ] = journal
	for f in ( "volume" , "issue" , "page" ):
		v = first( cite.get( f ) )
		if v:
			ref[ f ] = v
	# The year is on the library entry itself , not in its cite block -- it was
	# already there for every other surface that shows one.
	year = lib.get( "year" ) or row.get( "year" )
	try:
		year = int( year ) if year else None
	except Exception:
		year = None
	if year:
		ref[ "issued" ] = { "date-parts": [ [ year ] ] }
	doi = first( lib.get( "doi" ) , row.get( "doi" ) )
	if doi:
		ref[ "DOI" ] = doi
	return ref


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

EMPTY = { "intext": "" , "full": "" }


def _flat( s ):
	"""citeproc's cite() hands back a string on some paths and a LIST of string
	fragments on others ( the et-al and anonymous ones , which is to say most of
	a real library ). Both mean the same thing."""
	if isinstance( s , str ):
		return s
	try:
		return "".join( str( x ) for x in s )
	except Exception:
		return str( s )


def citable( ref ):
	"""Is there enough here to be worth printing? A reference with no author is
	not a citation -- rendered anyway it comes out '( Anon , n.d. )' , which
	reads as a fact about the paper rather than about our metadata. Better to
	show nothing and let the row say 'not in library' , which it already does."""
	return bool( ( ref or {} ).get( "author" ) )


def render( args , style_id , key , lib , row=None ):
	"""{ intext , full } for one paper , in one style. Both strings come out of
	the SAME render , so the full reference costs nothing extra once the in-text
	one has been asked for.

	Never raises : a style that turns out to be broken , or a reference citeproc
	chokes on , costs this one row its citation line and nothing else."""
	ref = csl_json( key , lib , row )
	if not citable( ref ):
		return dict( EMPTY )
	style = load_style( args , style_id )
	if style is None:
		return dict( EMPTY )
	try:
		from citeproc        import CitationStylesBibliography , Citation , CitationItem , formatter
		from citeproc.source.json import CiteProcJSON
		bib = CitationStylesBibliography( style , CiteProcJSON( [ ref ] ) , formatter.plain )
		# ONE citation in ONE bibliography -- see the note at the top of the file
		# about why a board's rows are never rendered together.
		c = Citation( [ CitationItem( ref[ "id" ] ) ] )
		bib.register( c )
		intext = _flat( bib.cite( c , lambda x: None ) ).strip()
		out    = bib.bibliography()
		full   = _flat( out[ 0 ] ).strip() if out else ""
	except Exception as e:
		print( f"cite :: could not render {key} in '{style_id}' ( {e} )" )
		return dict( EMPTY )
	return { "intext": intext , "full": full }
