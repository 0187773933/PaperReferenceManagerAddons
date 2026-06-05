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


def get_common( args ):
	"""Run snapshots for the manager(s) selected by --manager and
	return a DOI-keyed view of the resulting unified DB. Supports :
	  --manager zotero    -> just Zotero ( DEFAULT )
	  --manager mendeley  -> just Mendeley
	  --manager all       -> both ( gracefully skips ones not configured )
	The legacy --zotero / --mendeley boolean flags still work as an
	override for backwards compat."""
	manager_name = ( args.manager or "zotero" ).lower()
	if args.mendeley:
		manager_name = "mendeley"
	elif args.zotero:
		manager_name = "zotero"

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
	return papers.snapshot_view( args )


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