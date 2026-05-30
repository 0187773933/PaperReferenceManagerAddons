import os
import logging
from tqdm import tqdm
import cv2
import numpy as np
from pathlib import Path
from .deskew import deskew

os.environ[ "TRANSFORMERS_VERBOSITY" ] = "critical"
logging.getLogger( "transformers" ).setLevel( logging.CRITICAL )
logging.getLogger( "transformers.utils.import_utils" ).setLevel( logging.CRITICAL )
logging.getLogger( "ultralytics" ).setLevel( logging.ERROR )
logging.getLogger( "doclayout_yolo" ).setLevel( logging.ERROR )
os.environ[ "SAFETENSORS_FAST_GPU" ] = "1"
os.environ[ "YOLO_VERBOSE" ] = "False"
os.environ[ "ULTRALYTICS_VERBOSE" ] = "False"

def get_bbox_area( bbox ):
	x1 , y1 , x2 , y2 = bbox
	return ( x2 - x1 ) * ( y2 - y1 )

_YOLO_MODEL = None
_YOLO_MODEL_PATH = None


def get_bbox_area( bbox ):
	x1 , y1 , x2 , y2 = bbox
	return ( x2 - x1 ) * ( y2 - y1 )

def load_yolo_model():
	global _YOLO_MODEL
	if _YOLO_MODEL is None:
		from doclayout_yolo import YOLOv10
		_YOLO_MODEL = YOLOv10( Path.cwd().joinpath( "src" , "models" , "doclayout_yolo_docstructbench_imgsz1024.pt" ) )
	return _YOLO_MODEL

def img( _img , confidence=0.1 , model_img_size=1024 , do_deskew=False ):
	model = load_yolo_model()
	if hasattr( _img , "mode" ):
		_img = np.array( _img.convert( "RGB" ) )
		_img = cv2.cvtColor( _img , cv2.COLOR_RGB2BGR )
	prepped = deskew( _img ) if do_deskew else _img
	detection = model.predict(
		prepped ,
		imgsz=model_img_size ,
		conf=confidence ,
	)

	page_result = []

	if len( detection ) == 0:
		return page_result

	detection = detection[ 0 ]

	if len( detection.boxes ) == 0:
		return page_result

	names = detection.names
	boxes = detection.boxes

	h , w = prepped.shape[ : 2 ]

	for i in range( len( boxes ) ):
		class_id = int( boxes.cls[ i ] )
		class_name = names[ class_id ]

		# normalized → pixel coords
		x1 , y1 , x2 , y2 = boxes.xyxyn[ i ].tolist()

		x1 = int( x1 * w )
		x2 = int( x2 * w )
		y1 = int( y1 * h )
		y2 = int( y2 * h )

		# clamp
		x1 = max( 0 , x1 )
		y1 = max( 0 , y1 )
		x2 = min( w , x2 )
		y2 = min( h , y2 )

		_bbox = [ x1 , y1 , x2 , y2 ]
		_bbox_area = get_bbox_area( _bbox )

		result = {
			"type": class_name ,
			"class_id": class_id ,
			"bbox": _bbox ,
			"bbox_area": _bbox_area ,
			"confidence": float( boxes.conf[ i ] ) ,
		}

		page_result.append( result )

	return page_result


def add_overlay( _img , yolo_result , alpha=0.25 ):
	vis = _img.copy()
	overlay = _img.copy()

	def get_color( class_id ):
		np.random.seed( class_id )
		return tuple( int( x ) for x in np.random.randint( 100 , 255 , 3 ) )

	for result in yolo_result:
		x1 , y1 , x2 , y2 = result[ "bbox" ]

		if x2 <= x1 or y2 <= y1:
			continue

		class_name = result[ "type" ]
		conf = result.get( "confidence" , 0.0 )

		class_id = result.get( "class_id" )

		if class_id is None:
			class_id = abs( hash( class_name ) ) % 10_000

		color = get_color( class_id )

		# filled transparent box
		cv2.rectangle(
			overlay ,
			( x1 , y1 ) ,
			( x2 , y2 ) ,
			color ,
			-1 ,
		)

		# sharp border
		cv2.rectangle(
			vis ,
			( x1 , y1 ) ,
			( x2 , y2 ) ,
			color ,
			2 ,
		)

		label = f"{class_name} {conf:.2f}"

		( label_w , label_h ) , _ = cv2.getTextSize(
			label ,
			cv2.FONT_HERSHEY_SIMPLEX ,
			0.5 ,
			1 ,
		)

		label_y1 = max( 0 , y1 - label_h - 4 )

		cv2.rectangle(
			vis ,
			( x1 , label_y1 ) ,
			( x1 + label_w , y1 ) ,
			color ,
			-1 ,
		)

		cv2.putText(
			vis ,
			label ,
			( x1 , max( label_h , y1 - 2 ) ) ,
			cv2.FONT_HERSHEY_SIMPLEX ,
			0.5 ,
			( 0 , 0 , 0 ) ,
			1 ,
			cv2.LINE_AA ,
		)

	vis = cv2.addWeighted(
		overlay ,
		alpha ,
		vis ,
		1 - alpha ,
		0 ,
	)

	return vis

def img_with_overlay( _img , confidence , model_img_size=1024 , alpha=0.25 ):
	yolo_result = img(
		_img ,
		confidence=confidence ,
		model_img_size=model_img_size ,
	)

	vis = add_yolo_overlay(
		_img ,
		yolo_result ,
		alpha=alpha ,
	)

	return yolo_result , vis