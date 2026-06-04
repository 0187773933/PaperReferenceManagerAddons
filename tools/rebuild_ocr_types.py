#!/usr/bin/env python3
"""
One-time consolidator : pull pre-schema legacy artifacts into the
unified output/cache/papers/{doi}.json store.

Two migrations , both keyed by normalized-DOI filename prefix :

  1) YOLO  ->  paper[ 'yolo' ]
       Source : output/cache/yolo/{zotero,mendeley}/{prefix}-*.yolo.json
       The whole file is dropped onto paper[ 'yolo' ] as-is .

  2) OCR   ->  paper[ 'ocr' ][ engine ]
       Source : output/text/{zotero,mendeley}/{prefix}.json
       The list of legacy { type , lines , page } block dicts gets
       stored verbatim under paper[ 'ocr' ][ engine ] ; default engine
       name is 'rapid' .

Both passes are idempotent : papers that ALREADY have the field are
skipped unless --force is passed. Use --delete-source to remove the
legacy file after a successful import ( safer to leave them on disk
the first run and verify ).

Usage :
  python tools/rebuild_ocr_types.py                      # both passes , both managers
  python tools/rebuild_ocr_types.py --dry-run            # report only
  python tools/rebuild_ocr_types.py --engine rapid       # name the ocr engine bucket
  python tools/rebuild_ocr_types.py --force              # overwrite even if paper already has it
  python tools/rebuild_ocr_types.py --delete-source      # remove legacy file after import
  python tools/rebuild_ocr_types.py --skip-yolo          # ocr-only pass
  python tools/rebuild_ocr_types.py --skip-ocr           # yolo-only pass
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make src.* importable when run as a standalone script from the repo root.
_REPO = Path( __file__ ).resolve().parent.parent
if str( _REPO ) not in sys.path:
	sys.path.insert( 0 , str( _REPO ) )

from src.db   import papers
from src.utils import utils

MANAGER_DIRS    = ( "zotero" , "mendeley" )
DEFAULT_ENGINE  = "rapid"


def _find_legacy_yolo( args , doi_prefix ):
	"""First matching {prefix}-*.yolo.json across both manager dirs , or None."""
	for manager in MANAGER_DIRS:
		d = args.output / "cache" / "yolo" / manager
		if not d.exists():
			continue
		for p in d.glob( f"{doi_prefix}-*.yolo.json" ):
			return p
	return None


def _find_legacy_ocr( args , doi_prefix ):
	"""First matching {prefix}.json under output/text/<manager> , or None."""
	for manager in MANAGER_DIRS:
		d = args.output / "text" / manager
		if not d.exists():
			continue
		p = d / f"{doi_prefix}.json"
		if p.exists():
			return p
	return None


def _import_yolo( args , doi , paper , prefix , force=False , dry_run=False , delete_source=False ):
	"""Returns ( status , path_or_None ) where status is one of
	'imported' | 'skipped-have-data' | 'no-legacy' | 'bad-shape'."""
	already = bool( ( paper.get( "yolo" ) or {} ).get( "pages" ) )
	if already and not force:
		return "skipped-have-data" , None
	legacy = _find_legacy_yolo( args , prefix )
	if legacy is None:
		return "no-legacy" , None
	try:
		data = utils.read_json( legacy )
	except Exception as e:
		print( f"  ! yolo {prefix} : read failed ( {e} )" )
		return "bad-shape" , legacy
	if not isinstance( data , dict ) or "pages" not in data:
		return "bad-shape" , legacy
	if not dry_run:
		paper[ "yolo" ] = data
	if delete_source and not dry_run:
		try:
			legacy.unlink()
		except Exception:
			pass
	return "imported" , legacy


def _import_ocr( args , doi , paper , prefix , engine , force=False , dry_run=False , delete_source=False ):
	already = bool( ( paper.get( "ocr" ) or {} ).get( engine ) )
	if already and not force:
		return "skipped-have-data" , None
	legacy = _find_legacy_ocr( args , prefix )
	if legacy is None:
		return "no-legacy" , None
	try:
		blocks = utils.read_json( legacy )
	except Exception as e:
		print( f"  ! ocr {prefix} : read failed ( {e} )" )
		return "bad-shape" , legacy
	if not isinstance( blocks , list ):
		return "bad-shape" , legacy
	if not dry_run:
		paper.setdefault( "ocr" , {} )[ engine ] = blocks
	if delete_source and not dry_run:
		try:
			legacy.unlink()
		except Exception:
			pass
	return "imported" , legacy


def main():
	ap = argparse.ArgumentParser(
		description=__doc__ ,
		formatter_class=argparse.RawDescriptionHelpFormatter ,
	)
	ap.add_argument( "--output" , type=Path , default=_REPO / "output" ,
		help="Output dir ( default: ./output )" )
	ap.add_argument( "--engine" , type=str , default=DEFAULT_ENGINE ,
		help=f"Name of the bucket to store legacy OCR blocks under "
		     f"( paper.ocr.<engine> ; default '{DEFAULT_ENGINE}' )" )
	ap.add_argument( "--force" , action="store_true" ,
		help="Overwrite paper.yolo / paper.ocr.<engine> even if already populated" )
	ap.add_argument( "--dry-run" , action="store_true" ,
		help="Report what would change without writing or deleting" )
	ap.add_argument( "--delete-source" , action="store_true" ,
		help="Delete legacy files after a successful import "
		     "( recommended only after a clean dry-run )" )
	ap.add_argument( "--skip-yolo" , action="store_true" , help="Skip the YOLO pass" )
	ap.add_argument( "--skip-ocr"  , action="store_true" , help="Skip the OCR pass" )
	args = ap.parse_args()

	n_total = papers.count( args )
	if n_total == 0:
		print( f"No papers in {args.output}/cache/papers/ . Run `prma snapshot` first." )
		return 0

	print(
		f"Consolidating legacy files into {args.output}/cache/papers/ "
		f"( {n_total} papers ){' [DRY RUN]' if args.dry_run else ''}"
	)

	y_in , y_skip , y_none , y_bad = 0 , 0 , 0 , 0
	o_in , o_skip , o_none , o_bad = 0 , 0 , 0 , 0

	for doi , paper in papers.iter_all( args ):
		prefix = utils.doi_to_filename( doi )
		if not prefix:
			continue
		changed = False

		if not args.skip_yolo:
			status , _path = _import_yolo(
				args , doi , paper , prefix ,
				force=args.force , dry_run=args.dry_run ,
				delete_source=args.delete_source ,
			)
			if   status == "imported"          : y_in += 1   ; changed = True
			elif status == "skipped-have-data" : y_skip += 1
			elif status == "no-legacy"         : y_none += 1
			elif status == "bad-shape"         : y_bad += 1

		if not args.skip_ocr:
			status , _path = _import_ocr(
				args , doi , paper , prefix , args.engine ,
				force=args.force , dry_run=args.dry_run ,
				delete_source=args.delete_source ,
			)
			if   status == "imported"          : o_in += 1   ; changed = True
			elif status == "skipped-have-data" : o_skip += 1
			elif status == "no-legacy"         : o_none += 1
			elif status == "bad-shape"         : o_bad += 1

		if changed and not args.dry_run:
			papers.save( args , paper )

	print( "\nDone." + ( "  ( dry-run -- nothing written )" if args.dry_run else "" ) )
	if not args.skip_yolo:
		print(
			f"  YOLO  : imported={y_in}  already-have={y_skip}  no-legacy-file={y_none}  bad-shape={y_bad}"
		)
	if not args.skip_ocr:
		print(
			f"  OCR   : imported={o_in}  already-have={o_skip}  no-legacy-file={o_none}  bad-shape={o_bad}  "
			f"( engine = '{args.engine}' )"
		)
	return 0


if __name__ == "__main__":
	raise SystemExit( main() )
