def main( args ):
	"""prma ( no subcommand ) : the base pipeline.

	  0.) Prep the output/cache dir
	  1.) Refresh the unified snapshot from the selected --manager
	  2.) Update the OpenAlex cache for every library paper

	Stops there. The stats + searches pass that produces
	output/missing.xlsx lives in ` prma missing ` ( see missing() below ) ,
	which calls this first.

	Returns ( snapshot , oa ) so ` prma missing ` can reuse both without
	re-fetching. The CLI dispatcher ignores the return value when ` prma `
	is run on its own."""
	from . import snapshot as snapshot_mod
	from ..openalex.openalex import OpenAlex

	# 0.) Prep
	cache_dir = args.output.joinpath( "cache" )
	cache_dir.mkdir( parents=True , exist_ok=True )

	# 1.) Get Snapshot
	snapshot = snapshot_mod.get_common( args )

	# 2.) Update OpenAlex Cache
	oa = OpenAlex( args )
	oa.update_cache( snapshot )

	return snapshot , oa


def missing( args ):
	"""prma missing : generate output/missing.xlsx.

	Depends on the base pipeline ( main() ) -- it runs that first to make
	sure the snapshot + OpenAlex cache are current , then :

	  3.) Compute stats over the cached OpenAlex data
	  4.) Run the searches over that index

	Both write into output/missing.xlsx ( the stats pass builds the
	missing / cited-by / authors sheets ; the searches pass appends one
	sheet per predicate )."""
	from ..utils import utils

	# Steps 0-2 : reuse the snapshot + OpenAlex client the base pipeline
	# already built rather than reconstructing them.
	snapshot , oa = main( args )

	# 3.) Stats
	oa_index = oa.Stats.compute( snapshot )

	# 4.) Searches
	searches = utils.load_searches( args.searches )
	oa.search( oa_index , searches )
