def _ensure_snapshot( args ):
	if getattr( args , "skip_snapshot" , False ):
		return
	from . import snapshot
	snapshot.get_common( args )

def main( args ):
	from . import main
	main.main( args )

def snapshot( args ):
	from . import snapshot
	snapshot.get_common( args )

def yolo( args ):
	_ensure_snapshot( args )
	from . import yolo
	yolo.run( args )

def images( args ):
	_ensure_snapshot( args )
	from . import images
	images.run( args )

def ocr( args ):
	_ensure_snapshot( args )
	from . import ocr
	ocr.run( args )

def preprocess( args ):
	_ensure_snapshot( args )
	from . import preprocess
	preprocess.run( args )

def md( args ):
	_ensure_snapshot( args )
	from . import md
	md.run( args )

def text( args ):
	_ensure_snapshot( args )
	from . import text
	text.run( args )

def mendeley_download( args ):
	_ensure_snapshot( args )
	from ..mendeley.mendeley import Mendeley
	m = Mendeley( args )
	m.download()

def server( args ):
	from ..server.server import run
	run( args )

def crawl( args ):
	from . import crawl
	crawl.crawl( args )