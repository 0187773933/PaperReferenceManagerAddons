def _run_zotero( args ):
	"""Snapshot Zotero -> pickle + push into output/cache/papers/.
	No-op if Zotero isn't configured ( no zotero.sqlite found )."""
	from ..zotero.zotero import Zotero
	try:
		z = Zotero( args )
	except FileNotFoundError as e:
		print( f"Snapshot :: Zotero not configured ( {e} ) ; skipping." )
		return
	# save_snapshot() already calls .snapshot() internally and pushes
	# to the unified DB ; don't call .snapshot() again from out here.
	z.save_snapshot()


def _run_mendeley( args ):
	"""Snapshot Mendeley -> pickle + push into output/cache/papers/.
	No-op if Mendeley isn't configured ( no API creds / local DB )."""
	from ..mendeley.mendeley import Mendeley
	try:
		m = Mendeley( args )
		m.save_snapshot()
	except Exception as e:
		print( f"Snapshot :: Mendeley not configured ( {e} ) ; skipping." )
		return


def _snapshot_dois( args ):
	"""Set of DOIs currently in the unified db/papers/ . Used to diff
	before / after a manager snapshot so we can tell exactly which
	papers were newly added on this run."""
	from ..db import papers
	return { doi for doi , _ in papers.iter_all( args ) }


def _auto_update_openalex( args , new_dois , view ):
	"""When a snapshot just added one or more new papers , automatically
	fetch their OpenAlex meta + references + cited-by , and pin the
	resulting wid back onto db/papers/{doi}.json as 'openalex_id'. This
	is what keeps the OpenAlex cache in lock-step with the library --
	any task that internally calls `get_common` ( yolo , ocr , md ,
	methods , summarize , ... ) automatically picks up the openalex
	data for newly added papers without the user having to also run
	the default ` prma ` ( full main task ).

	Scoped to JUST the new DOIs ( not the full snapshot ) so a steady-
	state run that added 1 paper makes 1 OpenAlex fetch , not 2000.
	Existing papers without a cache file are still backfilled by the
	default ` prma ` task ( which calls OpenAlex.update_cache on the
	full snapshot ).

	Failures here are LOGGED but never raised : a missing config.yaml ,
	an offline machine , or an OpenAlex outage must not break the
	user's ` prma snapshot ` invocation."""
	if not new_dois:
		return
	# Build a snapshot dict containing only the new DOIs so we don't
	# re-walk every paper on every snapshot. View is already DOI-keyed.
	sub = { d: view[ d ] for d in new_dois if d in view }
	if not sub:
		return
	print(
		f"Snapshot :: {len( sub )} new paper(s) -> auto-updating "
		f"OpenAlex cache for them ( meta + cited-by + references )"
	)
	try:
		from ..openalex.openalex import OpenAlex
		oa = OpenAlex( args )
	except FileNotFoundError as e:
		print(
			f"Snapshot :: OpenAlex auto-update skipped -- "
			f"config.yaml missing or unreadable ( {e} )"
		)
		return
	except Exception as e:
		print( f"Snapshot :: OpenAlex auto-update skipped ( {e} )" )
		return
	try:
		oa.update_cache( sub )
	except Exception as e:
		print( f"Snapshot :: OpenAlex auto-update failed ( {e} ) ; continuing" )


def get_common( args ):
	"""Run snapshots for the manager(s) selected by --manager and
	return a DOI-keyed view of the resulting unified DB. Supports :
	  --manager zotero    -> just Zotero ( DEFAULT )
	  --manager mendeley  -> just Mendeley
	  --manager all       -> both ( gracefully skips ones not configured )
	The legacy --zotero / --mendeley boolean flags still work as an
	override for backwards compat.

	Side effect : if the snapshot added any NEW DOIs to db/papers/ ,
	we automatically fetch their OpenAlex meta + references + cited-by
	and pin the wid back onto each new paper as paper[ 'openalex_id' ]
	-- see _auto_update_openalex. Errors are logged but never raised."""
	manager_name = ( args.manager or "zotero" ).lower()
	if args.mendeley:
		manager_name = "mendeley"
	elif args.zotero:
		manager_name = "zotero"

	# Diff "before vs after" so we know exactly which papers were added.
	before_dois = _snapshot_dois( args )

	if manager_name == "zotero":
		_run_zotero( args )
	elif manager_name == "mendeley":
		_run_mendeley( args )
	elif manager_name == "all":
		_run_zotero( args )
		_run_mendeley( args )
	elif manager_name in ( "endnote" , "paperpile" , "refworks" , "readcube" ):
		print( f"Snapshot :: {manager_name} -- not yet implemented , skipping." )
	else:
		print( f"Snapshot :: unknown manager '{manager_name}' ; expected zotero|mendeley|all." )

	# Always return the unified-DB view so downstream ( OpenAlex
	# update , stats , searches ) sees one merged set regardless of
	# which manager ran.
	from ..db import papers
	view = papers.snapshot_view( args )

	# Auto-update OpenAlex for the papers we just added.
	after_dois = set( view.keys() )
	new_dois   = after_dois - before_dois
	_auto_update_openalex( args , new_dois , view )

	return view


def titles_and_dois( args ):
	"""FAST path used by ` prma server ` : return ( titles_set ,
	dois_set ) for the manager(s) selected by --manager without
	going anywhere near output/cache/papers/ . Reads straight from
	the source ( Zotero SQLite copy / Mendeley jsonl cache ) and
	skips the per-paper upsert + save churn that get_common() does.

	Typical run is ~50-200 ms on a 2000-paper library vs ~10-30 s
	for get_common() ; the server cache calls this on each refresh.
	Honors --manager zotero | mendeley | all ( gracefully skips
	managers that aren't configured )."""
	manager_name = ( args.manager or "zotero" ).lower()
	if getattr( args , "mendeley" , False ):
		manager_name = "mendeley"
	elif getattr( args , "zotero" , False ):
		manager_name = "zotero"

	titles , dois = set() , set()

	def _run_zotero_fast():
		from ..zotero.zotero import Zotero
		try:
			z = Zotero( args )
		except FileNotFoundError as e:
			print( f"Snapshot :: Zotero not configured ( {e} ) ; skipping." )
			return
		t , d = z.take_titles_and_dois()
		titles.update( t )
		dois.update( d )

	def _run_mendeley_fast():
		from ..mendeley.mendeley import Mendeley
		try:
			m = Mendeley( args )
			t , d = m.take_titles_and_dois()
		except Exception as e:
			print( f"Snapshot :: Mendeley not configured ( {e} ) ; skipping." )
			return
		titles.update( t )
		dois.update( d )

	if manager_name == "zotero":
		_run_zotero_fast()
	elif manager_name == "mendeley":
		_run_mendeley_fast()
	elif manager_name == "all":
		_run_zotero_fast()
		_run_mendeley_fast()
	else:
		print( f"Snapshot :: unknown manager '{manager_name}' ; expected zotero|mendeley|all." )

	return titles , dois