"""
The literature-gap sweep's hands : what a sweep ( a person , or the daily
scheduled agent ) uses to check a candidate against everything we already have
and to write the keepers into ONE Google Sheet , Inner Speech - Literature Gap
Sweep , which has four tabs :

  Sweep          papers , laid out for /sort's ⇩ Import CSV / Sheet
  Rejected       papers looked at and turned down , with the reason -- kept so
                 they are never suggested again , and off the Sweep tab so an
                 import never brings them in
  Datasets       the Inner Speech - Datasets sheet's Datasets columns
  Code Examples  the Inner Speech - Datasets sheet's Code Examples columns

  venv/bin/python tools/gap-sweep.py init
  venv/bin/python tools/gap-sweep.py check "10.48550/arXiv.2505.18185" "Some Title" ...
  venv/bin/python tools/gap-sweep.py add rows.json
  venv/bin/python tools/gap-sweep.py reject "no fMRI" <doi-or-title> ...
  venv/bin/python tools/gap-sweep.py code rows.json
  venv/bin/python tools/gap-sweep.py dataset rows.json
  venv/bin/python tools/gap-sweep.py known

EVERYTHING MUST INVOLVE fMRI. Bimodal is fine ( EEG-fMRI , fMRI + MEG ) ; EEG-
only , MEG-only , fNIRS-only or modality-free papers are not. ` add ` refuses a
row whose tags lack fMRI , and ` code ` / ` dataset ` refuse a row without an
` fmri ` field saying where the fMRI is ( "func/*_bold runs in ds00xxxx" ).

"Already have" means any of : the Zotero library itself ( read from a copy of
zotero.sqlite through the repo's own fail-safe snapshot -- every item , not just
the board's , and as of this run ; the dashboard index is the fallback ) , the
live /sort main list , and the Sweep and Rejected tabs. Code and datasets are
also checked against the Inner Speech - Datasets sheet , which this only reads.

arXiv papers are keyed 10.48550/arXiv.<id> , the way the library spells them.
Google access is the Sheets REST API as banjo : the refresh token lives in
~/.config/prma/google-sheets-token.json , so an unattended run needs no consent.
"""

import csv
import datetime
import gzip
import io
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import requests

ROOT        = Path( __file__ ).resolve().parent.parent
SWEEP_ID    = "160yheZieArx8Y5QK1yx14WbGUhIXnQMjYmqUfH-1Cfc"   # Inner Speech - Literature Gap Sweep
DATASETS_ID = "1foTcOWLRAXgwGOOQB3PZ5DWY6EKg6bPDzm8fFxoA3Wk"   # Inner Speech - Datasets ( read only )
TOKEN       = Path( "~/.config/prma/google-sheets-token.json" ).expanduser()
CLIENT      = Path( "~/WORKSPACE/PYTHON/SCRIPTS/client_secrets_banjo.json" ).expanduser()
QUOTA       = "sound-memory-246307"
PROXY       = "https://doi-org.ezproxy.libraries.wright.edu/"   # the same prefix as the dashboards ( static/common.js )
SORT_CSV    = "http://localhost:9371/api/sort/export.csv"
DASH_INDEX  = ROOT / "output" / "cache" / "dashboard_index.json.gz"

FMRI_TAGS = { "fmri" , "7t-fmri" }

TABS = {
	"Sweep":         [ "DOI_or_Key" , "Title" , "Year" , "Tags" , "Tier" , "Theme" , "Why" , "Link" , "Found" , "Status" ] ,
	"Rejected":      [ "DOI_or_Key" , "Title" , "Year" , "Tags" , "Tier" , "Theme" , "Why" , "Link" , "Found" , "Reason" ] ,
	"Datasets":      [ "Title" , "ID" , "DOI" , "Used by" , "Categories" , "Code" , "Notes" , "Size (GB)" ] ,
	"Code Examples": [ "Name" , "Type" , "DOI" , "Notes" , "Size (GB)" , "Content" , "ULID" ] ,
}


# ---- google -----------------------------------------------------------------

