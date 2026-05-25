def main( args ):
	from . import main
	main.main( args )

def mendeley_download( args ):
	from ..mendeley.mendeley import Mendeley
	m = Mendeley( args )
	m.download()

def server( args ):
	from ..server.server import run
	run( args )

def crawl( args ):
	from ..crawler.crawler import Crawler
	from ..utils import utils
	searches = utils.load_searches( args.searches )
	c = Crawler( args )
	c.crawl(
		searches ,
		max_visits=args.crawl_max_visits ,
		max_depth=args.crawl_max_depth ,
		min_seed_hits=args.crawl_min_seed_hits ,
		min_novel_hits=args.crawl_min_novel_hits ,
		do_fetch=not args.crawl_no_fetch ,
		api_budget=args.crawl_api_budget ,
		cite_weight=args.crawl_cite_weight ,
		link_weight=args.crawl_link_weight ,
		expand_cited_by=not args.crawl_no_cited_by ,
	)