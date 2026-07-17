def _ensure_snapshot( args ):
	if getattr( args , "skip_snapshot" , False ):
		return
	from . import snapshot
	snapshot.get_common( args )

def main( args ):
	from . import main
	main.main( args )

def missing( args ):
	# missing depends on the base pipeline ; main.missing() runs main()
	# ( snapshot + OpenAlex cache update ) first , then computes the stats
	# and searches that produce output/missing.xlsx.
	from . import main
	main.missing( args )

def snapshot( args ):
	from . import snapshot
	snapshot.get_common( args )

def status( args ):
	# Snapshot ( unless --skip-snapshot ) then tally pipeline completeness ;
	# writes output/cache/status.json for the dashboard's /status page.
	from . import status
	status.run( args )

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

def search( args ):
	# No snapshot here -- search reads whatever text we already have on
	# disk for each paper. Run prma snapshot / md / methods / summarize
	# beforehand for a richer haystack.
	from . import library_search
	library_search.run( args )

def mendeley_download( args ):
	_ensure_snapshot( args )
	from ..mendeley.mendeley import Mendeley
	m = Mendeley( args )
	m.download()

def server( args ):
	# The server now hosts BOTH the lightweight /exists endpoint and the
	# full-text-search dashboard ( GET / ). The dashboard serves the index
	# ` prma reindex ` persisted to disk -- see src/server/server.py.
	from ..server.server import run
	run( args )

def reindex( args ):
	# Build ( or incrementally refresh ) the dashboard's own full-text index
	# and persist it to disk. A running ` prma server ` picks the fresh index
	# up automatically. Independent of the missing.xlsx pipeline.
	from ..dashboard import indexer
	indexer.reindex( args , full=getattr( args , "reindex_full" , False ) )

def crawl( args ):
	from . import crawl
	crawl.crawl( args )

def process( args ):
	# Find one paper by DOI / title and run the full per-paper suite on just
	# it ( openalex -> yolo -> ocr -> images -> methods -> code -> md -> modalities
	# [ -> summarize ] ) , then reindex so the dashboard reflects it. Shares
	# run_suite() with the server's live --watch worker.
	from . import process as process_task
	process_task.run( args )

def method_images( args ):
	# No snapshot : like rollup , this is a pure post-processing pass over
	# outputs the PDF suite ( yolo -> ocr -> images -> methods -> code -> md -> modalities )
	# already wrote. Papers the suite hasn't reached are skipped + counted.
	from . import method_images
	method_images.run( args )

def modalities( args ):
	# No snapshot : like method-images , a pure pass over what the suite
	# already produced ( OpenAlex cache , methods .txt , md ) -- it just
	# stamps paper[ 'modalities' ] on each record.
	from . import modalities
	modalities.run( args )

def code( args ):
	# Scan every paper's OpenAlex abstract + OCR text for source-code / data
	# links , pin them on each record for the dashboard's "Code" column , fetch
	# each unique GitHub repo's ( github.api_key ) and OSF node's ( osfio.api_key )
	# details so they can be method-tagged , then roll everything up into
	# output/code/code.xlsx. Shares run() with the 'code' stage run_suite drives.
	_ensure_snapshot( args )
	from . import code
	code.run( args )
	code.fetch_github( args )
	code.resolve_github( args )
	code.fetch_osf( args )
	code.write_workbook( args )