def _headers():
	t = json.loads( TOKEN.read_text() )
	cs = json.loads( CLIENT.read_text() )[ "installed" ]
	r = requests.post( cs[ "token_uri" ] , data={
		"client_id": cs[ "client_id" ] , "client_secret": cs[ "client_secret" ] ,
		"refresh_token": t[ "refresh_token" ] , "grant_type": "refresh_token" } )
	r.raise_for_status()
	t.update( r.json() )
	TOKEN.write_text( json.dumps( t ) )
	os.chmod( TOKEN , 0o600 )
	return { "Authorization": "Bearer " + t[ "access_token" ] , "x-goog-user-project": QUOTA }

def _api( sid ):
	return f"https://sheets.googleapis.com/v4/spreadsheets/{sid}"

def _values( H , sid , rng ):
	r = requests.get( _api( sid ) + "/values/" + rng , headers=H )
	r.raise_for_status()
	return r.json().get( "values" , [] )

def _tabs( H , sid ):
	meta = requests.get( _api( sid ) , headers=H , params={ "fields": "sheets.properties" } ).json()
	return { s[ "properties" ][ "title" ]: s[ "properties" ][ "sheetId" ] for s in meta[ "sheets" ] }

def _sheet_id( H , sid , title ):
	gid = _tabs( H , sid ).get( title )
	if gid is None:
		raise SystemExit( f"no tab named {title!r} -- run ` gap-sweep.py init `" )
	return gid

def _batch( H , sid , reqs ):
	r = requests.post( _api( sid ) + ":batchUpdate" , headers=H , json={ "requests": reqs } )
	if not r.ok:
		raise SystemExit( f"{r.status_code} {r.text[:600]}" )
	return r.json()

def _cell( v , link=None ):
	if isinstance( v , ( int , float ) ) and not isinstance( v , bool ):
		c = { "userEnteredValue": { "numberValue": v } }
	else:
		c = { "userEnteredValue": { "stringValue": "" if v is None else str( v ) } }
	if link:
		c[ "textFormatRuns" ] = [ { "startIndex": 0 , "format": { "link": { "uri": link } } } ]
	return c

def _append( H , tab , rows ):
	_batch( H , SWEEP_ID , [ { "appendCells": { "sheetId": _sheet_id( H , SWEEP_ID , tab ) ,
		"rows": rows , "fields": "userEnteredValue,textFormatRuns" } } ] )


# ---- what we already have ---------------------------------------------------

def norm_doi( s ):
	s = str( s or "" ).strip().lower()
	s = re.sub( r"^https?://(dx\.)?doi\.org/" , "" , s )
	m = re.search( r"arxiv\.org/(abs|pdf|html)/(\d{4}\.\d{4,5})" , s ) or re.match( r"^(arxiv:)?(\d{4}\.\d{4,5})(v\d+)?$" , s )
	if m:
		return "10.48550/arxiv." + m.group( 2 )
	return re.sub( r"v\d+$" , "" , s ) if s.startswith( "10.48550/arxiv." ) else s

def norm_title( s ):
	return re.sub( r"[^a-z0-9]+" , " " , str( s or "" ).lower() ).strip()

def _zotero_items():
	"""[ ( doi , title , [ arXiv ids ] ) ] for every regular item in the live
	library , read from the snapshot copy src/zotero takes ( never the live DB )."""
	sys.path.insert( 0 , str( ROOT ) )
	from src.zotero.zotero import Zotero
	z = Zotero( SimpleNamespace( zotero_sqlite=None , output=ROOT / "output" ) )
	with z.open_snapshot() as conn:
		rows = conn.execute( """
			SELECT i.itemID , f.fieldName , v.value FROM items i
			JOIN itemTypes t ON t.itemTypeID = i.itemTypeID
			JOIN itemData d ON d.itemID = i.itemID
			JOIN fields f ON f.fieldID = d.fieldID
			JOIN itemDataValues v ON v.valueID = d.valueID
			WHERE t.typeName NOT IN ( 'attachment' , 'note' , 'annotation' )
			  AND f.fieldName IN ( 'DOI' , 'title' , 'url' , 'extra' , 'archiveID' )
			  AND i.itemID NOT IN ( SELECT itemID FROM deletedItems )""" ).fetchall()
	items = {}
	for iid , f , v in rows:
		items.setdefault( iid , {} )[ f ] = v or ""
	out = []
	for it in items.values():
		blob = " ".join( it.get( k , "" ) for k in ( "url" , "extra" , "archiveID" ) )
		ax = re.findall( r"(?:arxiv\.org/(?:abs|pdf|html)/|arxiv[:. ]\s*)(\d{4}\.\d{4,5})" , blob , re.I )
		out.append( ( it.get( "DOI" ) , it.get( "title" ) , ax ) )
	return out

