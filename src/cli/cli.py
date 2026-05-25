import os
import argparse
from pathlib import Path
import src.tasks.tasks as tasks

# ---------------------------------------------------------------------------
# Shared / global options
#
# Every subcommand inherits these via parents=[ _global_parser() ] , so they
# work both before and after the subcommand name:
#
#     prma --output ./out crawl --max-visits 500
#     prma crawl --max-visits 500 --output ./out
#
# parse_args() returns one flat Namespace -- the dest= calls below pin the
# attribute names so the downstream code ( tasks.py , api.py , etc. ) sees
# the same args.crawl_max_visits / args.mendeley_download names it always
# did. The CLI is nested ; the parsed object is not.
# ---------------------------------------------------------------------------

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
		help="Optionally Specify Direct Path to Mendeley SQLite DB" )
	g.add_argument( "--zotero-sqlite" , type=Path , default=None ,
		help="Optionally Specify Direct Path to Zotero SQLite DB" )
	g.add_argument( "--top-author-count" , type=int , default=100 ,
		help="Optionally Specify Total Number of Most Common Authors" )
	return g


def cli():
	global_parser = _global_parser()

	parser = argparse.ArgumentParser(
		prog="prma" ,
		parents=[ global_parser ] ,
		description="Paper Reference Manager Addons" ,
	)

	sub = parser.add_subparsers( dest="command" , metavar="<command>" )

	# -------- snapshot ( current default flow : update OA cache + stats ) -----
	p_snapshot = sub.add_parser(
		"snapshot" ,
		parents=[ global_parser ] ,
		help="Take a library snapshot and refresh OpenAlex cache + stats"
	)
	p_snapshot.set_defaults( _entry=tasks.main )

	# -------- mendeley : grouping for Mendeley-only subcommands --------------
	p_mendeley = sub.add_parser(
		"mendeley" ,
		parents=[ global_parser ] ,
		help="Mendeley-specific tasks"
	)
	mendeley_sub = p_mendeley.add_subparsers( dest="mendeley_command" , metavar="<mendeley-command>" )

	p_m_download = mendeley_sub.add_parser(
		"download" ,
		parents=[ global_parser ] ,
		help="Download PDFs for everything in the Mendeley snapshot"
	)
	p_m_download.set_defaults(
		_entry=tasks.mendeley_download ,
		mendeley_download=True ,    # preserved for any downstream code that reads it
	)

	# -------- crawl ----------------------------------------------------------
	p_crawl = sub.add_parser(
		"crawl" ,
		parents=[ global_parser ] ,
		help="Keyword spider crawl over the OpenAlex graph"
	)
	p_crawl.add_argument( "--max-visits"     , dest="crawl_max_visits"     , type=int   , default=500  ,
		help="Maximum papers to visit" )
	p_crawl.add_argument( "--max-depth"      , dest="crawl_max_depth"      , type=int   , default=2    ,
		help="Maximum hops from a seed paper" )
	p_crawl.add_argument( "--min-seed-hits"  , dest="crawl_min_seed_hits"  , type=int   , default=1    ,
		help="Minimum predicate hits required to seed a library paper" )
	p_crawl.add_argument( "--min-novel-hits" , dest="crawl_min_novel_hits" , type=int   , default=1    ,
		help="Minimum predicate hits required to record a novel discovery" )
	p_crawl.add_argument( "--no-fetch"       , dest="crawl_no_fetch"       , action="store_true" , default=False ,
		help="Never hit the OpenAlex API ; rely only on cached data" )
	p_crawl.add_argument( "--api-budget"     , dest="crawl_api_budget"     , type=int   , default=1000 ,
		help="Hard cap on OpenAlex API fetches for a single crawl" )
	p_crawl.add_argument( "--cite-weight"    , dest="crawl_cite_weight"    , type=float , default=0.4  ,
		help="Weight on log10( cites+1 ) when scoring a candidate" )
	p_crawl.add_argument( "--link-weight"    , dest="crawl_link_weight"    , type=float , default=0.5  ,
		help="Weight on incoming-link count from visited frontier ( graph density )" )
	p_crawl.add_argument( "--no-cited-by"    , dest="crawl_no_cited_by"    , action="store_true" , default=False ,
		help="Skip the forward ( cited-by ) wedge ; only walk references" )
	p_crawl.set_defaults( _entry=tasks.crawl , crawl=True )

	# -------- server ---------------------------------------------------------

	p_server = sub.add_parser(
		"server" ,
		parents=[ global_parser ] ,
		help="Run the local 'exists' HTTP server for browser userscripts"
	)
	p_server.add_argument( "--host"     , default=os.environ.get( "SERVER_HOST" , "127.0.0.1" ) ,
		help="Bind host ( env SERVER_HOST )" )
	p_server.add_argument( "--port"     , type=int , default=int( os.environ.get( "SERVER_PORT" , "9371" ) ) ,
		help="Bind port ( env SERVER_PORT )" )
	p_server.add_argument( "--debounce" , type=float , default=0.5 ,
		help="Min seconds between source stat() checks" )
	p_server.add_argument( "--ttl"      , type=float , default=60.0 ,
		help="TTL for managers without an mtime watch ( Mendeley API )" )
	p_server.set_defaults( _entry=tasks.server )

	# -------------------------------------------------------------------------
	# Defaults that keep the flat namespace stable across all subcommands , so
	# existing code reading args.crawl / args.mendeley_download still works
	# whichever subcommand was invoked.
	parser.set_defaults(
		crawl=False ,
		mendeley_download=False ,
		# Crawler knobs need to exist on args even when not crawling , because
		# tasks.crawl( args ) reads them directly. Subcommand defaults ( above )
		# overlay these.
		crawl_max_visits=500 ,
		crawl_max_depth=2 ,
		crawl_min_seed_hits=1 ,
		crawl_min_novel_hits=1 ,
		crawl_no_fetch=False ,
		crawl_api_budget=1000 ,
		crawl_cite_weight=0.4 ,
		crawl_link_weight=0.5 ,
		crawl_no_cited_by=False ,
		# Server knobs default here so nothing AttributeErrors if a code path
		# reads them outside the `server` subcommand.
		host="127.0.0.1" ,
		port=9371 ,
		debounce=0.5 ,
		ttl=60.0 ,
	)

	args = parser.parse_args()

	# No subcommand -> default behaviour ( same as `snapshot` )
	entry = getattr( args , "_entry" , None ) or tasks.main
	entry( args )