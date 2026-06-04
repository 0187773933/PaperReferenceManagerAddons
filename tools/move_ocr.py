#!/usr/bin/env python3
"""
Pin OCR text onto each individual YOLO detection by re-extracting
embedded text per bbox.

Result schema , inside each paper at output/cache/papers/*.json :
  paper[ 'yolo' ][ 'pages' ][ p ][ d ] = {
      'type' , 'class_id' , 'bbox' , 'bbox_area' , 'confidence' ,
      'ocr' : { 'rapid' : '...text in this bbox...' }     # NEW
  }

Why not use the existing paper[ 'ocr' ][ 'rapid' ] flat blocks as
the source ? Because the old ocr pipeline ( src/pdf/ocr.py ) aggregated
text across many detections into single section blocks ( 'Methods' ,
'Results' , each combining N detections worth of text ). There is no
clean back-mapping from a 'Methods' block to the specific YOLO bbox
that produced each sentence. Re-extracting directly from the PDF
text layer is fast , clean , and authoritative.

Performance : ~1-2 seconds per paper ( pypdfium2 text-layer call is
near-free ; no neural network involved ).

Failure modes :
  - Paper has no PDF on disk     -> skipped ( reported )
  - Paper has no yolo data       -> skipped ( reported )
  - PDF is a scan ( no text layer ) -> detections get empty text and
    are NOT pinned ; you'll see them in the 'bboxes-empty' count and
    will need to re-run the new ` prma ocr ` task for those papers.

Usage :
  python tools/move_ocr.py                    # pin everything , leave legacy blocks
  python tools/move_ocr.py --dry-run          # report only
  python tools/move_ocr.py --drop-legacy      # remove paper.ocr.<engine> after pinning
  python tools/move_ocr.py --force            # overwrite even if det.ocr.<engine> exists
  python tools/move_ocr.py --engine rapid     # key under det.ocr.<engine>  ( default 'rapid' )
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tqdm import tqdm
import pypdfium2 as pdfium

_REPO = Path( __file__ ).resolve().parent.parent
if str( _REPO ) not in sys.path:
	sys.path.insert( 0 , str( _REPO ) )

from src.db   import papers
from src.utils import utils
from src.pdf  import pdf as PDF
from src.pdf  import ocr as OCR

DEFAULT_ENGINE = "rapid"


def _extract_text_for_bbox( textpage , bbox , yolo_dpi , page_height_pt ):
	"""Reuse the OCR module's embedded-text helper , then normalize
	through its standard cleanup ( ligatures , soft hyphens , control
	chars , whitespace ) so what we pin is the same shape the new
	per-bbox pipeline would produce."""
	raw = OCR._embedded_text( textpage , bbox , yolo_dpi , page_height_pt )
	if not raw:
		return ""
	return OCR._normalize_text( raw )


def _process_paper( paper , engine , force=False , dry_run=False ):
	"""Pin embedded text onto every YOLO detection in `paper`.
	Returns ( n_pinned , n_empty , n_already_have , skipped_reason )."""
	yolo = paper.get( "yolo" ) or {}
	pages_yolo = yolo.get( "pages" ) or []
	if not pages_yolo:
		return 0 , 0 , 0 , "no-yolo"

	pdf_path = paper.get( "pdf_path" )
	if not pdf_path or not Path( pdf_path ).exists():
		return 0 , 0 , 0 , "no-pdf"

	yolo_dpi = ( yolo.get( "meta" ) or {} ).get( "dpi" , PDF.DPI )

	n_pinned , n_empty , n_already = 0 , 0 , 0

	pdf = pdfium.PdfDocument( str( pdf_path ) )
	try:
		n_pdf_pages = len( pdf )
		for page_idx , dets in enumerate( pages_yolo ):
			if page_idx >= n_pdf_pages:
				break
			page = pdf[ page_idx ]
			page_h_pt = page.get_height()
			textpage  = page.get_textpage()
			try:
				for det in dets:
					if not isinstance( det , dict ):
						continue
					existing = ( det.get( "ocr" ) or {} ).get( engine )
					if existing and not force:
						n_already += 1
						continue
					bbox = det.get( "bbox" )
					if not bbox:
						continue
					text = _extract_text_for_bbox(
						textpage , bbox , yolo_dpi , page_h_pt ,
					)
					if not text:
						n_empty += 1
						continue
					if not dry_run:
						det.setdefault( "ocr" , {} )[ engine ] = text
					n_pinned += 1
			finally:
				try: textpage.close()
				except Exception: pass
				try: page.close()
				except Exception: pass
	finally:
		try: pdf.close()
		except Exception: pass

	return n_pinned , n_empty , n_already , None


def main():
	ap = argparse.ArgumentParser(
		description=__doc__ ,
		formatter_class=argparse.RawDescriptionHelpFormatter ,
	)
	ap.add_argument( "--output" , type=Path , default=_REPO / "output" ,
		help="Output dir ( default: ./output )" )
	ap.add_argument( "--engine" , type=str , default=DEFAULT_ENGINE ,
		help=f"Pin text under det.ocr.<engine> ( default '{DEFAULT_ENGINE}' )" )
	ap.add_argument( "--force" , action="store_true" ,
		help="Overwrite even if det.ocr.<engine> already exists" )
	ap.add_argument( "--dry-run" , action="store_true" ,
		help="Report only , don't write" )
	ap.add_argument( "--drop-legacy" , action="store_true" ,
		help="After pinning , remove paper.ocr.<engine> ( the flat section "
		     "blocks from the old pipeline ) ; only do this after you've "
		     "verified the per-bbox text looks right" )
	args = ap.parse_args()

	n_total = papers.count( args )
	if n_total == 0:
		print( f"No papers in {args.output}/cache/papers/ . Run `prma snapshot` first." )
		return 0

	print(
		f"Pinning embedded text onto YOLO detections "
		f"-> {args.output}/cache/papers/ ( {n_total} papers )"
		+ ( "  [DRY RUN]" if args.dry_run else "" )
	)

	n_ok , n_no_pdf , n_no_yolo = 0 , 0 , 0
	total_pinned , total_empty , total_already = 0 , 0 , 0
	n_dropped_legacy = 0

	iter_all = tqdm( list( papers.iter_all( args ) ) , desc="papers" , unit="paper" )
	for doi , paper in iter_all:
		pinned , empty , already , reason = _process_paper(
			paper , args.engine ,
			force=args.force , dry_run=args.dry_run ,
		)
		total_pinned  += pinned
		total_empty   += empty
		total_already += already

		if reason == "no-yolo":
			n_no_yolo += 1
			continue
		if reason == "no-pdf":
			n_no_pdf += 1
			continue
		n_ok += 1

		# Drop legacy flat blocks if requested and we actually got data.
		if args.drop_legacy and not args.dry_run and ( pinned + already ) > 0:
			ocr_bucket = paper.get( "ocr" ) or {}
			if args.engine in ocr_bucket:
				del ocr_bucket[ args.engine ]
				n_dropped_legacy += 1
				if not ocr_bucket:
					paper.pop( "ocr" , None )
				else:
					paper[ "ocr" ] = ocr_bucket

		if not args.dry_run and ( pinned > 0 or args.drop_legacy ):
			papers.save( args , paper )

	print( f"\nDone {'( DRY RUN -- nothing written )' if args.dry_run else ''}" )
	print( f"  papers processed   : {n_ok}" )
	print( f"  papers no-yolo     : {n_no_yolo}  ( run `prma yolo` first )" )
	print( f"  papers no-pdf      : {n_no_pdf}" )
	print( f"" )
	print( f"  bboxes pinned      : {total_pinned}" )
	print( f"  bboxes empty       : {total_empty}  ( scanned PDFs / image-only regions ; rerun `prma ocr` to OCR them )" )
	print( f"  bboxes already-set : {total_already}  ( use --force to overwrite )" )
	if args.drop_legacy:
		print( f"  papers with legacy flat blocks dropped : {n_dropped_legacy}" )
	return 0


if __name__ == "__main__":
	raise SystemExit( main() )
