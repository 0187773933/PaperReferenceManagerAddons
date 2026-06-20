"""
` prma status ` -- library completeness report.

Takes a snapshot ( so the unified papers/ DB is current ) , then walks every
paper and tallies how far each has made it through the pipeline :

  pdf -> openalex -> yolo -> ocr -> sections -> md / text / methods -> summaries

For each stage we report done / applicable / failed / missing , where
'applicable' follows the natural dependency chain ( you can't OCR a paper
that has no YOLO , can't render md without sections , ... ) so 'missing'
only ever counts papers that are actually ready for that step but haven't
had it done. 'failed' is pulled straight from the on-the-fly problems log
( src/db/papers.py ) so a paper that errored isn't double-counted as merely
'missing'.

The tally is printed AND written to output/cache/status.json so the
dashboard's /status page can display it without re-walking the library.
"""

import re

from datetime import datetime , timezone
from pathlib import Path

from ..db    import papers as papers_db
from ..utils import utils


_YEAR_RE = re.compile( r"(19|20)\d\d" )


def _utc_now_iso():
	return datetime.now( timezone.utc ).replace( microsecond=0 ).isoformat()


def _year( s ):
	"""First plausible 4-digit year in a free-form date string , or None."""
	m = _YEAR_RE.search( str( s or "" ) )
	return int( m.group( 0 ) ) if m else None


def _norm_added( s ):
	"""Normalize a timestamp to a sortable 'YYYY-MM-DD HH:MM:SS' string
	( collapse an ISO 'T' / millis / 'Z' ) , or None."""
	s = str( s or "" ).strip()
	if not s:
		return None
	return s.replace( "T" , " " )[ :19 ]


def _year_and_added( paper ):
	"""Pull ( publication year , date-added ) for a paper straight from what's
	already in the record. Year is parsed from each source's publication
	'date'. Date-added uses the source's 'updated_at' ( stamped when we first
	wrote that source ) , most-recent across sources , falling back to the
	record's top-level created_at."""
	year , added = None , None
	for sf in ( paper.get( "sources" ) or {} ).values():
		if year is None:
			year = _year( sf.get( "date" ) )
		da = _norm_added( sf.get( "updated_at" ) )
		if da and ( added is None or da > added ):
			added = da
	if added is None:
		added = _norm_added( paper.get( "created_at" ) )
	return year , added


def _has_ocr( ypages ):
	"""True if any YOLO detection on any page carries OCR text."""
	for page in ypages or []:
		for det in ( page or [] ):
			if det.get( "ocr" ):
				return True
	return False


def _scan_image_prefixes( all_dir ):
	"""Every doi_to_filename prefix that has at least one figure/table crop
	on disk -- mirrors src/tasks/images._scan_done_prefixes so a paper cropped
	before the paper[ 'images' ] marker existed still counts as done."""
	done = set()
	if not all_dir.exists():
		return done
	for f in all_dir.iterdir():
		if f.suffix != ".png":
			continue
		for marker in ( "-figure-" , "-table-" ):
			idx = f.stem.rfind( marker )
			if idx > 0:
				done.add( f.stem[ :idx ] )
				break
	return done


