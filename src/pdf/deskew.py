import cv2
import math
from deskew import determine_skew

DESKEW_THRESHOLD = 0.5

def get_skew_angle( image ):
	gray = cv2.cvtColor( image , cv2.COLOR_BGR2GRAY )
	thresh = cv2.threshold(
		gray , 0 , 255 ,
		cv2.THRESH_BINARY | cv2.THRESH_OTSU
	)[ 1 ]
	angle = determine_skew( thresh )
	return angle

# https://github.com/sbrunner/deskew
def _deskew( image , angle , h , w ):
	# compute new bounds to avoid cropping
	center = ( w // 2 , h // 2 )
	angle_rad = math.radians( angle )
	new_w = abs( np.sin( angle_rad ) * h ) + abs( np.cos( angle_rad ) * w )
	new_h = abs( np.sin( angle_rad ) * w ) + abs( np.cos( angle_rad ) * h )

	M = cv2.getRotationMatrix2D( center , angle , 1.0 )

	# shift image to center in new canvas
	M[ 0 , 2 ] += ( new_w - w ) / 2
	M[ 1 , 2 ] += ( new_h - h ) / 2

	return cv2.warpAffine(
		image ,
		M ,
		( int( round( new_w ) ) , int( round( new_h ) ) ) ,
		flags=cv2.INTER_CUBIC ,
		borderMode=cv2.BORDER_REPLICATE
	)

def deskew( _img , threshold=DESKEW_THRESHOLD ):
	H , W = _img.shape[ :2 ]
	scew_angle = get_skew_angle( _img )
	if abs( scew_angle ) > threshold:
		# print( f"\t\tDetected skew angle: {scew_angle:.2f}°, deskewing : {pdf_path}" )
		deskewed = _deskew( _img , scew_angle , H , W )
		return deskewed
	else:
		return _img