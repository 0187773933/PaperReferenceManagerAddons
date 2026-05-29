import cv2
import math
from deskew import determine_skew

def get_skew_angle( image ):
	gray = cv2.cvtColor( image , cv2.COLOR_BGR2GRAY )
	thresh = cv2.threshold(
		gray , 0 , 255 ,
		cv2.THRESH_BINARY | cv2.THRESH_OTSU
	)[ 1 ]
	angle = determine_skew( thresh )
	return angle

# https://github.com/sbrunner/deskew
def deskew( image , angle , h , w ):
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