#!/usr/bin/env python3
"""
Delete the legacy top-level paper[ 'ocr' ] key from every record in
output/cache/papers/ .

Background : early runs of ` prma {zotero,mendeley} ocr ` produced
flat section-block arrays ( [ { type , lines , page } , ... ] ) and
the consolidator parked them at paper[ 'ocr' ][ engine ] . The new
pipeline pins per-bbox text under paper[ 'yolo' ][ 'pages' ][ p ][ d ][ 'ocr' ][ engine ]
instead , so the top-level bucket is dead weight. This script removes it.

By default the entire paper[ 'ocr' ] sub-object is dropped. Pass
--engine NAME to drop only one engine bucket and keep the others.

The per-detection paper[ 'yolo' ][ 'pages' ][ * ][ * ][ 'ocr' ] data is
NEVER touched -- only the top-level key.

Usage :
  python tools/delete-old-ocr.py                 # delete whole paper.ocr
  python tools/delete-old-ocr.py --engine rapid  # delete only paper.ocr.rapid
  python tools/delete-old-ocr.py --dry-run       # report only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tqdm import tqdm

_REPO = Path( __file__ ).resolve().parent.parent
if str( _REPO ) not in sys.path:
	sys.path.insert( 0 , str( _REPO ) )

from src.db   import papers
from src.utils import utils


def main():
	ap = argparse.ArgumentParser(
		description=__doc__ ,
		formatter_class=argparse.RawDescriptionHelpFormatter ,
	)
	ap.add_argument( "--output" , type=Path , default=_REPO / "output" ,
		help="Output dir ( default: ./output )" )
	ap.add_argument( "--engine" , type=str , default=None ,
		help="If set , only delete paper.ocr.<engine> ; keep other engine "
		     "buckets. Default is to delete the entire paper.ocr key." )
	ap.add_argument( "--dry-run" , action="store_true" ,
		help="Report what would change without writing." )
	args = ap.parse_args()

	n_total = papers.count( args )
	if n_total == 0:
		print( f"No papers in {args.output}/cache/papers/ ." )
		return 0

	print(
		f"Deleting "
		+ ( f"paper.ocr.{args.engine}" if args.engine else "paper.ocr" )
		+ f" across {n_total} papers"
		+ ( "  [DRY RUN]" if args.dry_run else "" )
	)

	n_touched , n_already_clean , n_engine_missing = 0 , 0 , 0
	for doi , paper in tqdm(
		list( papers.iter_all( args ) ) , desc="papers" , unit="paper" ,
	):
		ocr_bucket = paper.get( "ocr" )
		if not ocr_bucket:
			n_already_clean += 1
			continue
		if args.engine:
			if args.engine not in ocr_bucket:
				n_engine_missing += 1
				continue
			if args.dry_run:
				n_touched += 1
				continue
			del ocr_bucket[ args.engine ]
			# If the bucket is empty after removal , drop it entirely.
			if not ocr_bucket:
				paper.pop( "ocr" , None )
			else:
				paper[ "ocr" ] = ocr_bucket
		else:
			if args.dry_run:
				n_touched += 1
				continue
			paper.pop( "ocr" , None )

		papers.save( args , paper )
		n_touched += 1

	print( "\nDone." + ( "  ( dry-run -- nothing written )" if args.dry_run else "" ) )
	print( f"  touched         : {n_touched}" )
	print( f"  already clean   : {n_already_clean}" )
	if args.engine:
		print( f"  engine missing  : {n_engine_missing}  ( paper.ocr existed but had no '{args.engine}' bucket )" )
	return 0


if __name__ == "__main__":
	raise SystemExit( main() )
