"""
Reading a GOOGLE SHEET , for the /sort board's ⇩ Import.

A sheet somebody keeps alongside the board -- the board's own export , grown
with papers of their own and a few columns the board doesn't have ( Methods
summary , Datasets , Code ) -- is the same CSV the importer already reads , just
living at a link. The page can't fetch it itself ( docs.google.com sends no CORS
headers ) , so the server does , and hands back the rows for the page to import
exactly as if the file had been dropped on it.

THE LINKS BEHIND THE TEXT. The sheet's Datasets / Code cells are rich text : what
shows is a short label ( ` alexhuth/sensimetrics_filter ` , ` zenodo 8349726 ` ,
` UK Biobank ` ) and the full URL is the hyperlink behind it -- one per line , so
a cell can carry a dozen. A CSV export keeps only the label , and so do the XLSX
and ODS exports ( one link per cell at most , the first ). Only the HTML export
keeps every line's own link , and Google offers that as a zip of every tab
( export?format=zip ). So the rows come from the CSV , as they always did , and
the links are read out of the HTML and laid over them ( fetch_links ) -- matched
by what each row SAYS , never by where it sits , so a link can only ever land on
the cell whose text it was drawn behind. If the HTML can't be had , or Google
ever changes it past reading , the import still works : the text comes in
without its links , and the page says so.

Only the sheet's ID ( and tab gid ) is taken from what was pasted : the request
always goes to docs.google.com's own export URL , never to whatever host the
pasted text names , so this is not a way to make the server fetch arbitrary
URLs.

It only works for a sheet shared "Anyone with the link" -- Google answers
everyone else with its sign-in page. That comes back as a ValueError phrased for
a person , with the way round it : File -> Download -> CSV , and drop the file.
"""

import csv
import io
import re
import zipfile
from html.parser import HTMLParser
from urllib.parse import parse_qs , urlparse

import requests


_ID_RE    = re.compile( r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{20,})" )
_GID_RE   = re.compile( r"[#?&]gid=([0-9]+)" )
MAX_BYTES = 20 * 1024 * 1024

_NOT_SHARED = ( "Google wants a sign-in for that sheet -- share it as "
	"“Anyone with the link : Viewer” , or download it "
	"( File → Download → CSV ) and drop the file here instead" )


def _sheet_id( link ):
	m = _ID_RE.search( str( link or "" ) )
	if not m:
		raise ValueError( "that isn't a Google Sheets link -- it should look like "
			"https://docs.google.com/spreadsheets/d/…" )
	return m.group( 1 )


def export_url( link ):
	"""The CSV export URL for a sheet link , built from its ID alone.
	Raises ValueError when the text isn't a Google Sheets link."""
	url = f"https://docs.google.com/spreadsheets/d/{_sheet_id( link )}/export?format=csv"
	g = _GID_RE.search( str( link ) )
	return url + ( f"&gid={g.group( 1 )}" if g else "" )


def zip_url( link ):
	"""Every tab of the sheet as HTML , zipped -- the one export that keeps each
	line's link ( see the module docstring ). Built from the ID alone , too."""
	return f"https://docs.google.com/spreadsheets/d/{_sheet_id( link )}/export?format=zip"


def _get( url , timeout ):
	"""One export's bytes. ValueError , already phrased for the page , on a
	sign-in page , a missing sheet , any other status , or past MAX_BYTES."""
	try:
		r = requests.get( url , timeout=timeout , stream=True ,
			headers={ "User-Agent": "prma" } )
	except requests.RequestException as e:
		raise ValueError( f"couldn't reach Google Sheets ( {e.__class__.__name__} )" )
	with r:
		kind = r.headers.get( "Content-Type" , "" ).lower()
		if r.status_code in ( 401 , 403 ) or "text/html" in kind:
			raise ValueError( _NOT_SHARED )
		if r.status_code == 404:
			raise ValueError( "Google Sheets has no sheet at that link" )
		if r.status_code != 200:
			raise ValueError( f"Google Sheets answered {r.status_code}" )
		data = b""
		for chunk in r.iter_content( 65536 ):
			data += chunk
			if len( data ) > MAX_BYTES:
				raise ValueError( "that sheet is over 20 MB -- download it as CSV instead" )
	return data


def fetch_csv( link , timeout=30 ):
	"""The sheet's first ( or linked ) tab as CSV text."""
	return _get( export_url( link ) , timeout ).decode( "utf-8-sig" , errors="replace" )


def rows( text ):
	"""CSV text -> rows of cells , read the way the page reads a dropped file
	( parseCSV in sort.html ) : line endings folded to \\n , inside a cell too ,
	and rows with nothing in them dropped."""
	text = text.replace( "\r\n" , "\n" ).replace( "\r" , "\n" )
	return [ r for r in csv.reader( io.StringIO( text ) ) if any( c.strip() for c in r ) ]


