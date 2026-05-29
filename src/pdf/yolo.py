import os
import logging

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

class YOLO:
	def __init__( self , args ):
		self.args = args
		self.model_path = args.yolo_model_path
		self.model_img_size = 1024
		self.model_confidence = args.yolo_confidence
		self.model = None

	def load_model( self ):
		if not self.model:
			from doclayout_yolo import YOLOv10
			self.model = YOLOv10( self.model_path )

	def img( self , img , confidence=self.model_confidence ):
		self.load_model()
		detection = self.model.predict(
			img ,
			imgsz=self.model_img_size ,
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

		h , w = img.shape[ : 2 ]

		for i in tqdm( range( len( boxes ) ) , desc="\t\tBoxes", leave=False ):
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
				"bbox": _bbox ,
				"bbox_area": _bbox_area ,
				"confidence": float( boxes.conf[ i ] ) ,
			}

			page_result.append( result )
		return page_result

	def add_overlay( self , img , yolo_result , alpha=0.25 ):
		vis = img.copy()
		overlay = img.copy()

		def get_color( class_id ):
			np.random.seed( class_id )
			return tuple( int( x ) for x in np.random.randint( 100 , 255 , 3 ) )

		for result in yolo_result:
			x1 , y1 , x2 , y2 = result[ "bbox" ]

			if x2 <= x1 or y2 <= y1:
				continue

			class_name = result[ "type" ]
			conf = result.get( "confidence" , 0.0 )

			# Prefer saved class_id if present.
			# Fallback hashes class name into stable-ish color index.
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

	def img_with_overlay( self , img , confidence=None , alpha=0.25 ):
		yolo_result = self.img(
			img ,
			confidence=confidence ,
		)

		vis = self.add_overlay(
			img ,
			yolo_result ,
			alpha=alpha ,
		)

		return yolo_result , vis