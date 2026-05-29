def main( args ):
	from . import main
	main.main( args )

def mendeley_download( args ):
	from ..mendeley.mendeley import Mendeley
	m = Mendeley( args )
	m.download()

def mendeley_yolo( args ):
	from ..mendeley.mendeley import Mendeley
	m = Mendeley( args )
	m.yolo()

def zotero_yolo( args ):
	from ..zotero.zotero import Zotero
	z = Zotero( args )
	z.yolo()

def server( args ):
	from ..server.server import run
	run( args )

def crawl( args ):
	from . import crawl
	crawl.crawl( args )