def compute( args ):
	"""Walk the unified papers/ DB + on-disk outputs and return the status
	tally dict. Pure reads ; caller is responsible for snapshotting first if
	a fresh picture is wanted ( see run )."""
	out        = args.output
	md_dir     = out.joinpath( "md" )
	text_dir   = out.joinpath( "text" )
	methods_dir= out.joinpath( "methods" )
	summ_dir   = out.joinpath( "summaries" )
	summ_sections = [ p for p in summ_dir.glob( "*" ) if p.is_dir() ] if summ_dir.exists() else []
	image_prefixes = _scan_image_prefixes( out.joinpath( "images" , "ALL" ) )

	# OpenAlex reference cache : how many referenced-work metas are on disk.
	refs_dir = out.joinpath( "cache" , "openalex" , "references" )
	refs_cached = sum( 1 for _ in refs_dir.glob( "*.json" ) ) if refs_dir.exists() else 0

	# Per-paper failure lookup from the on-the-fly problems log : kind -> set
	# of ids. These ids ARE the paper's primary key ( DOI / synthetic ) for the
	# per-paper kinds , so membership = "this paper failed that stage".
	problems  = papers_db.load_problems( args )
	by_kind   = { k: len( v or {} ) for k , v in problems.items() }
	prob_total= sum( by_kind.values() )
	def fail_ids( kind ): return set( ( problems.get( kind ) or {} ).keys() )
	f_oa , f_yolo , f_ocr = fail_ids( "openalex_meta" ) , fail_ids( "yolo" ) , fail_ids( "ocr" )

	# Stage order + label ( drives both the per-paper state map and the bars ).
	STAGES = [
		( "pdf"      , "PDF on disk"       ) ,
		( "openalex" , "OpenAlex metadata" ) ,
		( "yolo"     , "YOLO layout"       ) ,
		( "ocr"      , "OCR text"          ) ,
		( "sections" , "Reading order"     ) ,
		( "images"   , "Figure crops"      ) ,
		( "md"       , "Markdown"          ) ,
		( "text"     , "Plain text"        ) ,
		( "methods"  , "Methods"           ) ,
		( "summaries", "Summaries"         ) ,
	]

	def _state( applicable , done , failed ):
		if not applicable: return "na"      # not yet eligible ( upstream step pending )
		if done:           return "done"
		if failed:         return "failed"  # logged an error -> see /errors
		return "missing"                    # eligible but not done yet

	rows = []
	n_papers = n_doi = n_nodoi = 0
	for key , paper in papers_db.iter_all( args ):
		n_papers += 1
		doi = utils.normalize_doi( paper.get( "doi" ) )
		if doi: n_doi += 1
		else:   n_nodoi += 1

		pdf_path  = paper.get( "pdf_path" )
		pdf_ok    = bool( pdf_path and Path( pdf_path ).exists() )
		has_oa    = bool( paper.get( "openalex_id" ) )
		ypages    = ( paper.get( "yolo" ) or {} ).get( "pages" ) or []
		has_yolo  = bool( ypages )
		has_ocr   = has_yolo and _has_ocr( ypages )
		has_sect  = bool( paper.get( "sections" ) )
		prefix    = utils.doi_to_filename( key )
		has_img   = bool( paper.get( "images" ) ) or prefix in image_prefixes
		has_md    = md_dir.joinpath( f"{prefix}.md" ).exists()
		has_txt   = text_dir.joinpath( f"{prefix}.txt" ).exists()
		has_meth  = methods_dir.joinpath( f"{prefix}.txt" ).exists()
		has_summ  = any( s.joinpath( f"{prefix}.md" ).exists() for s in summ_sections )

		st = {
			"pdf":       _state( True       , pdf_ok   , False ) ,
			"openalex":  _state( bool( doi ), has_oa   , key in f_oa ) ,
			"yolo":      _state( pdf_ok     , has_yolo , key in f_yolo ) ,
			"ocr":       _state( has_yolo   , has_ocr  , key in f_ocr ) ,
			"sections":  _state( has_yolo   , has_sect , False ) ,
			"images":    _state( pdf_ok     , has_img  , False ) ,
			"md":        _state( has_sect   , has_md   , False ) ,
			"text":      _state( pdf_ok     , has_txt  , False ) ,
			"methods":   _state( pdf_ok     , has_meth , False ) ,
			"summaries": _state( has_md     , has_summ , False ) ,
		}
		year , added = _year_and_added( paper )
		rows.append( {
			"key":    key ,
			"title":  paper.get( "title" ) or "" ,
			"doi":    doi ,
			"year":   year ,
			"added":  added ,                          # date added to the manager
			"pdf":    pdf_path if pdf_ok else None ,   # local path ; served via /pdf?key=
			"states": st ,
		} )

	# Derive the aggregate bars from the per-paper states ( single source of
	# truth -> counts and drill-down can never disagree ).
	stages = []
	for k , label in STAGES:
		done = appl = failed = missing = 0
		for r in rows:
			s = r[ "states" ][ k ]
			if s == "na": continue
			appl += 1
			if   s == "done":    done    += 1
			elif s == "failed":  failed  += 1
			else:                missing += 1
		pct = round( 100.0 * done / appl , 1 ) if appl else 100.0
		stages.append( {
			"key": k , "label": label , "done": done ,
			"applicable": appl , "failed": failed ,
			"missing": missing , "pct": pct ,
		} )

	return {
		"generated_at": _utc_now_iso() ,
		"manager":      getattr( args , "manager" , "" ) ,
		"totals": {
			"papers":   n_papers ,
			"with_doi": n_doi ,
			"no_doi":   n_nodoi ,
			"with_pdf": sum( 1 for r in rows if r[ "states" ][ "pdf" ] == "done" ) ,
		} ,
		"stages":   stages ,
		# OpenAlex reference cache is a global pool ( one file per referenced
		# work , shared across papers ) , not a per-paper stage -- reported on
		# its own : how many ref metas are cached vs how many came back empty.
		"references": {
			"cached": refs_cached ,
			"failed": by_kind.get( "openalex_ref" , 0 ) ,
		} ,
		"papers":   rows ,
		"problems": { "total": prob_total , "by_kind": by_kind } ,
	}


def status_path( args ):
	return args.output.joinpath( "cache" , "status.json" )


def run( args ):
	# Take a snapshot first ( unless explicitly skipped ) so the tally reflects
	# the current library. get_common also backfills OpenAlex for new papers.
	if not getattr( args , "skip_snapshot" , False ):
		from . import snapshot
		snapshot.get_common( args )

	report = compute( args )

	p = status_path( args )
	p.parent.mkdir( parents=True , exist_ok=True )
	utils.write_json( p , report )

	# Pretty-print to the terminal.
	t = report[ "totals" ]
	print(
		f"\nLibrary status -- {t['papers']} papers "
		f"( {t['with_doi']} with DOI , {t['no_doi']} no-DOI , {t['with_pdf']} with PDF )"
	)
	print( f"  {'stage':<18}{'done':>7}{'/appl':>8}{'  missing':>10}{'  failed':>9}   progress" )
	for s in report[ "stages" ]:
		bar_n = int( round( s[ "pct" ] / 10.0 ) )
		bar   = "█" * bar_n + "·" * ( 10 - bar_n )
		print(
			f"  {s['label']:<18}{s['done']:>7}{('/'+str(s['applicable'])):>8}"
			f"{s['missing']:>10}{s['failed']:>9}   {bar} {s['pct']:.0f}%"
		)
	rf = report[ "references" ]
	print(
		f"  {'OpenAlex refs':<18}{rf['cached']:>7} cached"
		+ ( f" , {rf['failed']} empty/failed" if rf[ "failed" ] else "" )
		+ "   ( shared pool )"
	)
	pr = report[ "problems" ]
	if pr[ "total" ]:
		kinds = " , ".join( f"{k}={n}" for k , n in sorted( pr[ "by_kind" ].items() ) )
		print( f"  problems logged : {pr['total']} ( {kinds} ) -- see /errors" )
	print( f"\nwrote {p}" )
	return report
