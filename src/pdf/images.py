"""
Extract figures and tables ( with their captions ) from a PDF given its
precomputed YOLO doclayout detections in <pdf>.yolo.json.

Two-DPI strategy ( adapted from a working reference implementation ):
  - yolo.json bboxes are in pixel coords at pdf.DPI ( the DPI YOLO was
    run at ; currently 200 ).
  - We re-render each page at a HIGHER CROP_DPI so the crops are good
    quality , and scale every yolo bbox up by CROP_DPI / pdf.DPI before
    cropping.

Union-based grouping:
  - "Anchors" : figure , table.
  - "Attachments" : figure_caption , table_caption , table_footnote , title.
  - For each anchor , take the bounding-box UNION of itself plus every
    attachment that ( a ) horizontally overlaps it and ( b ) is within
    MAX_ATTACH_GAP px above-or-below. Crop that single rectangle.
  - This preserves the natural document layout ( caption stays where it
    visually belongs to the figure ) instead of cropping pieces and
    stacking them with mismatched widths.

NOTE: assumes the yolo.json was generated at the current pdf.DPI. If you
change pdf.DPI between running yolo and running this , the bboxes will
scale wrong and crops will be off. Regenerate yolo.json after a DPI change.
"""

import math

from PIL import Image

from ..utils import utils
from . import pdf as PDF

# DPI used for re-rendering pages just for cropping. Higher = nicer crops ,
# more memory / time. yolo was run at PDF.DPI ; we scale up by CROP_DPI/PDF.DPI.
CROP_DPI = 400

# Attachment matching tunables ( pixel coords at CROP_DPI ).
ATTACH_X_OVERLAP = 0.30   # min fraction of x-overlap ( vs narrower box )
MAX_ATTACH_GAP   = 150    # max vertical gap between anchor and attachment

# Per-bbox padding before union ( handles slightly-too-tight detections ).
PADDING = 10

# Vertical gap between figures in the per-paper montage ( px ).
MONTAGE_PAD = 30

# Human-readable montage size presets ( scale relative to the assembled grid ).
# E.g. "medium" = 50% of the natural montage size , whatever that happens to
# be for this paper. Adapts to papers with 2 figures or 20 figures alike.
MONTAGE_SCALES = {
	"original": 1.00 ,
	"high":     0.75 ,
	"medium":   0.50 ,
	"low":      0.25 ,
}

FIGURE_ANCHOR = "figure"
TABLE_ANCHOR  = "table"
ATTACHMENTS   = { "figure_caption" , "table_caption" , "table_footnote" , "title" }


def _scale_bbox( bbox , scale , padding , w , h ):
	"""Scale a low-DPI bbox to high-DPI , apply padding , clamp to page."""
	x1 , y1 , x2 , y2 = bbox
	x1 = int( x1 * scale ) - padding
	y1 = int( y1 * scale ) - padding
	x2 = int( x2 * scale ) + padding
	y2 = int( y2 * scale ) + padding
	return (
		max( 0 , x1 ) ,
		max( 0 , y1 ) ,
		min( w , x2 ) ,
		min( h , y2 ) ,
	)


def _x_overlap_frac( a , b ):
	"""Fraction of x-overlap relative to the narrower of the two boxes."""
	ax1 , _ , ax2 , _ = a
	bx1 , _ , bx2 , _ = b
	overlap = max( 0 , min( ax2 , bx2 ) - max( ax1 , bx1 ) )
	narrower = min( ax2 - ax1 , bx2 - bx1 )
	if narrower <= 0:
		return 0.0
	return overlap / narrower


def _montage_grid( crops , pad=MONTAGE_PAD , bg="white" , scale=1.0 ):
	"""Arrange crops in a near-square grid ( cols ~= sqrt(N) ).
	Each cell is the max crop width x max crop height ; each crop is
	centered within its cell. After assembly , the whole montage is
	downscaled by `scale` ( 1.0 = no downscale )."""
	if not crops:
		return None
	if len( crops ) == 1:
		out = crops[ 0 ]
	else:
		n = len( crops )
		cols = max( 1 , int( math.ceil( math.sqrt( n ) ) ) )
		rows = int( math.ceil( n / cols ) )
		cell_w = max( c.width  for c in crops )
		cell_h = max( c.height for c in crops )
		canvas_w = cols * cell_w + pad * ( cols - 1 )
		canvas_h = rows * cell_h + pad * ( rows - 1 )
		canvas = Image.new( "RGB" , ( canvas_w , canvas_h ) , bg )
		for i , c in enumerate( crops ):
			r , col = divmod( i , cols )
			cx = col * ( cell_w + pad ) + ( cell_w - c.width  ) // 2
			cy = r   * ( cell_h + pad ) + ( cell_h - c.height ) // 2
			canvas.paste( c , ( cx , cy ) )
		out = canvas
	if scale and scale < 1.0:
		new_w = max( 1 , int( out.width  * scale ) )
		new_h = max( 1 , int( out.height * scale ) )
		out = out.resize( ( new_w , new_h ) , Image.LANCZOS )
	return out