def have_index( H=None , papers_only=False ):
	"""{ normalized doi or title : where it was found }"""
	idx = {}
	def put( doi , title , where ):
		for k in ( norm_doi( doi ) , norm_title( title ) ):
			if k and len( k ) > 6:
				idx.setdefault( k , where )
	try:
		for doi , title , ax in _zotero_items():
			put( doi , title , "library" )
			for a in ax:
				put( a , None , "library" )
	except Exception as e:
		print( f"( live Zotero unreadable , using the dashboard index : {e} )" , file=sys.stderr )
		try:
			for m in json.load( gzip.open( DASH_INDEX ) )[ "lib_meta" ].values():
				put( m.get( "doi" ) or m.get( "key" ) , m.get( "title" ) , "library ( index )" )
		except Exception as e2:
			print( f"( library index unreadable too : {e2} )" , file=sys.stderr )
	try:
		r = requests.get( SORT_CSV , timeout=20 )
		r.raise_for_status()
		for row in csv.DictReader( io.StringIO( r.content.decode( "utf-8-sig" ) ) ):
			put( row.get( "DOI_or_Key" ) , row.get( "Title" ) , "/sort" )
	except Exception as e:
		print( f"( /sort export unreachable -- is the server up ? {e} )" , file=sys.stderr )
	H = H or _headers()
	# Either tab may be gone : the Sweep tab gets emptied once its papers are
	# imported into Zotero ( the library check covers them from then on ) , and
	# Rejected only exists while something has been rejected.
	tabs = _tabs( H , SWEEP_ID )
	for tab in ( "Sweep" , "Rejected" ):
		if tab not in tabs:
			continue
		for row in _values( H , SWEEP_ID , f"'{tab}'!A2:B" ):
			row = row + [ "" , "" ]
			put( row[ 0 ] , row[ 1 ] , f"{tab} tab" )
	return idx

def lookup( idx , doi=None , title=None ):
	for k in ( norm_doi( doi ) , norm_title( title ) ):
		if k and k in idx:
			return idx[ k ]
	t = norm_title( title )
	if len( t ) > 25:   # a title that's a prefix of ours , or ours of it ( subtitles drop )
		for k , where in idx.items():
			if not k.startswith( "10." ) and len( k ) > 25 and ( k.startswith( t ) or t.startswith( k ) ):
				return where
	return None

def _tags( r ):
	t = r.get( "tags" ) or []
	return [ x.strip() for x in ( t.split( ";" ) if isinstance( t , str ) else t ) if x.strip() ]

def _has_fmri( r ):
	return any( t.lower() in FMRI_TAGS for t in _tags( r ) )


# ---- commands ---------------------------------------------------------------

def cmd_init():
	H = _headers()
	have = _tabs( H , SWEEP_ID )
	if "Sheet1" in have and "Sweep" not in have:
		_batch( H , SWEEP_ID , [ { "updateSheetProperties": { "properties": { "sheetId": have[ "Sheet1" ] ,
			"title": "Sweep" } , "fields": "title" } } ] )
		have[ "Sweep" ] = have.pop( "Sheet1" )
	new = [ t for t in TABS if t not in have ]
	if new:
		_batch( H , SWEEP_ID , [ { "addSheet": { "properties": { "title": t } } } for t in new ] )
		have = _tabs( H , SWEEP_ID )
	reqs = []
	for t , head in TABS.items():
		if _values( H , SWEEP_ID , f"'{t}'!A1:A1" ):
			continue
		reqs += [
			{ "updateSheetProperties": { "properties": { "sheetId": have[ t ] , "gridProperties": { "frozenRowCount": 1 } } ,
				"fields": "gridProperties.frozenRowCount" } } ,
			{ "updateCells": { "start": { "sheetId": have[ t ] , "rowIndex": 0 , "columnIndex": 0 } ,
				"rows": [ { "values": [ dict( _cell( h ) , userEnteredFormat={ "textFormat": { "bold": True } } ) for h in head ] } ] ,
				"fields": "userEnteredValue,userEnteredFormat.textFormat.bold" } } ]
	if reqs:
		_batch( H , SWEEP_ID , reqs )
	print( "tabs :" , list( _tabs( H , SWEEP_ID ) ) )

