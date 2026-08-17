"""
Reading somebody else's REFERENCE LIST.

The boards ( /sort , /tiers ) grow one paper at a time out of your own library.
This is the other way in : the bibliography at the end of a review , a .docx a
collaborator sent , a block of numbered references pasted straight off a PDF --
turned back into papers , matched against what you already have ( see
server._refs_payload ) , and dropped on the board's STAGING shelf for you to tag
before any of it touches the sorted list.

Nothing here is exact , and it isn't trying to be. A reference is prose : the
same paper is written six ways by six journals and OCR mangles a seventh. So
every heuristic below falls back to "put the whole line in the title and let
them fix it on the page" rather than to an error -- a row you can edit beats a
row you never got , and the staging shelf exists precisely so a bad parse is
visible before it lands anywhere.

What it reads :

  a .docx     -- a zip of XML. One paragraph per reference is how every Word
                 bibliography is built , so the paragraph breaks ARE the entry
                 breaks ( _docx_text ). ( .doc -- the pre-2007 binary -- is not
                 a zip and is not supported ; save it as .docx or paste it. )
  a .txt      -- or anything else that decodes as text.
  pasted text -- posted as { "text": "..." } .

and , per entry ( parse_entry ) :

  AMA / Vancouver   Comte A, Gabriel D, Pazart L, et al. On the difficulty to
                    communicate with fMRI-based protocols… Neuroscience.
                    2015;300:448-459. doi:10.1016/j.neuroscience.2015.05.059
  APA / Chicago     Smith J. A., & Doe B. (2020). Title of the paper. Journal
                    Name, 12(3), 45-67.
  quoted-title      V. Sequeira et al., "Neural narratives," in Proc. …

The one trick the whole file turns on : find where the AUTHORS stop
( _looks_like_authors ) , because the TITLE is whatever comes next. Everything
else -- journal , year , DOI -- is picked out of the tail after that.
"""

import io
import json
import re
import zipfile
from html import unescape

from ..utils import utils


# ---------------------------------------------------------------------------
# Getting text out of whatever the page posted
# ---------------------------------------------------------------------------

_ZIP_MAGIC = b"PK\x03\x04"

# The two files somebody reaches for by mistake when the panel says ".docx or
# .txt" , both of which would otherwise decode into pages of binary noise and
# reach the shelf as a hundred junk rows. Say what happened instead.
_WRONG_FILE = (
	( b"\xd0\xcf\x11\xe0" , "that's a .doc ( the old Word format ) , not a .docx -- "
	                       "open it in Word and Save As .docx , or paste the references in" ) ,
	( b"%PDF-"            , "that's a PDF -- select the reference list in your reader , "
	                       "copy it , and paste it into the box" ) ,
	( b"{\\rtf"           , "that's an .rtf -- save it as .docx or .txt , "
	                       "or paste the references in" ) ,
)

_XML_PARA = re.compile( r"</w:p\s*>"            , re.I )
_XML_BR   = re.compile( r"<w:(?:br|cr)\b[^>]*>" , re.I )
_XML_TAB  = re.compile( r"<w:tab\b[^>]*>"       , re.I )
_XML_TAG  = re.compile( r"<[^>]+>" )

# The parts of a .docx that can hold a reference list. Footnotes / endnotes are
# in because some people put their bibliography there.
_DOCX_PARTS = ( "word/document.xml" , "word/footnotes.xml" , "word/endnotes.xml" )


def text_from_upload( data ):
	"""The reference list , as text , out of whatever the page posted : a .docx
	( a zip ) , a text file , or a JSON { "text": "<pasted>" } body.

	ONE entry point because the page has one paste-or-drop panel and the three
	are indistinguishable from where it stands -- it posts the bytes and this
	works out which it got."""
	if not data:
		return ""
	for magic , why in _WRONG_FILE:
		if data[ :len( magic ) ] == magic:
			raise ValueError( why )
	if data[ :4 ] == _ZIP_MAGIC:
		return _docx_text( data )
	s = data.decode( "utf-8" , errors="replace" ).lstrip( "﻿" )
	if s.lstrip()[ :1 ] == "{":
		try:
			obj = json.loads( s )
			if isinstance( obj , dict ) and isinstance( obj.get( "text" ) , str ):
				return obj[ "text" ]
		except Exception:
			pass
	return s


