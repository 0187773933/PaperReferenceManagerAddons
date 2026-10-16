"""
tools/modal_ocr.py

Run the OCR stage ( src/pdf/ocr.ocr_paper -- the SAME code the local
pipeline runs ) on Modal , so a library-wide rebuild doesn't grind this
machine's CPU through every scanned page.

Existing papers keep their YOLO boxes : the remote call gets the PDF bytes
plus paper[ 'yolo' ] and hands back that dict with det[ 'ocr' ] pinned --
crop numbering ( and the /images picks keyed on it ) can't move.
yolo_ocr_gpu() is the full YOLO + OCR path , ONLY for papers with no YOLO
yet ; it isn't wired into anything , just timed by probe().

Two OCR flavours , so they can be measured against each other :
  ocr_cpu  -- CPU container , the same onnxruntime build as local -> text
              identical to a local run ;
  ocr_gpu  -- L4 , RapidOCR on onnxruntime-gpu ( CUDA ) .

Run from the venv ( the local side imports the repo's src/ ) :
  venv/bin/modal run tools/modal_ocr.py::probe --keys "10.1016/0010-0277(83)90026-4"
  venv/bin/modal run tools/modal_ocr.py::probe --keys "k1,k2,..." --flavour cpu
  venv/bin/modal run tools/modal_ocr.py::probe --keys "k1" --yolo

probe() prints per paper : pages , OCR pages , remote seconds , and whether
the remote text matches a local run , then the measured cost per paper /
per OCR page and a projection for the whole library.
"""

import copy
import os
import sys
import time
from pathlib import Path

import modal

_REPO = Path( __file__ ).resolve().parent.parent

# Modal list prices ( modal.com/pricing , 2026-10 ) , per second.
PRICE_CPU_CORE = 0.0000131
PRICE_MEM_GIB  = 0.00000222
PRICE_L4       = 0.000222
CPU_CORES , MEM_GIB = 2 , 4

# Library size , from the papers DB on 2026-10-16.
LIB_PAPERS , LIB_PAGES = 2369 , 41515

_PIP_COMMON = [
	"pypdfium2==4.30.0" , "pikepdf==10.7.2" , "numpy==1.26.4" , "pillow==10.4.0" ,
	"opencv-python-headless==4.10.0.84" , "wordninja==2.0.0" , "tqdm==4.67.3" ,
	"PyYAML==6.0.2" , "openpyxl==3.1.5" , "RapidFuzz==3.14.5" , "deskew==1.5.3" ,
	"rapidocr-onnxruntime==1.4.4" ,
]
_SRC_IGNORE = [ "**/__pycache__" , "**/*.pyc" ]

_base = (
	modal.Image.debian_slim( python_version="3.10" )
	.apt_install( "libgl1" , "libglib2.0-0" )
	.pip_install( *_PIP_COMMON , "onnxruntime==1.23.2" )
)
cpu_image = _base.add_local_dir( _REPO / "src" , "/root/src" , ignore=_SRC_IGNORE )
# onnxruntime-gpu replaces the CPU wheel rapidocr pulled in ; the [cuda,cudnn]
# extras bring the CUDA 12 / cuDNN 9 runtime as pip wheels. No torch here :
# torch 2.2 pins cuDNN 8 , which shadows the cuDNN 9 onnxruntime-gpu needs.
gpu_image = (
	_base
	.run_commands( "pip uninstall -y onnxruntime" )
	.pip_install( "onnxruntime-gpu[cuda,cudnn]==1.23.2" )
	.add_local_dir( _REPO / "src" , "/root/src" , ignore=_SRC_IGNORE )
)
# YOLO needs torch ( CUDA build ) ; its OCR pass stays on CPU onnxruntime.
yolo_image = (
	_base
	.pip_install( "doclayout_yolo==0.0.4" , "torch==2.2.2" , "torchvision==0.17.2" ,
	              "huggingface_hub==0.36.2" )
	.add_local_dir( _REPO / "src" , "/root/src" , ignore=_SRC_IGNORE )
)

app = modal.App( "prma-ocr" )


def _run_ocr( pdf_bytes , yolo , use_cuda , key="" ):
	"""Shared body : write the PDF , run ocr_paper( force ) , time it.
	Never raises : a failure comes back as ( None , { key , error } ) so
	the caller still knows WHICH paper failed."""
	try:
		return _run_ocr_inner( pdf_bytes , yolo , use_cuda , key )
	except Exception as e:
		return None , { "key": key , "error": f"{type( e ).__name__}: {e}" }