def cmd_check( items ):
	idx = have_index()
	for q in items:
		where = lookup( idx , q , q )
		print( f"{'HAVE' if where else 'NEW '}  {q}" + ( f"   <- {where}" if where else "" ) )

def cmd_known():
	H = _headers()
	tabs = _tabs( H , SWEEP_ID )
	for tab in ( "Sweep" , "Rejected" ):
		if tab not in tabs:
			continue
		for row in _values( H , SWEEP_ID , f"'{tab}'!A2:B" ):
			row = row + [ "" , "" ]
			print( f"{tab:9s} {row[ 0 ]:40s} {row[ 1 ][ :90 ]}" )

def _is_arxiv( doi , link ):
	return norm_doi( doi ).startswith( "10.48550/arxiv." ) or "arxiv.org" in ( link or "" )

def cmd_add( path ):
	"""Rows : { doi , title , year , tags , tier , theme , why , link }. tags must include fMRI."""
	rows = json.loads( Path( path ).read_text() )
	H = _headers()
	idx = have_index( H )
	today = datetime.date.today().isoformat()
	out , skipped = [] , []
	for r in rows:
		doi = r.get( "doi" ) or ""
		if not _has_fmri( r ):
			skipped.append( ( r.get( "title" ) , "refused : no fMRI tag -- papers must involve fMRI" ) ); continue
		where = lookup( idx , doi , r.get( "title" ) )
		if where:
			skipped.append( ( r.get( "title" ) , where ) ); continue
		key = norm_doi( doi ).replace( "10.48550/arxiv." , "10.48550/arXiv." ) if doi else ""
		link = r.get( "link" ) or ( f"https://doi.org/{key}" if key else "" )
		# The title opens the paper through the Wright State proxy , and so does
		# the Link column -- except for arXiv papers , which are free anyway.
		title_link = ( PROXY + key ) if key else link
		if key and not _is_arxiv( key , link ):
			link = PROXY + key
		out.append( { "values": [
			_cell( key ) , _cell( r.get( "title" ) , title_link or None ) , _cell( r.get( "year" ) ) ,
			_cell( "; ".join( _tags( r ) ) ) , _cell( r.get( "tier" ) ) , _cell( r.get( "theme" ) ) , _cell( r.get( "why" ) ) ,
			_cell( link , link or None ) , _cell( r.get( "found" ) or today ) , _cell( r.get( "status" ) or "new" ) ,
		] } )
		for k in ( norm_doi( doi ) , norm_title( r.get( "title" ) ) ):
			if k: idx[ k ] = "this batch"
	for t , where in skipped:
		print( f"skip  {str( t )[ :90 ]}   <- {where}" )
	if out:
		_append( H , "Sweep" , out )
	print( f"added {len( out )} , skipped {len( skipped )}" )