def _docx_text( data ):
	"""The visible text of a .docx , one line per paragraph.

	Four regexes over the document XML rather than a python-docx dependency :
	all that's wanted here is the text and the paragraph breaks , and the
	paragraph breaks are the only structure a reference list has."""
	out = []
	try:
		with zipfile.ZipFile( io.BytesIO( data ) ) as z:
			names = set( z.namelist() )
			for part in _DOCX_PARTS:
				if part not in names:
					continue
				xml = z.read( part ).decode( "utf-8" , errors="replace" )
				xml = _XML_PARA.sub( "\n" , xml )
				xml = _XML_BR.sub(   "\n" , xml )
				xml = _XML_TAB.sub(  "\t" , xml )
				out.append( unescape( _XML_TAG.sub( "" , xml ) ) )
	except zipfile.BadZipFile:
		raise ValueError( "that file is damaged , or isn't really a .docx" )
	if not out:
		# A zip , but not a Word document ( a .xlsx , a .zip of PDFs , … ).
		raise ValueError( "that's a zip file , but not a .docx -- no Word document inside it" )
	return "\n".join( out )


# ---------------------------------------------------------------------------
# One string per reference
# ---------------------------------------------------------------------------

# A numbered reference : "33." , "33)" , "[33]" , "(33)" opening a line. Three
# digits at most , so a wrapped line opening "2024;14(17):17." can't be mistaken
# for the start of reference number 2024.
_MARK_RE = re.compile( r"^[\s ]*(?:\[\s*(\d{1,3})\s*\]|\(\s*(\d{1,3})\s*\)|(\d{1,3})\s*[.)])[\s ]+" )

_WS_RE = re.compile( r"[\s ]+" )


def _collapse( s ):
	return _WS_RE.sub( " " , ( s or "" ) ).strip()


def _mark( line ):
	"""( line without its number , the number ) , or ( line , None )."""
	m = _MARK_RE.match( line or "" )
	if not m:
		return line , None
	return line[ m.end(): ] , int( m.group( 1 ) or m.group( 2 ) or m.group( 3 ) )


def split_entries( text ):
	"""One ( string , number ) per reference.

	Three shapes , in the order they're worth trusting :

	  NUMBERED   two or more lines opening "33." / "[33]" , counting up. The
	             markers are the entry boundaries , so a reference wrapped over
	             four lines ( a PDF copy-paste , always ) still comes back whole.
	  BLOCKS     blank lines between entries.
	  A LINE     the Word default -- a paragraph per reference and nothing else
	             to go on.
	"""
	lines = [ l.rstrip() for l in
		( text or "" ).replace( "\r\n" , "\n" ).replace( "\r" , "\n" ).split( "\n" ) ]

	marks = [ ( i , _mark( l )[ 1 ] ) for i , l in enumerate( lines ) if l.strip() ]
	marks = [ ( i , n ) for i , n in marks if n is not None ]
	# "Counting up" is what separates a real numbered list from a stray "1." in
	# the middle of a sentence-wrapped paragraph.
	if len( marks ) >= 2 and all( b[ 1 ] > a[ 1 ] for a , b in zip( marks , marks[ 1: ] ) ):
		out = []
		for j , ( i , n ) in enumerate( marks ):
			end   = marks[ j + 1 ][ 0 ] if j + 1 < len( marks ) else len( lines )
			chunk = _collapse( " ".join( [ _mark( lines[ i ] )[ 0 ] ] + lines[ i + 1 : end ] ) )
			if chunk:
				out.append( ( chunk , n ) )
		return out

	blocks , cur = [] , []
	for l in lines:
		if l.strip():
			cur.append( l )
		elif cur:
			blocks.append( cur )
			cur = []
	if cur:
		blocks.append( cur )
	if len( blocks ) >= 2:
		joined = [ _collapse( _mark( " ".join( b ) )[ 0 ] ) for b in blocks ]
		return [ ( j , None ) for j in joined if j ]

	out = []
	for l in lines:
		s = _collapse( _mark( l )[ 0 ] )
		if s:
			out.append( ( s , None ) )
	return out


# ---------------------------------------------------------------------------
# One reference -> fields
# ---------------------------------------------------------------------------

_DOI_RE = re.compile( r"\b10\.\d{4,9}/[^\s\"<>,;]+" )
_URL_RE = re.compile( r"https?://[^\s\"<>]+" )
_YEAR_RE = re.compile( r"\b(1[5-9]\d{2}|20\d{2})\b" )
# "(2020)." / "(2020a)" -- the APA / Chicago tell , and an unambiguous one : it
# ends the authors and starts the title in one mark.
_APA_YEAR = re.compile( r"\(\s*(1[5-9]\d{2}|20\d{2})[a-z]?\s*\)\s*\.?\s*" )
# A quoted run of real length is the title outright , whatever the sentence
# splitting made of it ( IEEE and its relatives quote titles ).
_QUOTED = re.compile( "[“\"]([^“”\"]{12,300}?)[”\"]" )