# ---------------------------------------------------------------------------
# The links , out of the HTML export
# ---------------------------------------------------------------------------

def _href( h ):
	"""An exported link , unwrapped if Google routed it through its redirector.
	Only web links count : a link to another tab ( #gid=… ) means nothing off
	the sheet."""
	h = ( h or "" ).strip()
	u = urlparse( h )
	if u.netloc.endswith( "google.com" ) and u.path == "/url":
		h = ( parse_qs( u.query ).get( "q" ) or [ "" ] )[ 0 ]
	return h if re.match( r"https?://" , h , re.I ) else None


class _Cells( HTMLParser ):
	"""One tab of the HTML export -> its rows , each cell a list of runs
	[ text , url | None ] in reading order , neighbouring runs under the same
	link merged. <br> is a line break. The <th> headers ( the A B C column
	letters , the 1 2 3 row numbers ) are not cells."""

	def __init__( self ):
		super().__init__( convert_charrefs=True )
		self.rows , self.row , self.cell , self.href , self.th = [] , None , None , None , 0

	def handle_starttag( self , tag , attrs ):
		a = dict( attrs )
		if tag == "tr":
			self.row = []
		elif tag == "th":
			self.th += 1
		elif tag == "td" and self.row is not None:
			self.cell , self.span = [] , a.get( "colspan" ) or "1"
		elif tag == "a":
			self.href = _href( a.get( "href" ) )
		elif tag == "br":
			self._text( "\n" )

	def handle_endtag( self , tag ):
		if tag == "th":
			self.th = max( 0 , self.th - 1 )
		elif tag == "a":
			self.href = None
		elif tag == "td" and self.cell is not None and self.row is not None:
			self.row.append( self.cell )
			# A merged cell still owns every column it covers , or everything to
			# its right would slide one column left.
			self.row.extend( [] for _ in range( int( self.span ) - 1 if self.span.isdigit() else 0 ) )
			self.cell = None
		elif tag == "tr" and self.row is not None:
			self.rows.append( self.row )
			self.row = None

	def handle_data( self , data ):
		self._text( data )

	def _text( self , t ):
		if self.cell is None or self.th:
			return
		if self.cell and self.cell[ -1 ][ 1 ] == self.href:
			self.cell[ -1 ][ 0 ] += t
		else:
			self.cell.append( [ t , self.href ] )


def _norm( s ):
	return str( s or "" ).replace( "\r\n" , "\n" ).replace( "\r" , "\n" ).replace( "\xa0" , " " ).strip()


def _sig( cells ):
	"""A row as the texts of its cells , trailing empty ones dropped : what a row
	is matched on across the two exports."""
	out = [ _norm( c ) for c in cells ]
	while out and not out[ -1 ]:
		out.pop()
	return tuple( out )


def links_from_zip( data , rows ):
	"""The links behind `rows` ( the CSV , through rows() ) , read out of the
	zipped HTML export `data`. A list as long as `rows` : None for a row with no
	link in it , otherwise { column index : runs } for each cell that has one ,
	runs being the whole cell as [ text , url | None ] pieces.

	The zip holds every tab and names them by title , not by the gid the CSV was
	asked for , so the tab is the one whose rows match the most of `rows` -- and
	a row only takes links from a row that says exactly what it says , cell for
	cell. Raises ValueError when nothing in the zip matches at all."""
	try:
		zf = zipfile.ZipFile( io.BytesIO( data ) )
	except zipfile.BadZipFile:
		raise ValueError( "Google sent something that isn't the zipped sheet" )
	want = { _sig( r ) for r in rows }
	best , hits = {} , 0
	for info in zf.infolist():
		if not info.filename.lower().endswith( ".html" ) or info.file_size > 4 * MAX_BYTES:
			continue
		p = _Cells()
		p.feed( zf.read( info ).decode( "utf-8" , errors="replace" ) )
		tab = {}
		for cells in p.rows:
			tab.setdefault( _sig( "".join( t for t , _ in runs ) for runs in cells ) , cells )
		n = len( want & set( tab ) )
		if n > hits:
			best , hits = tab , n
	if not hits:
		raise ValueError( "none of its rows could be found in Google's HTML copy of the sheet" )
	out = []
	for r in rows:
		cells = { j: runs for j , runs in enumerate( best.get( _sig( r ) ) or [] )
			if any( u for _ , u in runs ) }
		out.append( cells or None )
	return out


def fetch_links( link , rows , timeout=30 ):
	"""links_from_zip , off the sheet's own zipped HTML export."""
	return links_from_zip( _get( zip_url( link ) , timeout ) , rows )
