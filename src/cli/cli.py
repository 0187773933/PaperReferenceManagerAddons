import os
import argparse
from pathlib import Path
import src.tasks.tasks as tasks

def global_parser():
	g = argparse.ArgumentParser( add_help=False )
	g.add_argument( "--output" , type=Path , default=Path.cwd().joinpath( "output" ) ,
		help="Output Location" )
	g.add_argument( "--config" , type=Path , default=Path.cwd().joinpath( "config" ) ,
		help="Config Files Location" )
	g.add_argument( "--searches" , type=Path , default=Path.cwd().joinpath( "searches" ) ,
		help="Searches Files Location" )

	# Manager selection. Default is zotero ( single-manager ) ; pass
	# --manager all to snapshot every configured manager into the
	# unified DB in one shot.
	g.add_argument( "--manager" , type=str , default="zotero" ,
		help="Reference manager : zotero ( default ) | mendeley | all" )
	# Most commands ( yolo , ocr , images , ... ) auto-refresh the
	# papers/ DB before running so the user doesn't have to call
	# `prma snapshot` first. Pass --skip-snapshot when you know the
	# library hasn't changed and want to skip the round trip.
	g.add_argument( "--skip-snapshot" , dest="skip_snapshot" ,
		action="store_true" , default=False ,
		help="Skip the pre-task snapshot refresh ; assume papers/ is up to date" )
	g.add_argument( "--mendeley" , action="store_true" , default=False ,
		help="Mendeley Reference Manager" )
	g.add_argument( "--zotero" , action="store_true" , default=False ,
		help="Zotero Reference Manager" )
	g.add_argument( "--mendeley-source" , type=str , default="api" ,
		help="Mendeley Source :: API OR Local SQLite ( encrypted , todo... )" )
	g.add_argument( "--mendeley-sqlite" , type=Path , default=None ,
		help="Direct Path to Mendeley SQLite DB" )
	g.add_argument( "--zotero-sqlite" , type=Path , default=None ,
		help="Direct Path to Zotero SQLite DB" )
	g.add_argument( "--top-author-count" , type=int , default=100 ,
		help="Total Number of Most Common Authors" )

	# PDF
	g.add_argument( "--pdf-deskew" , action="store_true" , default=False ,
		help="Deskew PDFs before YOLO ( off by default ; only useful for scanned PDFs )" )
	g.add_argument( "--pdf-deskew-threshold" , type=float , default=0.5 , help="PDF Deskew Threshold" )

	# YOLO
	g.add_argument( "--yolo-model-path" , type=Path , default=Path.cwd().joinpath( "models" , "doclayout_yolo_docstructbench_imgsz1024.pt" ) ,
		help="Path to YOLO DocLayout Model" )
	g.add_argument( "--yolo-confidence" , type=float , default=0.1 ,
		help="YOLO Model Confidence" )
	return g

def base( sub , global_parser ):
	p = sub.add_parser(
		"main" ,
		parents=[ global_parser ] ,
		help="The base pipeline : refresh the snapshot , then update the OpenAlex cache ( meta + cited-by + references ) for EVERY library paper. This was the old no-subcommand default ; bare ` prma ` now launches the live server ( alias for ` prma server --watch ` ) instead."
	)
	p.set_defaults( _entry=tasks.main )
	return {}

def missing( sub , global_parser ):
	p = sub.add_parser(
		"missing" ,
		parents=[ global_parser ] ,
		help="Generate output/missing.xlsx : runs the base pipeline ( snapshot + OpenAlex cache update ) first , then computes the missing / cited-by / authors stats and runs the searches. Depends on the main task."
	)
	p.set_defaults( _entry=tasks.missing )
	return {}

