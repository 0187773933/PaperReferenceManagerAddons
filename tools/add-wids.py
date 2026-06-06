#!/usr/bin/env python3
"""
Backfill paper[ 'openalex_id' ] on every record in output/cache/papers/
that's missing it.

Background : the OpenAlex update_cache step was extended ( late 2026 ) to
pin the OpenAlex work id -- e.g. "W2911234567" -- on each db/papers/
record as a top-level key , so downstream tasks ( summarize , rollup ,
methods , crawl reports ) can correlate a paper to its OpenAlex meta
without re-base64-decoding the openalex cache filenames. Going forward
this happens automatically on every snapshot that detects new DOIs
( see src/tasks/snapshot._auto_update_openalex ) and on every default
` prma ` run.

For libraries whose openalex cache predates that change , the wid is
sitting in output/cache/openalex/{base64( doi )}.json but never made it
onto the paper record. This script walks every db paper without
openalex_id , reads its cached OpenAlex meta , extracts the wid , and
saves it back. Idempotent : papers that already have openalex_id are
skipped.

Usage :
  python tools/add-wids.py              # backfill in place
  python tools/add-wids.py --dry-run    # report what would change
  python tools/add-wids.py --force      # rewrite even already-set wids
                                          ( only useful if you suspect
                                            some are wrong )
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tqdm import tqdm

_REPO = Path( __file__ ).resolve().parent.parent
if str( _REPO ) not in sys.path:
	sys.path.insert( 0 , str( _REPO ) )

from src.db    import papers
from src.utils import utils


def _wid_from_openalex_cache( openalex_dir , doi ):
	"""Look up the cached OpenAlex meta for `doi` and return its WID
	( e.g. 'W2911234567' ) , or '' if no cache exists or the file is
	missing an 'id' . The cache is keyed by base64( normalized DOI ) ;
	see src/openalex/openalex.OpenAlex.update_cache."""
	if not doi:
		return ""
	b64 = utils.base64_encode( doi )
	if not b64:
		return ""
	fp = openalex_dir.joinpath( f"{b64}.json" )
	if not fp.exists():
		return ""
	try:
		data = utils.read_json( fp ) or {}
	except Exception:
		return ""
	oa_id = data.get( "id" ) or ""
	if not isinstance( oa_id , str ):
		return ""
	wid = oa_id.rsplit( "/" , 1 )[ -1 ]
	# Sanity : real WIDs start with 'W' and are otherwise digits.
	if not wid.startswith( "W" ):
		return ""
	return wid


def main():
	ap = argparse.ArgumentParser(
		description=__doc__ ,
		formatter_class=argparse.RawDescriptionHelpFormatter ,
	)
	ap.add_argument( "--output" , type=Path , default=_REPO / "output" ,
		help="Output dir ( default: ./output )" )
	ap.add_argument( "--dry-run" , action="store_true" ,
		help="Report what would change without writing." )
	ap.add_argument( "--force" , action="store_true" ,
		help="Rewrite openalex_id even on papers that already have one. "
		     "Use only if you suspect existing wids are wrong ; otherwise "
		     "the default ( skip already-set ) is what you want." )
	args = ap.parse_args()

	# Pretend to be an args object the way src.db.papers expects.
	openalex_dir = args.output.joinpath( "cache" , "openalex" )

	n_total = papers.count( args )
	if n_total == 0:
		print( f"No papers in {args.output}/cache/papers/ ." )
		return 0
	if not openalex_dir.exists():
		print( f"No OpenAlex cache at {openalex_dir} -- nothing to backfill from." )
		return 0

	print(
		f"Backfilling openalex_id across {n_total} papers"
		f" ( OpenAlex cache: {openalex_dir} )"
		+ ( "  [DRY RUN]" if args.dry_run else "" )
		+ ( "  [FORCE]"   if args.force   else "" )
	)

	n_set , n_already , n_no_cache , n_bad_cache , n_unchanged = 0 , 0 , 0 , 0 , 0
	for doi , paper in tqdm(
		list( papers.iter_all( args ) ) , desc="papers" , unit="paper" ,
	):
		existing = paper.get( "openalex_id" )
		if existing and not args.force:
			n_already += 1
			continue
		wid = _wid_from_openalex_cache( openalex_dir , doi )
		if not wid:
			# Distinguish "no cache file" from "cache exists but no usable id" .
			b64 = utils.base64_encode( doi )
			fp  = openalex_dir.joinpath( f"{b64}.json" ) if b64 else None
			if fp is None or not fp.exists():
				n_no_cache += 1
			else:
				n_bad_cache += 1
			continue
		if existing == wid:
			# Force mode landed on the same wid we already had.
			n_unchanged += 1
			continue
		if args.dry_run:
			n_set += 1
			continue
		paper[ "openalex_id" ] = wid
		try:
			papers.save( args , paper )
		except Exception as e:
			print( f"\nFailed to save {doi}: {e}" )
			continue
		n_set += 1

	print( "\nDone." + ( "  ( dry-run -- nothing written )" if args.dry_run else "" ) )
	print( f"  wid set            : {n_set}" )
	print( f"  already had wid    : {n_already}"   + ( "  ( skipped )" if not args.force else "  ( re-checked under --force )" ) )
	print( f"  unchanged ( force ): {n_unchanged}" )
	print( f"  no openalex cache  : {n_no_cache}   ( paper exists in db but has no openalex/<b64>.json )" )
	print( f"  bad openalex cache : {n_bad_cache}  ( file exists but no usable 'id' field )" )

	if n_no_cache:
		print(
			"\nTip : ` prma ` ( default task ) walks the FULL snapshot through "
			"OpenAlex.update_cache , fetching anything still uncached and then "
			"pinning the wid. Run it once to backfill the no-cache rows above."
		)
	return 0


if __name__ == "__main__":
	raise SystemExit( main() )
