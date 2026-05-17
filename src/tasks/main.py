def main( args ):
	from ..utils import utils
	from . import snapshot
	from pathlib import Path
	from ..openalex.openalex import OpenAlex

	# 0.) Prep
	cache_dir = args.output.joinpath( "cache" )
	cache_dir.mkdir( parents=True , exist_ok=True )

	# 1.) Get Snapshot
	snapshot = snapshot.get_common( args )

	# 2.) Update OpenAlex Cache
	oa = OpenAlex( args )
	oa.update_cache( snapshot )

	# 3.) Stats
	oa_index = oa.Stats.compute( snapshot )

	# 4.) Searches
	searches = utils.load_searches( args.searches )
	oa.search( oa_index , searches )