def snapshot( sub , global_parser ):
	p = sub.add_parser(
		"snapshot" ,
		parents=[ global_parser ] ,
		help="Refresh the unified papers/ DB from the selected --manager. Default also PRUNES Zotero papers that no longer exist in your library."
	)
	p.add_argument( "--no-prune" , dest="no_prune" ,
		action="store_true" , default=False ,
		help="Don't detach sources / delete papers for DOIs missing from the current snapshot. Default behavior is sync ( prune ) for Zotero ; Mendeley is always append-only because its incremental API can miss IDs." )
	p.add_argument( "--prune-mendeley" , dest="prune_mendeley" ,
		action="store_true" , default=False ,
		help="Also prune for Mendeley ( only safe when you've just done a full re-fetch ; the incremental API can otherwise wrongly delete papers )" )
	p.set_defaults( _entry=tasks.snapshot )
	return {
		"no_prune": False ,
		"prune_mendeley": False ,
	}

def status( sub , global_parser ):
	p = sub.add_parser(
		"status" ,
		parents=[ global_parser ] ,
		help="Take a snapshot , then tally how far every paper has made it through the pipeline ( pdf / openalex / yolo / ocr / sections / md / text / methods / summaries ) -- done vs missing vs failed per stage. Prints a report and writes output/cache/status.json for the dashboard's /status page. Pass --skip-snapshot to tally the existing DB without refreshing."
	)
	p.set_defaults( _entry=tasks.status )
	return {}

def yolo( sub , global_parser ):
	p = sub.add_parser(
		"yolo" ,
		parents=[ global_parser ] ,
		help="Run doclayout-yolo on every PDF in output/cache/papers/ and merge the result into each paper's record"
	)
	p.add_argument( "--force" , dest="yolo_force" ,
		action="store_true" , default=False ,
		help="Re-run YOLO even on papers that already have a 'yolo' field" )
	p.set_defaults( _entry=tasks.yolo )
	return {
		"yolo_force": False ,
	}

def images( sub , global_parser ):
	p = sub.add_parser(
		"images" ,
		parents=[ global_parser ] ,
		help="Extract figure ( and optionally table ) crops + per-paper montages using each paper's YOLO bboxes ; runs YOLO inline for any paper that doesn't have it yet"
	)
	p.add_argument( "--include-tables" , dest="images_include_tables" ,
		action="store_true" , default=False ,
		help="Include tables in extraction ( off by default ; figures only )" )
	p.add_argument( "--no-montage" , dest="images_no_montage" ,
		action="store_true" , default=False ,
		help="Skip the per-paper grid montage ( ALL/ crops only )" )
	p.add_argument( "--montage-size" , dest="images_montage_size" ,
		choices=[ "original" , "high" , "medium" , "low" ] , default="medium" ,
		help="Montage scale relative to its natural size: original=100% , high=75% , medium=50% ( default ) , low=25%" )
	p.add_argument( "--force" , dest="images_force" ,
		action="store_true" , default=False ,
		help="Re-process even papers already marked as done ( either via paper.images marker or existing PNGs on disk )" )
	p.set_defaults( _entry=tasks.images )
	return {
		"images_include_tables": False ,
		"images_no_montage":     False ,
		"images_montage_size":   "medium" ,
		"images_force":          False ,
	}

def ocr( sub , global_parser ):
	p = sub.add_parser(
		"ocr" ,
		parents=[ global_parser ] ,
		help="Pin OCR text onto every text-bearing YOLO detection in output/cache/papers/ ; runs YOLO inline for any paper that doesn't have it yet"
	)
	p.add_argument( "--force-ocr" , dest="ocr_force" ,
		action="store_true" , default=False ,
		help="Skip the embedded-text path and OCR every bbox ( for PDFs with garbled text layers )" )
	p.add_argument( "--force" , dest="ocr_force_recompute" ,
		action="store_true" , default=False ,
		help="Overwrite det.ocr.<engine> even if already populated for this engine" )
	p.add_argument( "--max-pages" , dest="ocr_max_pages" ,
		type=int , default=None ,
		help="Cap pages OCR'd per PDF ( default: all pages YOLO covered )" )
	p.add_argument( "--engine" , dest="ocr_engine" ,
		choices=[ "rapid" , "paddle" , "surya" , "tesseract" ] , default="rapid" ,
		help="OCR backend: rapid ( default , PP-OCRv5 on ONNX , ~0.2s/page CPU ) , paddle ( same models on paddlepaddle , slower ) , surya ( transformer , best quality , slow ) , tesseract ( fallback )" )
	p.add_argument( "--lang" , dest="ocr_lang" , type=str , default="en" ,
		help="Document language code ( en , zh , fr , de , es ) ; default 'en'" )
	p.set_defaults( _entry=tasks.ocr )
	return {
		"ocr_force": False ,
		"ocr_force_recompute": False ,
		"ocr_max_pages": None ,
		"ocr_engine": "rapid" ,
		"ocr_lang": "en" ,
	}