def _run_ocr_inner( pdf_bytes , yolo , use_cuda , key ):
	import tempfile
	os.chdir( "/root" )
	sys.path.insert( 0 , "/root" )
	from src.pdf import ocr as OCR
	info = { "cuda": False , "key": key }
	if use_cuda:
		import onnxruntime as ort
		try:
			ort.preload_dlls()
		except Exception:
			pass
		info[ "providers" ] = ort.get_available_providers()
		if OCR._RAPID_OCR is None:
			from rapidocr_onnxruntime import RapidOCR
			try:
				OCR._RAPID_OCR = RapidOCR( det_use_cuda=True , cls_use_cuda=True , rec_use_cuda=True )
				info[ "cuda" ] = "CUDAExecutionProvider" in info[ "providers" ]
			except Exception as e:
				info[ "cuda_error" ] = str( e )
	calls = [ 0 , 0.0 ]
	orig = OCR._ocr_page
	def counted( *a , **k ):
		t = time.time()
		try:
			return orig( *a , **k )
		finally:
			calls[ 0 ] += 1
			calls[ 1 ] += time.time() - t
	OCR._ocr_page = counted
	with tempfile.NamedTemporaryFile( suffix=".pdf" ) as f:
		f.write( pdf_bytes )
		f.flush()
		t0 = time.time()
		OCR.ocr_paper( f.name , yolo , force=True )
		info[ "seconds" ] = time.time() - t0
	OCR._ocr_page = orig
	info[ "ocr_pages" ] , info[ "ocr_seconds" ] = calls
	info[ "pages" ] = len( yolo.get( "pages" ) or [] )
	return yolo , info


@app.function( image=cpu_image , cpu=CPU_CORES , memory=MEM_GIB * 1024 , timeout=3600 )
def ocr_cpu( pdf_bytes: bytes , yolo: dict , key: str = "" ):
	return _run_ocr( pdf_bytes , yolo , use_cuda=False , key=key )


@app.function( image=gpu_image , gpu="L4" , cpu=CPU_CORES , memory=MEM_GIB * 1024 , timeout=3600 )
def ocr_gpu( pdf_bytes: bytes , yolo: dict , key: str = "" ):
	return _run_ocr( pdf_bytes , yolo , use_cuda=True , key=key )


@app.function( image=yolo_image , gpu="L4" , cpu=CPU_CORES , memory=MEM_GIB * 1024 , timeout=3600 )
def yolo_ocr_gpu( pdf_bytes: bytes ):
	"""Full path for a paper with NO yolo yet : YOLO ( torch on CUDA ) then OCR."""
	import tempfile
	os.chdir( "/root" )
	sys.path.insert( 0 , "/root" )
	from src.pdf import pdf as PDF
	with tempfile.NamedTemporaryFile( suffix=".pdf" ) as f:
		f.write( pdf_bytes )
		f.flush()
		t0 = time.time()
		yolo = PDF.yolo( f.name )
		yolo_s = time.time() - t0
	yolo , info = _run_ocr( pdf_bytes , yolo , use_cuda=False )
	info[ "yolo_seconds" ] = yolo_s
	return yolo , info


def _cost( info , gpu ):
	s = info[ "seconds" ] + info.get( "yolo_seconds" , 0.0 )
	c = s * ( CPU_CORES * PRICE_CPU_CORE + MEM_GIB * PRICE_MEM_GIB )
	return c + ( s * PRICE_L4 if gpu else 0.0 )


def _texts( yolo ):
	return [ ( d.get( "ocr" ) or {} ).get( "rapid" , "" )
	         for page in yolo.get( "pages" ) or [] for d in page if isinstance( d , dict ) ]