def cmd_reject( reason , items ):
	"""Move Sweep rows matching any of `items` ( DOI or title ) to Rejected , with `reason`."""
	H = _headers()
	gid = _sheet_id( H , SWEEP_ID , "Sweep" )
	got = requests.get( _api( SWEEP_ID ) , headers=H , params={ "ranges": "'Sweep'!A2:J" ,
		"fields": "sheets.data.rowData.values(userEnteredValue,textFormatRuns)" } ).json()
	data = got[ "sheets" ][ 0 ][ "data" ][ 0 ].get( "rowData" , [] )
	want = { norm_doi( q ) for q in items } | { norm_title( q ) for q in items }
	hits = []
	for i , rd in enumerate( data ):
		vals = rd.get( "values" , [] ) + [ {} ] * 10
		text = [ ( v.get( "userEnteredValue" ) or {} ).get( "stringValue" , "" ) for v in vals[ :2 ] ]
		if norm_doi( text[ 0 ] ) in want or norm_title( text[ 1 ] ) in want:
			moved = [ { k: v[ k ] for k in ( "userEnteredValue" , "textFormatRuns" ) if k in v } for v in vals[ :9 ] ]
			hits.append( ( i , { "values": moved + [ _cell( reason ) ] } , text[ 1 ] ) )
	if not hits:
		print( "no matching Sweep rows" ); return
	if "Rejected" not in _tabs( H , SWEEP_ID ):
		cmd_init()
	_append( H , "Rejected" , [ h[ 1 ] for h in hits ] )
	_batch( H , SWEEP_ID , [ { "deleteDimension": { "range": { "sheetId": gid , "dimension": "ROWS" ,
		"startIndex": i + 1 , "endIndex": i + 2 } } } for i , _ , _ in sorted( hits , reverse=True ) ] )
	for _ , _ , t in hits:
		print( f"rejected  {t[ :90 ]}" )


# ---- code and datasets ------------------------------------------------------

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

def _ulid_after( existing , n ):
	"""n ULIDs that sort after every ULID already in use , so a row copied over
	to the Inner Speech - Datasets sheet still sorts after that sheet's own."""
	def to_int( s ):
		v = 0
		for ch in s: v = v * 32 + _B32.index( ch )
		return v
	def to_str( v ):
		return "".join( _B32[ ( v >> ( 5 * i ) ) & 31 ] for i in reversed( range( 26 ) ) )
	ok = [ to_int( s ) for s in existing if re.fullmatch( r"[0-9A-HJKMNP-TV-Z]{26}" , s or "" ) ]
	start = max( ok ) if ok else int( datetime.datetime.now().timestamp() * 1000 ) << 80 | secrets.randbits( 80 )
	return [ to_str( start + i + 1 ) for i in range( n ) ]

def _github_summary( url ):
	"""( size in GB , 'Python · dir/ file …' ) for a github repo url , like the sheet's other rows."""
	m = re.search( r"github\.com/([^/]+)/([^/#?]+)" , url or "" )
	if not m:
		return "" , ""
	repo = f"{m.group( 1 )}/{m.group( 2 ).removesuffix( '.git' )}"
	H = {}
	try:
		tok = subprocess.run( [ "gh" , "auth" , "token" ] , capture_output=True , text=True , timeout=10 ).stdout.strip()
		if tok: H[ "Authorization" ] = "Bearer " + tok
	except Exception:
		pass
	try:
		info = requests.get( f"https://api.github.com/repos/{repo}" , headers=H , timeout=20 ).json()
		langs = list( requests.get( f"https://api.github.com/repos/{repo}/languages" , headers=H , timeout=20 ).json() )[ :2 ]
		top = requests.get( f"https://api.github.com/repos/{repo}/contents" , headers=H , timeout=20 ).json()
		dirs  = [ x[ "name" ] + "/" for x in top if x.get( "type" ) == "dir" ]
		files = [ x[ "name" ] for x in top if x.get( "type" ) == "file" and not x[ "name" ].startswith( "." ) and x[ "name" ] not in ( "LICENSE" , "README.md" ) ]
		listing = " ".join( dirs + files )
		if len( listing ) > 70:
			listing = listing[ :70 ].rsplit( " " , 1 )[ 0 ] + " …"
		return round( info.get( "size" , 0 ) / 1e6 , 3 ) , f"{', '.join( langs ) or '?'} · {listing}"
	except Exception as e:
		print( f"( github lookup failed for {repo} : {e} )" , file=sys.stderr )
		return "" , ""

def _need_fmri( rows ):
	ok = [ r for r in rows if str( r.get( "fmri" , "" ) ).strip() ]
	for r in rows:
		if r not in ok:
			print( f"refused  {r.get( 'name' ) or r.get( 'title' )} : no ` fmri ` field -- say where the fMRI is" )
	return ok