def preprocess( sub , global_parser ):
	p = sub.add_parser(
		"preprocess" ,
		parents=[ global_parser ] ,
		help="Sort each paper's YOLO blocks ( with OCR text already pinned ) into reading order and stamp paper[ 'sections' ] ; reuses existing yolo + ocr data , does not re-run either"
	)
	p.add_argument( "--force" , dest="preprocess_force" ,
		action="store_true" , default=False ,
		help="Re-sort even papers that already have a 'sections' field" )
	p.set_defaults( _entry=tasks.preprocess )
	return {
		"preprocess_force": False ,
	}

def md( sub , global_parser ):
	p = sub.add_parser(
		"md" ,
		parents=[ global_parser ] ,
		help="Render a Markdown document for each paper at output/md/<doi>.md ; runs preprocess inline ( idempotent ) so 'prma md' is a one-stop command"
	)
	p.add_argument( "--force" , dest="md_force" ,
		action="store_true" , default=False ,
		help="Re-render even papers whose .md file already exists" )
	p.add_argument( "--include-references" , dest="md_include_references" ,
		action="store_true" , default=False ,
		help="Include the References / Bibliography section in the rendered .md ( default : drop it )" )
	p.set_defaults( _entry=tasks.md )
	return {
		"md_force":              False ,
		"md_include_references": False ,
	}

def text( sub , global_parser ):
	p = sub.add_parser(
		"text" ,
		parents=[ global_parser ] ,
		help="Render a plain-text document for each paper at output/text/<doi>.txt ; --source raw ( pymupdf full text , default ) or ocr ( walk yolo+ocr sections )"
	)
	p.add_argument( "--source" , dest="text_source" ,
		choices=[ "raw" , "ocr" ] , default="ocr" ,
		help="raw ( default ) -> emit paper[ 'raw_text' ] from pymupdf verbatim ; ocr -> rebuild text from yolo+ocr+sections , same content path as 'prma md'" )
	p.add_argument( "--force" , dest="text_force" ,
		action="store_true" , default=False ,
		help="Re-render even papers whose .txt file already exists" )
	p.add_argument( "--include-references" , dest="text_include_references" ,
		action="store_true" , default=False ,
		help="Include References / Bibliography in the output ( default : drop them ; for 'ocr' source we skip the references bucket , for 'raw' source we detect the References section in the full-text dump and truncate )" )
	p.set_defaults( _entry=tasks.text )
	return {
		"text_source":              "raw" ,
		"text_force":               False ,
		"text_include_references":  False ,
	}

def methods( sub , global_parser ):
	p = sub.add_parser(
		"methods" ,
		parents=[ global_parser ] ,
		help="Extract the Methods section of every paper ( from yolo + ocr + preprocess sections , with raw_text fallback ) and write one .txt file per paper at output/methods/<doi>.txt ; runs yolo + ocr + preprocess inline ( all idempotent ) so 'prma methods' is a one-stop command"
	)
	p.add_argument( "--force" , dest="methods_force" ,
		action="store_true" , default=False ,
		help="Re-render even papers whose .txt file already exists" )
	p.set_defaults( _entry=tasks.methods )
	return {
		"methods_force": False ,
	}

