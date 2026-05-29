def crawl( args ):
	from pathlib import Path
	from ..crawler.crawler import Crawler
	from ..utils import utils
	from . import snapshot as snap_mod
	snapshot = snap_mod.get_common( args )
	searches = utils.load_searches( args.searches , files=args.crawl_search_files )

	if args.crawl_out_name:
		out_name = args.crawl_out_name
		if not out_name.endswith( ".xlsx" ):
			out_name = out_name + ".xlsx"
	elif args.crawl_search_files:
		stems = [ Path( f ).stem for f in args.crawl_search_files ]
		out_name = "crawl-" + "+".join( stems ) + ".xlsx"
	else:
		out_name = "crawl.xlsx"

	c = Crawler( args )
	c.crawl(
		searches ,
		snapshot=snapshot ,
		out_name=out_name ,
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