from PIL import Image
import pikepdf
import pypdfium2 as pdfium

def get_page_count( self , pdf_path ):
	try:
		with pikepdf.open( pdf_path ) as pdf:
			return int( pdf.Root.Pages.Count )
	except Exception as e:
		print( f"{pdf_path}: {e}" )
		return -1

def to_images( pdf_path , page_index=None , debug=False ):
	tmp = Path( tempfile.mkdtemp( prefix="pdf_pages_" ) )
	paths = []
	scale = DPI / 72.0 # 72 is pdf-spec for 1 inch
	with pdfium.PdfDocument( str( pdf_path ) ) as pdf:
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
		if debug:
			print( f"[pdfium] {Path(pdf_path).name}: {n} pages, rendering {indices}" )
		for idx in indices:
			page = pdf[ idx ]
			bitmap = page.render( scale=scale )
			pil_img = bitmap.to_pil()
			p = tmp / f"page_{idx + 1:04d}.png"
			pil_img.save( p )
			paths.append( str( p ) )
			page.close()
	return paths[ 0 ] if page_index is not None else paths

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