def summarize( sub , global_parser ):
	p = sub.add_parser(
		"summarize" ,
		parents=[ global_parser ] ,
		help="Feed each paper's section text through an LLM and write one Markdown file per paper at output/summaries/{section}/{doi}.md with a detailed , hashtag-tagged overview. Section defaults to 'all' ( one folder per summarizable section ). Source priority : output/{section}/{doi}.txt ( if a per-section task like 'prma methods' has been run ) , then output/md/{doi}.md ( so run 'prma md' first )."
	)
	p.add_argument( "summarize_section" ,
		nargs="?" , default="all" ,
		help="Which section to summarize : all ( default ) , abstract , introduction , background , methods , results , conclusions , future" )
	p.add_argument( "--provider" , dest="summarize_provider" ,
		choices=[ "claude" , "openai" , "gemini" ] , default="openai" ,
		help="LLM provider ( default : claude ). The model itself is read "
		     "from config.yaml gpts.<provider>.model ; "
		     "falls back to a built-in per-provider default if unset." )
	p.add_argument( "--force" , dest="summarize_force" ,
		action="store_true" , default=False ,
		help="Re-summarize even papers whose .md file already exists" )
	p.set_defaults( _entry=tasks.summarize )
	return {
		"summarize_section":  "all" ,
		"summarize_provider": "claude" ,
		"summarize_force":    False ,
	}

def rollup( sub , global_parser ):
	p = sub.add_parser(
		"rollup" ,
		parents=[ global_parser ] ,
		help="Roll up the per-paper summary .md files written by 'prma summarize' into a wide xlsx workbook at output/summaries/{section}.xlsx -- one row per paper , one column per discovered '**Subsection.**' header , plus a hashtag-histogram sheet. Decoupled from 'prma summarize' so layout / parser tweaks can be re-run without paying for more LLM calls."
	)
	p.add_argument( "rollup_section" ,
		nargs="?" , default="all" ,
		help="Which section folder to roll up : all ( default ) , abstract , introduction , background , methods , results , conclusions , future" )
	p.set_defaults( _entry=tasks.rollup )
	return {
		"rollup_section": "all" ,
	}

def search( sub , global_parser ):
	p = sub.add_parser(
		"search" ,
		parents=[ global_parser ] ,
		help="Run every predicate from args.searches / *.py against the text we have for each library paper ( title + raw_text + per-section LLM summaries + ` prma text ` / ` prma methods ` outputs ) and emit hits to output/searches/{search-slug}.md + a combined output/searches/searches.xlsx ( one sheet per search ). Same predicates the OpenAlex pipeline uses for missing.xlsx -- this just turns them on the LOCAL library instead."
	)
	p.add_argument( "--search-file" , dest="search_files" ,
		action="append" , default=None ,
		help="Load only this search file ( repeatable ; name relative to "
		     "--searches , or absolute path ). Default : every *.py under --searches." )
	p.set_defaults( _entry=tasks.search )
	return {
		"search_files": None ,
	}

def mendeley( sub , global_parser ):
	p = sub.add_parser(
		"mendeley" ,
		parents=[ global_parser ] ,
		help="Mendeley-specific tasks"
	)
	msub = p.add_subparsers( dest="mendeley_command" , metavar="<mendeley-command>" )

	p_dl = msub.add_parser(
		"download" ,
		parents=[ global_parser ] ,
		help="Download PDFs for everything in the Mendeley snapshot"
	)
	p_dl.set_defaults(
		_entry=tasks.mendeley_download ,
		mendeley_download=True ,    # downstream code may read args.mendeley_download
	)
	# 'mendeley yolo' subcommand removed -- use `prma yolo` ( unified DB )
	# 'mendeley snapshot' subcommand removed -- use `prma snapshot --manager mendeley`
	# 'mendeley images' subcommand removed -- use `prma images --manager mendeley`
	# 'mendeley ocr'    subcommand removed -- use `prma ocr    --manager mendeley`
	return {
		"mendeley_download": False ,
	}

def zotero( sub , global_parser ):
	p = sub.add_parser(
		"zotero" ,
		parents=[ global_parser ] ,
		help="Zotero-specific tasks"
	)
	msub = p.add_subparsers( dest="zotero_command" , metavar="<zotero-command>" )
	# 'zotero yolo' subcommand removed -- use `prma yolo` ( unified DB )
	# 'zotero snapshot' subcommand removed -- use `prma snapshot --manager zotero`
	# 'zotero images'   subcommand removed -- use `prma images   --manager zotero`
	# 'zotero ocr'      subcommand removed -- use `prma ocr      --manager zotero`
	return {}

