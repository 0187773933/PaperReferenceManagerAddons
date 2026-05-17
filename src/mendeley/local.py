import sqlite3
import glob
import os
import tempfile
import shutil
from pathlib import Path
from pprint import pprint


class MendeleyLocal():
	def __init__( self , args ):
		self.args = args
		if args.mendeley_sqlite and args.mendeley_sqlite.strip():
			self.sqlite_path = Path( args.mendeley_sqlite )
		else:
			self.sqlite_path = self.find_db()
		if self.sqlite_path:
			self.storage = self.sqlite_path.parent.joinpath( "storage" )

	def find_db( self ):
		candidates = [
			os.path.expanduser( "~/Library/Application Support/Mendeley Desktop/*@www.mendeley.com.sqlite" ) ,
			os.path.expanduser( "~/.local/share/data/Mendeley Ltd./Mendeley Desktop/*@www.mendeley.com.sqlite" ) ,
			os.path.join( os.environ.get( "LOCALAPPDATA" , "" ) , "Mendeley Ltd." , "Mendeley Desktop" , "*@www.mendeley.com.sqlite" ) ,
		]
		for pattern in candidates:
			hits = glob.glob( pattern )
			if hits:
				return Path( hits[ 0 ] )
		print( "No Mendeley Local .sqlite found" )
		return False

	def take_snapshot( self ):
		tmp = tempfile.NamedTemporaryFile( suffix=".sqlite" , delete=False )
		tmp.close()
		shutil.copy2( self.sqlite_path , tmp.name )
		conn = sqlite3.connect( f"file:{tmp.name}?mode=ro" , uri=True )
		conn.row_factory = sqlite3.Row
		try:
			cur = conn.execute("""
				SELECT id, type, title, year, doi, abstract, publication
				FROM Documents
				WHERE deletionPending = 'false'
				ORDER BY id
			""")
			for row in cur:
				doc = dict( row )
				auth = conn.execute("""
					SELECT firstNames, lastName
					FROM DocumentContributors
					WHERE documentId = ? AND contribution = 'DocumentAuthor'
					ORDER BY id
				""" , ( doc[ "id" ] , ) ).fetchall()
				doc[ "authors" ] = [
					f"{a['firstNames']} {a['lastName']}".strip()
					for a in auth
				]
				yield doc
		finally:
			conn.close()
			os.unlink( tmp.name )

	def snapshot( self ):
		print( self.sqlite_path )
		for i , doc in enumerate( self.take_snapshot() ):
			print( f"=== [{i}] doc {doc['id']} ===" )
			pprint( doc )
		return {}