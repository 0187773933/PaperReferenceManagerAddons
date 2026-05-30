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

	# limitations = only does first pdf , and only if it has a doi
	def yolo( self ):
		from tqdm import tqdm
		from ..pdf import pdf
		pdf_cache = self.args.output.joinpath( "pdfs" , "mendeley" )
		yolo_cache = self.args.output.joinpath( "cache" , "yolo" )
		yolo_cache.mkdir( parents=True , exist_ok=True )
		papers = self.API.snapshot()

		jobs = []
		skip_no_doi , skip_no_pdf , skip_missing , skip_done = 0 , 0 , 0 , 0
		for p_id , paper in papers.items():
			if not utils.normalize_doi( paper.get( "doi" ) ):
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
			if pdf_path.with_suffix( ".yolo.json" ).exists():
				skip_done += 1
				continue
			jobs.append( pdf_path )

		print(
			f"Mendeley :: YOLO -- {len(jobs)} pdfs to process "
			f"( skipped: no-doi={skip_no_doi} no-pdf={skip_no_pdf} "
			f"not-downloaded={skip_missing} already-done={skip_done} )"
		)

		outer = tqdm( jobs , desc="PDFs" , position=1 , leave=True , unit="pdf" )
		for pdf_path in outer:
			outer.set_postfix_str( pdf_path.name[ :60 ] )
			yolo_path = pdf_path.with_suffix( ".yolo.json" )
			result = pdf.yolo( pdf_path , do_deskew=self.args.pdf_deskew )
			utils.write_json( yolo_path , result )