def crawl( sub , global_parser ):
	p = sub.add_parser(
		"crawl" ,
		parents=[ global_parser ] ,
		help="Keyword spider crawl over the OpenAlex graph"
	)
	p.add_argument( "--max-visits"     , dest="crawl_max_visits"     , type=int   , default=500  ,
		help="Maximum papers to visit" )
	p.add_argument( "--max-depth"      , dest="crawl_max_depth"      , type=int   , default=2    ,
		help="Maximum hops from a seed paper" )
	p.add_argument( "--min-seed-hits"  , dest="crawl_min_seed_hits"  , type=int   , default=1    ,
		help="Minimum predicate hits required to seed a library paper" )
	p.add_argument( "--min-novel-hits" , dest="crawl_min_novel_hits" , type=int   , default=2    ,
		help="Minimum predicate hits required to record a novel discovery" )
	p.add_argument( "--no-fetch"       , dest="crawl_no_fetch"       , action="store_true" , default=False ,
		help="Never hit the OpenAlex API ; rely only on cached data" )
	p.add_argument( "--api-budget"     , dest="crawl_api_budget"     , type=int   , default=1000 ,
		help="Hard cap on OpenAlex API fetches for a single crawl" )
	p.add_argument( "--cite-weight"    , dest="crawl_cite_weight"    , type=float , default=0.4  ,
		help="Weight on log10( cites+1 ) when scoring a candidate" )
	p.add_argument( "--link-weight"    , dest="crawl_link_weight"    , type=float , default=0.5  ,
		help="Weight on incoming-link count from visited frontier ( graph density )" )
	p.add_argument( "--no-cited-by"    , dest="crawl_no_cited_by"    , action="store_true" , default=False ,
		help="Skip the forward ( cited-by ) wedge ; only walk references" )
	p.add_argument( "--search-file"    , dest="crawl_search_files"   , action="append"     , default=None ,
		help="Load only this search file ( repeatable ; name relative to --searches , or absolute path )" )
	p.add_argument( "--out-name"       , dest="crawl_out_name"       , type=str            , default=None ,
		help="Output xlsx filename ( default: crawl-<search-file-stems>.xlsx , or crawl.xlsx if no --search-file )" )
	p.set_defaults( _entry=tasks.crawl , crawl=True )
	return {
		"crawl":                False ,
		"crawl_max_visits":     500   ,
		"crawl_max_depth":      2     ,
		"crawl_min_seed_hits":  1     ,
		"crawl_min_novel_hits": 2     ,
		"crawl_no_fetch":       False ,
		"crawl_api_budget":     1000  ,
		"crawl_cite_weight":    0.4   ,
		"crawl_link_weight":    0.5   ,
		"crawl_no_cited_by":    False ,
		"crawl_search_files":   None  ,
		"crawl_out_name":       None  ,
	}

def process( sub , global_parser ):
	p = sub.add_parser(
		"process" ,
		parents=[ global_parser ] ,
		help="Find ONE paper in your manager by DOI or title and run the full per-paper suite on just it ( snapshot -> openalex -> yolo -> ocr -> images -> methods -> md ) , then reindex so the dashboard refreshes. The library tasks are idempotent and scoped to the single paper. Pass --summarize to also run the LLM summary ( costs money )."
	)
	p.add_argument( "process_query" ,
		help="DOI ( exact ) or title ( fuzzy-matched ) of the paper to process" )
	p.add_argument( "--summarize" , dest="process_summarize" ,
		action="store_true" , default=False ,
		help="Also run the LLM summarize stage ( off by default ; costs LLM calls )" )
	p.set_defaults( _entry=tasks.process )
	return {
		"process_summarize": False ,
	}

def reindex( sub , global_parser ):
	p = sub.add_parser(
		"reindex" ,
		parents=[ global_parser ] ,
		help="Refresh the OpenAlex cache ( snapshot + fetch new papers ) , then (re)build the dashboard's OWN full-text search index by streaming the OpenAlex cache off disk , and persist it. Incremental : adding papers only re-reads the new papers + their new references , not everything. A running ` prma server ` auto-picks-up the fresh index. Use --skip-snapshot to index the existing cache without re-fetching ; --full to rebuild from scratch."
	)
	p.add_argument( "--full" , dest="reindex_full" , action="store_true" , default=False ,
		help="Ignore the saved index state and rebuild from scratch ( e.g. after regenerating text / summaries , which the per-paper signature doesn't watch )" )
	p.set_defaults( _entry=tasks.reindex )
	return {
		"reindex_full": False ,
	}

