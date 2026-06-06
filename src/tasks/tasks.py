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

def methods( args ):
	_ensure_snapshot( args )
	from . import methods
	methods.run( args )

def summarize( args ):
	# No snapshot here : summarize depends on md / per-section output
	# that the user has already rendered. Running a snapshot mid-stream
	# can't help -- it'd just add new papers that don't have md yet.
	from . import summarize
	summarize.run( args )

def rollup( args ):
	# No snapshot here either ; rollup is a pure post-processing pass
	# over the per-paper .md files prma summarize already wrote.
	from . import summarize_rollup
	summarize_rollup.run( args )

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