# Segments are sentences : split on a period followed by space. NOT on "?" --
# a title that asks a question ( "Can inner speech be decoded? Evidence from…" )
# is one title , and losing half of it is worse than carrying a stray clause.
_SEG_SPLIT = re.compile( r"(?<=\.)\s+" )

# Words that never appear in a surname. What stops a short title like "Inner
# speech decoding with EEG" from reading as "<surname> <initials>".
_STOP_RE = re.compile(
	r"\b(?:the|of|with|and|in|for|on|a|an|to|from|using|via|by|as|at|is|are|was|were|"
	r"its|their|between|during|after|before|toward|towards|into|over|under|within|"
	r"study|review|analysis|evidence|effects?|role)\b" , re.I )

# "<surname , maybe several words> <1-4 initials>" -- how every numbered style
# writes a name.
_NAME_RE     = re.compile( r"^([^\d]{2,60}?)\s+([A-Z]{1,4})\.?$" )
_INITIALS_RE = re.compile( r"^(?:[A-Z]\.?[\s ]*){1,4}$" )
_ETAL_RE     = re.compile( r"^(?:et[\s ]al\.?|and others|eds?\.?|editors?)$" , re.I )
_AUTHOR_SPLIT = re.compile( r"[,;&]|\band\b" , re.I )

# Segments that are never a journal name.
_SKIP_SEG = re.compile(
	r"^(?:accessed|published|updated|cited|retrieved|available|preprint|epub|"
	r"in press|doi|pmid|pmcid|isbn|https?)\b" , re.I )
# ... only these lie about the YEAR , though : "Preprint posted online 2022" is
# the publication year , "Accessed September 5, 2024" is the day you read it.
_SKIP_YEAR = ( "accessed" , "retrieved" , "cited" , "updated" )
_EDS_RE   = re.compile( r"\beds?\.?$|\beditors?\.?$" , re.I )
_NOTJ_RE  = re.compile( r"^[\d\s;:,()\-–.]+$" )
# "In:" / "In " -- the book-chapter and proceedings wrapper , never part of the
# venue's name.
_IN_RE    = re.compile( r"^in[:\s]\s*" , re.I )
# The volume / issue / page run a journal name trails : "300:448-459" ,
# "14(17):17" , "12(3), 45-67" -- and the year , when it trails on its own.
_VOL_RE        = re.compile( r"[,.;]?\s*\d{1,4}\s*(?:\(\s*[^)]{1,24}\s*\))?\s*[:,]\s*[\dA-Za-z\-–]+\s*$" )
_TRAIL_YEAR_RE = re.compile( r"[,;\s]+(?:1[5-9]\d{2}|20\d{2})\s*$" )


def _segments( s ):
	"""A reference's sentences , trimmed of their trailing period."""
	return [ p.strip().strip( "." ).strip()
		for p in _SEG_SPLIT.split( s or "" ) if p.strip( " ." ) ]


def _is_name( part ):
	m = _NAME_RE.match( part )
	return bool( m ) and not _STOP_RE.search( m.group( 1 ) )


def _looks_like_authors( seg ):
	"""Is this segment a list of names?

	The whole parse turns on this : a reference's first sentence is nearly
	always the authors , and once you know where they stop , the TITLE is
	whatever comes next. When it says no , the caller treats the first sentence
	as the title instead -- which is the right answer for the reference lists
	that lead with one."""
	seg = ( seg or "" ).strip().rstrip( "." )
	if not seg or len( seg ) > 400:
		return False
	parts = [ p.strip() for p in _AUTHOR_SPLIT.split( seg ) if p.strip() ]
	if not parts:
		return False
	good = sum( 1 for p in parts
		if _ETAL_RE.match( p ) or _INITIALS_RE.match( p ) or _is_name( p ) )
	# Every part but at most one : a real author list is uniform , and a title
	# that happens to end in an acronym only ever scores its single self.
	return good >= 1 and good >= len( parts ) - 1


def _clean_journal( t ):
	"""Trim the volume / issue / page run and any trailing year , so
	"Neuroscience. 2015;300:448-459" comes back as "Neuroscience"."""
	t = re.split( r"\s*;\s*" , t )[ 0 ]
	t = _VOL_RE.sub( "" , t )
	t = _TRAIL_YEAR_RE.sub( "" , t )
	return re.sub( r"[\s,.;:]+$" , "" , t )[ :300 ]


def _first_journal( segs ):
	"""The first sentence after the title that's a venue rather than a note.
	"In: <editors>, eds" is the wrapper around a book chapter , not the book ,
	so it's skipped and the NEXT one -- the volume title -- is taken. So is a
	stub left behind by the sentence split ( IEEE's "in Proc." ) , which is a
	word , not a venue."""
	for seg in segs:
		t = _IN_RE.sub( "" , seg.strip() ).strip()
		if len( t ) < 5 or _SKIP_SEG.match( t ) or _EDS_RE.search( t ) or _NOTJ_RE.match( t ):
			continue
		out = _clean_journal( t )
		if out:
			return out
	return ""