def server( sub , global_parser ):
	p = sub.add_parser(
		"server" ,
		parents=[ global_parser ] ,
		help="Run the local HTTP server : the 'exists' endpoint for browser userscripts ( POST /exists ) AND the full-text-search dashboard ( GET / ). The dashboard serves the index that ` prma reindex ` persists to disk -- it loads instantly and auto-reloads when you re-run reindex. If no index exists yet , it builds one lazily on first open."
	)
	p.add_argument( "--host"     , default=os.environ.get( "SERVER_HOST" , "127.0.0.1" ) ,
		help="Bind host ( env SERVER_HOST )" )
	p.add_argument( "--port"     , type=int , default=int( os.environ.get( "SERVER_PORT" , "9371" ) ) ,
		help="Bind port ( env SERVER_PORT )" )
	p.add_argument( "--debounce" , type=float , default=0.5 ,
		help="Min seconds between source stat() checks" )
	p.add_argument( "--ttl"      , type=float , default=60.0 ,
		help="TTL for managers without an mtime watch ( Mendeley API )" )
	p.add_argument( "--watch" , dest="watch" , action="store_true" , default=False ,
		help="Live processing : a background worker detects papers you add to "
		     "your manager , runs the full per-paper suite on each "
		     "( openalex -> yolo -> ocr -> images -> methods -> md ) , and "
		     "rebuilds the dashboard index so new papers appear automatically. "
		     "Off by default ( the suite is CPU-heavy )." )
	p.add_argument( "--watch-summarize" , dest="watch_summarize" ,
		action="store_true" , default=False ,
		help="Also run the LLM summarize stage on each auto-processed paper "
		     "( only meaningful with --watch ; costs LLM calls )." )
	p.add_argument( "--no-watch-backlog" , dest="watch_backlog" ,
		action="store_false" , default=True ,
		help="With --watch , skip the startup backlog sweep ( don't process "
		     "papers already in the library that have undone tasks ; only act "
		     "on papers added after the server starts )." )
	p.set_defaults( _entry=tasks.server )
	return {
		"host":            "127.0.0.1" ,
		"port":            9371        ,
		"debounce":        0.5         ,
		"ttl":             60.0        ,
		"watch":           False       ,
		"watch_summarize": False       ,
		"watch_backlog":   True        ,
	}

REGISTRARS = (
	base       ,
	missing    ,
	snapshot   ,
	status     ,
	yolo       ,
	images     ,
	ocr        ,
	preprocess ,
	md         ,
	text       ,
	methods    ,
	summarize  ,
	rollup     ,
	search     ,
	mendeley   ,
	zotero     ,
	crawl      ,
	process    ,
	reindex    ,
	server     ,
)

def cli():
	_global_parser = global_parser()

	parser = argparse.ArgumentParser(
		prog="prma" ,
		parents=[ _global_parser ] ,
		description="Paper Reference Manager Addons" ,
	)

	sub = parser.add_subparsers( dest="command" , metavar="<command>" )

	top_defaults = {}
	for register in REGISTRARS:
		top_defaults.update( register( sub , _global_parser ) or {} )
	parser.set_defaults( **top_defaults )

	args = parser.parse_args()

	# Bare ` prma ` ( no subcommand ) is an alias for ` prma server --watch ` :
	# it launches the exists endpoint + dashboard + the live auto-processing
	# worker. ( The old base pipeline -- snapshot + full OpenAlex backfill --
	# is still available as ` prma main ` . ) Every server arg the worker reads
	# ( host / port / debounce / ttl / watch_summarize / watch_backlog ) is
	# already on the namespace via the server registrar's top-level defaults ;
	# we just flip watch on.
	if getattr( args , "_entry" , None ) is None:
		args.watch  = True
		args._entry = tasks.server

	entry = args._entry
	entry( args )