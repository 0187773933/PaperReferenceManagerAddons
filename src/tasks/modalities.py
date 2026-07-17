"""
prma modalities : tag every paper with the MODALITIES the study itself used
( fMRI / EEG / ... -- the < --config >/methods.py vocabulary ) and stamp the
result on its record :

  paper[ 'modalities' ] = { 'at' , 'used' , 'inferred' , 'vocab' }

The tagging itself is code.paper_methods -- title + cached OpenAlex abstract +
isolated Methods section , never the full OCR ( a reference list names every
modality the paper merely CITES ) -- with code.paper_methods_inferred as the
last resort for papers we are BLIND about ( no abstract cached AND no Methods
section extracted ; those read the rendered md body and are marked inferred ).
See those two docstrings for why each evidence line is in / out.

Why a stamp at all : ` prma method-images ` used to run the tagger per paper
on EVERY report rebuild -- one OpenAlex JSON parse + a methods-section resolve
per paper , tens of seconds across the library , paid once per processed
batch. Tagging is per-paper work with per-paper inputs , so it lives in the
per-paper suite like every other stage , runs once , and the report just reads
the stamp.

Pipeline position : AFTER md ( the last _PDF_STAGES entry ). The inferred
fallback reads the rendered md body , which ` prma md ` writes only at the end
of the suite -- stamping earlier would leave every blind paper permanently
un-inferred on its first pass.

'used' == [] is an ANSWER , not a gap -- a review / theory / behavioural
paper uses no modality -- so as with paper[ 'code' ] it is the stamp's
PRESENCE , not its contents , that marks a paper done. 'vocab' fingerprints
the methods.py vocabulary that produced the stamp ( utils.methods.signature ) :
read() treats a stamp minted under a different vocabulary as missing , so a
config edit re-tags papers on their next pass -- the method-images rebuild
self-heals the papers it shows , ` prma modalities ` restamps the whole
library in one go , and neither needs --force.
"""

from tqdm import tqdm

from ..db    import papers as papers_db
from ..utils import methods as methods_vocab


STAMP_KEY = "modalities"


# ---------------------------------------------------------------------------
# Manager scoping ( same shape every PDF task uses )
# ---------------------------------------------------------------------------

def _resolve_managers( args ):
	name = ( getattr( args , "manager" , None ) or "zotero" ).lower()
	if getattr( args , "mendeley" , False ):
		name = "mendeley"
	elif getattr( args , "zotero" , False ):
		name = "zotero"
	if name == "all":
		return ( papers_db.SOURCE_ZOTERO , papers_db.SOURCE_MENDELEY )
	return ( name , )


def _paper_matches_managers( paper , managers ):
	if not managers:
		return True
	srcs = paper.get( "sources" ) or {}
	return any( m in srcs for m in managers )


# ---------------------------------------------------------------------------
# The stamp : read / compute / persist
# ---------------------------------------------------------------------------

def read( args , paper ):
	"""This paper's modality stamp , or None when it was never tagged -- or was
	tagged under a DIFFERENT methods.py vocabulary , which makes it stale and
	the caller should treat it exactly like missing ( re-stamp )."""
	stamp_rec = paper.get( STAMP_KEY )
	if not isinstance( stamp_rec , dict ):
		return None
	if stamp_rec.get( "vocab" ) != methods_vocab.signature( args ):
		return None
	return stamp_rec


def compute( args , key , paper ):
	"""( used , inferred ) for one paper , straight from the shared tagger in
	code.py ( deferred import -- code.py drags in the workbook machinery )."""
	from . import code as code_task
	used     = code_task.paper_methods( args , key , paper )
	inferred = False
	if not used:
		used     = code_task.paper_methods_inferred( args , key , paper )
		inferred = bool( used )
	return used , inferred


def stamp( args , key , paper ):
	"""Tag one paper and persist the stamp on its record. Returns the stamp."""
	used , inferred = compute( args , key , paper )
	rec = {
		"at":       papers_db._utc_now_iso() ,
		"used":     used ,
		"inferred": inferred ,
		"vocab":    methods_vocab.signature( args ) ,
	}
	paper[ STAMP_KEY ] = rec
	papers_db.save( args , paper )
	return rec


# ---------------------------------------------------------------------------
# The stage : stamp every in-scope paper without a current stamp
# ---------------------------------------------------------------------------

def run( args ):
	"""Stamp every in-scope paper whose stamp is missing or stale. Honors
	args.only_keys ( so process.run_suite / the --watch worker can scope it to
	one paper ) , --manager , and --force."""
	force         = getattr( args , "modalities_force" , False )
	managers      = _resolve_managers( args )
	manager_label = " + ".join( managers ) if managers else "all"

	# Plan : which papers still need tagging.
	jobs , skip_done , skip_other = [] , 0 , 0
	for key , paper in papers_db.iter_all( args ):
		if not _paper_matches_managers( paper , managers ):
			skip_other += 1
			continue
		if not force and read( args , paper ) is not None:
			skip_done += 1
			continue
		jobs.append( key )

	print(
		f"MODALITIES :: ({manager_label})  {len( jobs )} papers to tag "
		f"( skipped: already-done={skip_done} other-manager={skip_other} )"
	)

	n_used , n_inferred = 0 , 0
	for key in tqdm( jobs , desc="Papers" , unit="paper" ):
		paper = papers_db.load( args , key )
		if paper is None:
			continue
		try:
			rec = stamp( args , key , paper )
		except Exception as e:
			print( f"MODALITIES :: {key}: tagging failed ( {e} )" )
			continue
		if rec[ "used" ]:
			n_used     += 1
			n_inferred += 1 if rec[ "inferred" ] else 0

	print(
		f"MODALITIES :: tagged {len( jobs )} papers -- {n_used} use at least one "
		f"modality ( {n_inferred} of those inferred from full text )"
	)