def cmd_code( path ):
	"""Rows : { name , url , type , doi , notes , fmri }."""
	rows = _need_fmri( json.loads( Path( path ).read_text() ) )
	H = _headers()
	mine   = _values( H , SWEEP_ID , "'Code Examples'!A1:G" )
	theirs = _values( H , DATASETS_ID , "'Code Examples'!A1:G" )
	names = { ( r[ 0 ] if r else "" ).strip().lower() for r in mine + theirs }
	dois  = { norm_doi( r[ 2 ] ) for r in mine + theirs if len( r ) > 2 and r[ 2 ] }
	fresh = [ r for r in rows if r[ "name" ].strip().lower() not in names and ( not r.get( "doi" ) or norm_doi( r[ "doi" ] ) not in dois ) ]
	for r in rows:
		if r not in fresh:
			print( f"skip  {r[ 'name' ]} : already in a Code Examples tab" )
	if not fresh:
		print( "nothing new for Code Examples" ); return
	ids = _ulid_after( [ r[ 6 ] for r in mine + theirs if len( r ) > 6 ] , len( fresh ) )
	out = []
	for r , ulid in zip( fresh , ids ):
		size , content = _github_summary( r[ "url" ] )
		notes = "; ".join( x for x in ( r.get( "notes" , "" ) , f"fMRI : {r[ 'fmri' ]}" ) if x )
		out.append( { "values": [ _cell( r[ "name" ] , r[ "url" ] ) , _cell( r.get( "type" , "" ) ) , _cell( r.get( "doi" , "" ) ) ,
			_cell( notes ) , _cell( size ) , _cell( content ) , _cell( ulid ) ] } )
	_append( H , "Code Examples" , out )
	print( f"Code Examples : added {len( out )}" , [ r[ "name" ] for r in fresh ] )

def cmd_dataset( path ):
	"""Rows : { title , id , id_url , doi , used_by , categories , code , code_url , notes , size_gb , fmri }."""
	rows = _need_fmri( json.loads( Path( path ).read_text() ) )
	H = _headers()
	have = { ( r[ 1 ] if len( r ) > 1 else "" ).strip().lower()
		for sid in ( SWEEP_ID , DATASETS_ID ) for r in _values( H , sid , "'Datasets'!A1:B" ) }
	fresh = [ r for r in rows if r[ "id" ].strip().lower() not in have ]
	for r in rows:
		if r not in fresh:
			print( f"skip  {r[ 'id' ]} : already in a Datasets tab" )
	if not fresh:
		print( "nothing new for Datasets" ); return
	out = []
	for r in fresh:
		notes = "; ".join( x for x in ( r.get( "notes" , "" ) , f"fMRI : {r[ 'fmri' ]}" ) if x )
		out.append( { "values": [ _cell( r[ "title" ] ) , _cell( r[ "id" ] , r.get( "id_url" ) ) , _cell( r.get( "doi" ) or "none" ) ,
			_cell( r.get( "used_by" , "" ) ) , _cell( r.get( "categories" , "" ) ) , _cell( r.get( "code" , "" ) , r.get( "code_url" ) ) ,
			_cell( notes ) , _cell( r.get( "size_gb" , "" ) ) ] } )
	_append( H , "Datasets" , out )
	print( f"Datasets : added {len( out )}" , [ r[ "id" ] for r in fresh ] )


if __name__ == "__main__":
	a = sys.argv[ 1: ]
	if not a:
		raise SystemExit( __doc__ )
	{ "init":    lambda: cmd_init() ,
	  "check":   lambda: cmd_check( a[ 1: ] ) ,
	  "known":   lambda: cmd_known() ,
	  "add":     lambda: cmd_add( a[ 1 ] ) ,
	  "reject":  lambda: cmd_reject( a[ 1 ] , a[ 2: ] ) ,
	  "code":    lambda: cmd_code( a[ 1 ] ) ,
	  "dataset": lambda: cmd_dataset( a[ 1 ] ) ,
	}.get( a[ 0 ] , lambda: sys.exit( __doc__ ) )()
