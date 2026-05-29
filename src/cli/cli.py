import os
import argparse
from pathlib import Path
import src.tasks.tasks as tasks


def _global_parser():
	g = argparse.ArgumentParser( add_help=False )
	g.add_argument( "--output" , type=Path , default=Path.cwd().joinpath( "output" ) ,
		help="Output Location" )
	g.add_argument( "--config" , type=Path , default=Path.cwd().joinpath( "config" ) ,
		help="Config Files Location" )
	g.add_argument( "--searches" , type=Path , default=Path.cwd().joinpath( "searches" ) ,
		help="Searches Files Location" )

	# Manager selection
	g.add_argument( "--manager" , type=str , default="zotero" ,
		help="Mendeley/Zotero/EndNote/Paperpile/RefWorks/ReadCube" )
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

	# YOLO
	g.add_argument( "--yolo-model-path" , type=Path , default=Path.cwd().joinpath( "models" , "doclayout_yolo_docstructbench_imgsz1024.pt" ) ,
		help="Path to YOLO DocLayout Model" )
	g.add_argument( "--yolo-confidence" , type=float , default=0.1 ,
		help="YOLO Model Confidence" )
	return g


def _snapshot( sub , global_parser ):
	p = sub.add_parser(
		"snapshot" ,
		parents=[ global_parser ] ,
		help="Take a library snapshot and refresh OpenAlex cache + stats"
	)
	p.set_defaults( _entry=tasks.main )
	return {}

def _mendeley( sub , global_parser ):
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
	return {
		"mendeley_download": False ,
	}

def _crawl( sub , global_parser ):
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

def _server( sub , global_parser ):
	p = sub.add_parser(
		"server" ,
		parents=[ global_parser ] ,
		help="Run the local 'exists' HTTP server for browser userscripts"
	)
	p.add_argument( "--host"     , default=os.environ.get( "SERVER_HOST" , "127.0.0.1" ) ,
		help="Bind host ( env SERVER_HOST )" )
	p.add_argument( "--port"     , type=int , default=int( os.environ.get( "SERVER_PORT" , "9371" ) ) ,
		help="Bind port ( env SERVER_PORT )" )
	p.add_argument( "--debounce" , type=float , default=0.5 ,
		help="Min seconds between source stat() checks" )
	p.add_argument( "--ttl"      , type=float , default=60.0 ,
		help="TTL for managers without an mtime watch ( Mendeley API )" )
	p.set_defaults( _entry=tasks.server )
	return {
		"host":     "127.0.0.1" ,
		"port":     9371        ,
		"debounce": 0.5         ,
		"ttl":      60.0        ,
	}


REGISTRARS = (
	_snapshot ,
	_mendeley ,
	_crawl    ,
	_server   ,
)

def cli():
	global_parser = _global_parser()

	parser = argparse.ArgumentParser(
		prog="prma" ,
		parents=[ global_parser ] ,
		description="Paper Reference Manager Addons" ,
	)

	sub = parser.add_subparsers( dest="command" , metavar="<command>" )

	top_defaults = {}
	for register in REGISTRARS:
		top_defaults.update( register( sub , global_parser ) or {} )
	parser.set_defaults( **top_defaults )

	args = parser.parse_args()

	# No subcommand -> default behaviour ( same as `snapshot` )
	entry = getattr( args , "_entry" , None ) or tasks.main
	entry( args )