@app.local_entrypoint()
def probe( keys: str , flavour: str = "both" , yolo: bool = False , local_diff: bool = True ):
	sys.path.insert( 0 , str( _REPO ) )
	import argparse
	from src.db  import papers
	from src.pdf import ocr as OCR
	args = argparse.Namespace( output=_REPO / "output" )

	jobs = []
	for k in [ k.strip() for k in keys.split( "," ) if k.strip() ]:
		p = papers.load( args , k )
		if not p or not p.get( "pdf_path" ) or not ( p.get( "yolo" ) or {} ).get( "pages" ):
			print( f"skip {k} : no record / pdf / yolo" )
			continue
		jobs.append( ( papers.record_key( p ) , p ) )
	if not jobs:
		return

	if yolo:
		key , p = jobs[ 0 ]
		out , info = yolo_ocr_gpu.remote( Path( p[ "pdf_path" ] ).read_bytes() )
		print( f"yolo+ocr {key} : {info[ 'pages' ]} pages , yolo {info[ 'yolo_seconds' ]:.1f}s , "
		       f"ocr {info[ 'seconds' ]:.1f}s ( {info[ 'ocr_pages' ]} OCR pages , cuda={info[ 'cuda' ]} ) , "
		       f"${_cost( info , True ):.4f}" )
		return

	flavours = [ ( "cpu" , ocr_cpu , False ) , ( "gpu" , ocr_gpu , True ) ]
	if flavour != "both":
		flavours = [ f for f in flavours if f[ 0 ] == flavour ]

	local = {}
	if local_diff:
		print( f"local :: running ocr_paper on {len( jobs )} papers for the diff ..." )
		for key , p in jobs:
			y = copy.deepcopy( p[ "yolo" ] )
			OCR.ocr_paper( p[ "pdf_path" ] , y , force=True )
			local[ key ] = _texts( y )

	for name , fn , gpu in flavours:
		t0 = time.time()
		inputs = [ ( Path( p[ "pdf_path" ] ).read_bytes() , copy.deepcopy( p[ "yolo" ] ) , k ) for k , p in jobs ]
		results = list( fn.starmap( inputs ) )
		wall = time.time() - t0
		tot_s = tot_pages = tot_ocr = tot_ocr_s = cost = 0.0
		print( f"\n== {name} ( {len( jobs )} papers , wall {wall:.0f}s ) ==" )
		for ( key , _p ) , ( out , info ) in zip( jobs , results ):
			same , diffs = "n/a" , []
			if key in local and out is not None:
				rt = _texts( out )
				diffs = [ ( a , b ) for a , b in zip( local[ key ] , rt ) if a != b ]
				same = "same" if not diffs and len( rt ) == len( local[ key ] ) else f"DIFFERS on {len( diffs )} boxes"
			if out is None:
				print( f"  {key} : FAILED {info.get( 'error' )}" )
				continue
			c = _cost( info , gpu )
			cost += c
			tot_s += info[ "seconds" ] ; tot_pages += info[ "pages" ]
			tot_ocr += info[ "ocr_pages" ] ; tot_ocr_s += info[ "ocr_seconds" ]
			extra = f" cuda={info[ 'cuda' ]}" if gpu else ""
			if gpu and info.get( "cuda_error" ):
				extra += f" ( {info[ 'cuda_error' ][ :80 ]} )"
			print( f"  {key[ :48 ]:48} pages {info[ 'pages' ]:3}  ocr-pages {info[ 'ocr_pages' ]:3}  "
			       f"{info[ 'seconds' ]:6.1f}s  ${c:.4f}  text {same}{extra}" )
			for a , b in diffs[ :3 ]:
				print( f"      local : {a[ :110 ]!r}\n      remote: {b[ :110 ]!r}" )
		per_ocr_page = tot_ocr_s / tot_ocr if tot_ocr else 0.0
		base_per_page = ( tot_s - tot_ocr_s ) / tot_pages if tot_pages else 0.0
		print( f"  totals : {tot_s:.0f}s billed-ish , ${cost:.4f} ; "
		       f"{per_ocr_page:.2f}s per OCR page , {base_per_page:.3f}s per non-OCR page" )
		rate = CPU_CORES * PRICE_CPU_CORE + MEM_GIB * PRICE_MEM_GIB + ( PRICE_L4 if gpu else 0.0 )
		for share in ( 0.03 , 0.07 , 0.15 ):
			ocr_pages = LIB_PAGES * share
			secs = ocr_pages * per_ocr_page + LIB_PAGES * base_per_page
			print( f"  projection , all {LIB_PAPERS} papers with {share:.0%} of pages needing OCR : "
			       f"{secs / 3600:.1f} container-hours ~ ${secs * rate:.2f}" )
