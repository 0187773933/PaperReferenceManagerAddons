from tqdm import tqdm
from pathlib import Path
import tempfile
import cv2
from PIL import Image
import pikepdf
import pypdfium2 as pdfium

from . import yolo as YOLO

def get_page_count( self , pdf_path ):
	try:
		with pikepdf.open( pdf_path ) as pdf:
			return int( pdf.Root.Pages.Count )
	except Exception as e:
		print( f"{pdf_path}: {e}" )
		return -1

DPI = 200
def to_images( pdf_path , page_index=None , max_pages=50 , dpi=None , debug=False ):
	images = []
	render_dpi = dpi if dpi is not None else DPI
	scale = render_dpi / 72.0 # 72 is pdf-spec for 1 inch

	# Use explicit open/close instead of `with pdfium.PdfDocument(...)`
	# because pypdfium2 < some-version doesn't implement __enter__ /
	# __exit__ on PdfDocument , and we hit that on the installed wheel.
	pdf = pdfium.PdfDocument( str( pdf_path ) )
	try:
		pdf.init_forms()
		n = len( pdf )

		if page_index is None:
			indices = list( range( n ) )
		elif page_index == -1:
			indices = [ n - 1 ]
		else:
			if not 0 <= page_index < n:
				raise IndexError( f"page_index {page_index} out of range for {n}-page PDF" )
			indices = [ page_index ]

		if max_pages is not None:
			indices = indices[ :max_pages ]

		if debug:
			print( f"[pdfium] {Path(pdf_path).name}: {n} pages, rendering {indices}" )

		for idx in indices:
			page = pdf[ idx ]
			bitmap = page.render( scale=scale )

			pil_img = bitmap.to_pil().convert( "RGB" )

			images.append( pil_img )

			page.close()
	finally:
		try:
			pdf.close()
		except Exception:
			pass

	return images[ 0 ] if page_index is not None else images

def images_are_identical( pdf_images , threshold=0.01 , deskew=False , deskew_threshold=0.5 ):
	if len( pdfs ) < 2:
		return True
	page_counts = [ len( p ) for p in pdfs ]
	if len( set( page_counts ) ) != 1:
		return False

	def prep( img ):
		if isinstance( img , str ):
			img = Image.open( img )
		if hasattr( img , "convert" ):
			img = np.array( img )
		if img.ndim == 3:
			img = cv2.cvtColor( img , cv2.COLOR_BGR2GRAY )
		if deskew:
			angle = get_skew_angle( img )
			if abs( angle ) > deskew_threshold:
				h , w = img.shape[ :2 ]
				img = deskew( img , angle , h , w )
		img = cv2.normalize( img , None , 0 , 255 , cv2.NORM_MINMAX )
		img = cv2.GaussianBlur( img , ( 3 , 3 ) , 0 )
		return img

	base = pdfs[ 0 ]
	for other in pdfs[ 1: ]:
		for i in range( len( base ) ):
			a = prep( base[ i ] )
			b = prep( other[ i ] )
			h = min( a.shape[ 0 ] , b.shape[ 0 ] )
			w = min( a.shape[ 1]  , b.shape[ 1 ] )
			a = cv2.resize( a , ( w , h ) )
			b = cv2.resize( b , ( w , h ) )
			diff = cv2.absdiff( a , b )
			score = diff.mean() / 255.0
			if score > threshold:
				return False
	return True

YOLO_CONFIDENCE = 0.2
YOLO_MAX_PAGES = 50

def yolo( pdf_path , do_deskew=False ):
	from datetime import datetime , timezone
	images = to_images( pdf_path , max_pages=YOLO_MAX_PAGES )
	pages = []
	for image in tqdm( images , desc="Pages" , position=0 , leave=False , unit="pg" ):
		page_result = YOLO.img( image , YOLO_CONFIDENCE , do_deskew=do_deskew )
		pages.append( page_result )
	return {
		"meta": {
			"dpi":        DPI ,
			"confidence": YOLO_CONFIDENCE ,
			"do_deskew":  do_deskew ,
			"max_pages":  YOLO_MAX_PAGES ,
			"version":    1 ,
			"ran_at":     datetime.now( timezone.utc ).replace( microsecond=0 ).isoformat() ,
		} ,
		"pages": pages ,
	}