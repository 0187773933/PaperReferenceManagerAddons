from .api import MendeleyAPI
from .local import MendeleyLocal
from ..utils import utils

class Mendeley:
	def __init__( self , args ):
		self.args = args
		self.API = MendeleyAPI( self.args )
		self.Local = MendeleyLocal( args )
		self.cache_path = self.args.output.joinpath( "cache" , "mendeley.jsonl" )

	def snapshot( self ):
		if self.args.mendeley_source == "local":
			self.snapshot = self.Local.snapshot()
		elif self.args.mendeley_source == "api":
			self.snapshot = self.API.snapshot()
		return self.snapshot

	def save_snapshot( self ):
		snap = self.snapshot()
		cache_dir = self.args.output.joinpath( "cache" )
		cache_dir.mkdir( parents=True , exist_ok=True )
		out_path = cache_dir.joinpath( "mendeley.snapshot" )
		utils.write_pickle( out_path , snap )
		print( f"Mendeley :: snapshot pickled -- {len(snap)} papers -> {out_path}" )
		return out_path

	def download( self ):
		if self.args.mendeley_source == "api":
			self.API.download_snapshot_pdfs()

	def _yolo_dir( self ):
		d = self.args.output.joinpath( "cache" , "yolo" , "mendeley" )
		d.mkdir( parents=True , exist_ok=True )
		return d

	def _yolo_path_for( self , pdf_path , doi ):
		"""{normalizedDOI}-{pdf_stem}.yolo.json -- DOI guards against PDF
		filename collisions across different papers ; pdf_stem disambiguates
		multiple PDFs that share a DOI."""
		prefix = utils.doi_to_filename( doi )
		return self._yolo_dir().joinpath( f"{prefix}-{pdf_path.stem}.yolo.json" )

	# limitations = only does first pdf , and only if it has a doi
	def yolo( self ):
		from tqdm import tqdm
		from ..pdf import pdf
		pdf_cache = self.args.output.joinpath( "pdfs" , "mendeley" )
		papers = self.API.snapshot()

		jobs = []
		skip_no_doi , skip_no_pdf , skip_missing , skip_done = 0 , 0 , 0 , 0
		for p_id , paper in papers.items():
			doi = utils.normalize_doi( paper.get( "doi" ) )
			if not doi:
				skip_no_doi += 1
				continue
			files = paper.get( "pdf_hosted" )
			if not files:
				skip_no_pdf += 1
				continue
			pdf_path = pdf_cache.joinpath( files[ 0 ][ "file_name" ] )
			if not pdf_path.exists():
				skip_missing += 1
				continue
			if self._yolo_path_for( pdf_path , doi ).exists():
				skip_done += 1
				continue
			jobs.append( ( pdf_path , doi ) )

		print(
			f"Mendeley :: YOLO -- {len(jobs)} pdfs to process "
			f"( skipped: no-doi={skip_no_doi} no-pdf={skip_no_pdf} "
			f"not-downloaded={skip_missing} already-done={skip_done} )"
		)

		outer = tqdm( jobs , desc="PDFs" , position=1 , leave=True , unit="pdf" )
		for pdf_path , doi in outer:
			outer.set_postfix_str( pdf_path.name[ :60 ] )
			yolo_path = self._yolo_path_for( pdf_path , doi )
			result = pdf.yolo( pdf_path , do_deskew=self.args.pdf_deskew )
			utils.write_json( yolo_path , result )

	# Depends on yolo() having run -- needs <pdf>.yolo.json next to each PDF.
	# Writes cropped figures/tables ( with captions stacked below ) to
	# output/images/mendeley/{normalizedDOI}-{kind}-{N}.png
	def images( self ):
		from tqdm import tqdm
		from ..pdf import images as images_mod
		pdf_cache  = self.args.output.joinpath( "pdfs" , "mendeley" )
		images_dir = self.args.output.joinpath( "images" , "mendeley" )
		images_dir.mkdir( parents=True , exist_ok=True )
		all_dir = images_dir.joinpath( "ALL" )
		all_dir.mkdir( parents=True , exist_ok=True )

		# Pre-scan ALL/ once to find already-done DOI prefixes.
		done_prefixes = set()
		for f in all_dir.iterdir():
			if f.suffix != ".png":
				continue
			for marker in ( "-figure-" , "-table-" ):
				idx = f.stem.rfind( marker )
				if idx > 0:
					done_prefixes.add( f.stem[ :idx ] )
					break

		papers = self.API.snapshot()
		jobs = []
		skip_no_doi , skip_no_pdf , skip_missing , skip_no_yolo , skip_done = 0 , 0 , 0 , 0 , 0
		for p_id , paper in papers.items():
			doi = utils.normalize_doi( paper.get( "doi" ) )
			if not doi:
				skip_no_doi += 1
				continue
			files = paper.get( "pdf_hosted" )
			if not files:
				skip_no_pdf += 1
				continue
			pdf_path = pdf_cache.joinpath( files[ 0 ][ "file_name" ] )
			if not pdf_path.exists():
				skip_missing += 1
				continue
			yolo_path = self._yolo_path_for( pdf_path , doi )
			if not yolo_path.exists():
				skip_no_yolo += 1
				continue
			prefix = utils.doi_to_filename( doi )
			if prefix in done_prefixes:
				skip_done += 1
				continue
			jobs.append( ( pdf_path , yolo_path , prefix ) )

		print(
			f"Mendeley :: IMAGES -- {len(jobs)} pdfs to process -> {images_dir} "
			f"( skipped: no-doi={skip_no_doi} no-pdf={skip_no_pdf} "
			f"not-downloaded={skip_missing} no-yolo={skip_no_yolo} already-done={skip_done} )"
		)

		include_tables = getattr( self.args , "images_include_tables" , False )
		montage        = not getattr( self.args , "images_no_montage" , False )
		size_name      = getattr( self.args , "images_montage_size" , "medium" )
		montage_scale  = images_mod.MONTAGE_SCALES.get( size_name , 1.0 )
		outer = tqdm( jobs , desc="PDFs" , position=1 , leave=True , unit="pdf" )
		total_imgs = 0
		for pdf_path , yolo_path , prefix in outer:
			outer.set_postfix_str( pdf_path.name[ :60 ] )
			n = images_mod.extract(
				pdf_path , yolo_path , images_dir , prefix ,
				include_tables=include_tables ,
				montage=montage ,
				montage_scale=montage_scale ,
			)
			total_imgs += n

		print( f"Mendeley :: IMAGES -- wrote {total_imgs} cropped images" )