def _first_year( segs ):
	for seg in segs:
		if seg.strip().lower().startswith( _SKIP_YEAR ):
			continue
		m = _YEAR_RE.search( seg )
		if m:
			return int( m.group( 1 ) )
	return None


def synth_key( s ):
	"""The key a reference gets when it is neither in the library nor carrying a
	DOI. Same idea as papers.py's nodoi- keys : stable , filename-safe , and
	derived from the one thing a reference always has -- what it's called -- so
	pasting the same bibliography twice doesn't stage it twice."""
	stem = re.sub( r"[^a-z0-9]+" , "-" , utils.normalize_title( s or "" ) ).strip( "-" )
	return "ref-" + ( stem[ :80 ] or "unknown" )


def _finish( out ):
	out[ "title" ]   = _collapse( out[ "title" ]   ).strip( " ,.;:" )[ :1000 ]
	out[ "authors" ] = _collapse( out[ "authors" ] ).strip( " ,.;:" )[ :600  ]
	out[ "journal" ] = _collapse( out[ "journal" ] ).strip( " ,.;:" )[ :300  ]
	if not out[ "title" ]:
		# Nothing recognizable in there. Hand the whole line back as the title
		# rather than nothing : it reaches the staging shelf as something to fix
		# instead of vanishing.
		out[ "title" ] = out[ "raw" ][ :1000 ]
	return out


def parse_entry( raw , n=None ):
	"""One reference -> { n , raw , authors , title , year , journal , doi , url }.
	None only when there was nothing there at all."""
	s = _collapse( raw )
	if not s:
		return None
	out = { "n": n , "raw": s[ :2000 ] , "authors": "" , "title": "" ,
	        "year": None , "journal": "" , "doi": "" , "url": "" }

	# DOI and url first : both are unambiguous , and lifting the url out stops
	# the sentence splitting tripping over a path full of dots.
	m = _DOI_RE.search( s )
	if m:
		out[ "doi" ] = utils.normalize_doi( m.group( 0 ) ) or m.group( 0 ).rstrip( ".,;:)]" )
	m = _URL_RE.search( s )
	if m:
		u = m.group( 0 ).rstrip( ".,;:)]" )
		if "doi.org/" not in u:
			out[ "url" ] = u[ :1000 ]
	body = _URL_RE.sub( " " , s )

	m = _APA_YEAR.search( body )
	if m:
		# APA / Chicago : the parenthesized year ends the authors and starts the
		# title , which is the cleanest signal any style gives us.
		out[ "authors" ] = body[ :m.start() ]
		out[ "year" ]    = int( m.group( 1 ) )
		segs = _segments( body[ m.end(): ] )
		out[ "title" ]   = segs[ 0 ] if segs else body[ m.end(): ]
		out[ "journal" ] = _first_journal( segs[ 1: ] )
		return _finish( out )

	segs = _segments( body )
	if not segs:
		return _finish( out )
	if len( segs ) > 1 and _looks_like_authors( segs[ 0 ] ):
		out[ "authors" ] = segs[ 0 ]
		rest = segs[ 1: ]
	else:
		rest = segs
	q = _QUOTED.search( body )
	if q:
		# A quoted title also settles where the authors stopped -- they are
		# everything in front of the quote , whatever the sentence split made of
		# "V. Sequeira, V. Mehta, and N. Sriram".
		out[ "title" ]   = q.group( 1 )
		out[ "authors" ] = body[ :q.start() ]
		tail = _segments( body[ q.end(): ] )
	else:
		out[ "title" ] = rest[ 0 ] if rest else ""
		tail = rest[ 1: ]
	out[ "journal" ] = _first_journal( tail )
	out[ "year" ]    = _first_year( tail ) or _first_year( [ body ] )
	return _finish( out )


# How many references one paste / file is allowed to produce. A bibliography is
# a few hundred entries ; anything past this is a pasted PDF , and staging it
# would be worse than saying no.
MAX_REFS = 600


def parse( text ):
	"""A block of reference text -> a list of parsed references , in order ,
	de-duplicated against itself ( the same paper cited twice in one list is one
	row on the shelf )."""
	out , seen = [] , set()
	for raw , n in split_entries( text )[ :MAX_REFS ]:
		ref = parse_entry( raw , n )
		if not ref:
			continue
		ident = ( ref[ "doi" ] or "" ).lower() or utils.normalize_title( ref[ "title" ] )
		if ident and ident in seen:
			continue
		if ident:
			seen.add( ident )
		out.append( ref )
	return out