def _is_attached( anchor , other ):
	"""True if `other` is adjacent to `anchor` ( x-overlap and within gap )."""
	if _x_overlap_frac( anchor , other ) < ATTACH_X_OVERLAP:
		return False
	_ , ay1 , _ , ay2 = anchor
	_ , by1 , _ , by2 = other
	# below anchor ( captions , footnotes )
	if 0 <= by1 - ay2 <= MAX_ATTACH_GAP:
		return True
	# above anchor ( titles sometimes )
	if 0 <= ay1 - by2 <= MAX_ATTACH_GAP:
		return True
	return False


def extract(
	pdf_path , yolo_path , output_dir , name_prefix ,
	include_tables=False ,
	montage=True ,
	montage_scale=1.0 ,
):
	"""Read yolo_path , re-render pdf_path at CROP_DPI , crop each anchor
	union'd with its neighboring attachments. Writes:
	  - individual crops to output_dir/ALL/{name_prefix}-{kind}-{N}.png
	  - per-paper grid montage  output_dir/{name_prefix}-Figures.png   ( if montage )
	  - per-paper grid montage  output_dir/{name_prefix}-Tables.png    ( if montage AND
	    include_tables AND any tables were extracted )
	Per-kind counters ( figures and tables numbered independently from 1 ).
	Tables are skipped unless include_tables=True. montage_scale is the
	fraction of the assembled grid to keep ( 1.0 = original , 0.5 = half )."""
	yolo_data = utils.read_json( yolo_path )
	if not yolo_data:
		return 0

	# Accept both wrapped { meta , pages } and legacy bare-list formats.
	if isinstance( yolo_data , dict ):
		pages = yolo_data.get( "pages" , [] )
		yolo_dpi = yolo_data.get( "meta" , {} ).get( "dpi" , PDF.DPI )
	else:
		pages = yolo_data
		yolo_dpi = PDF.DPI
	if not pages:
		return 0

	anchors = { FIGURE_ANCHOR }
	if include_tables:
		anchors.add( TABLE_ANCHOR )

	images = PDF.to_images( pdf_path , max_pages=len( pages ) , dpi=CROP_DPI )
	scale = CROP_DPI / yolo_dpi
	output_dir.mkdir( parents=True , exist_ok=True )
	all_dir = output_dir.joinpath( "ALL" )
	all_dir.mkdir( parents=True , exist_ok=True )

	figure_crops , table_crops = [] , []
	n_figure , n_table , total = 0 , 0 , 0
	for page_idx , page_dets in enumerate( pages ):
		if page_idx >= len( images ):
			break
		page_image = images[ page_idx ]
		W , H = page_image.size

		# Scale all relevant detections up to high-res , with padding.
		scaled = []
		for d in page_dets:
			cls = d.get( "type" )
			if cls not in anchors and cls not in ATTACHMENTS:
				continue
			scaled.append( {
				"cls": cls ,
				"bbox": _scale_bbox( d[ "bbox" ] , scale , PADDING , W , H ) ,
			} )

		# For each anchor , compute union with its adjacent attachments.
		for d in scaled:
			if d[ "cls" ] not in anchors:
				continue
			x1 , y1 , x2 , y2 = d[ "bbox" ]
			for o in scaled:
				if o[ "cls" ] not in ATTACHMENTS:
					continue
				if not _is_attached( d[ "bbox" ] , o[ "bbox" ] ):
					continue
				ox1 , oy1 , ox2 , oy2 = o[ "bbox" ]
				x1 = min( x1 , ox1 )
				y1 = min( y1 , oy1 )
				x2 = max( x2 , ox2 )
				y2 = max( y2 , oy2 )

			if x2 <= x1 or y2 <= y1:
				continue

			crop = page_image.crop( ( x1 , y1 , x2 , y2 ) )
			kind = d[ "cls" ]    # "figure" or "table"
			if kind == "figure":
				n_figure += 1
				idx = n_figure
				figure_crops.append( crop )
			else:
				n_table += 1
				idx = n_table
				table_crops.append( crop )
			crop.save( all_dir / f"{name_prefix}-{kind}-{idx}.png" )
			total += 1

	# Per-paper montages live in output_dir ( one level above ALL/ ).
	if montage:
		if figure_crops:
			_montage_grid( figure_crops , scale=montage_scale ).save(
				output_dir / f"{name_prefix}-Figures.png"
			)
		if include_tables and table_crops:
			_montage_grid( table_crops , scale=montage_scale ).save(
				output_dir / f"{name_prefix}-Tables.png"
			)

	return total
