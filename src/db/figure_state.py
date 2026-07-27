"""
Curation state for the figure reports ( selected figures + skipped papers ).

TWO reports share this store , each with its OWN independent collection :

  "method-images"  /method-images -- the keyword-filtered model-design figures
                   ( src/tasks/method_images.py )
  "images"         /images -- every extracted figure , unfiltered
                   ( src/tasks/all_images.py )

Both are triage views : you click individual FIGURES to build a curated
selection , and SKIP whole PAPERS you've reviewed and don't want. Historically
this lived only in the browser's localStorage , which trapped the curation in
one browser and lost it on a switch. This persists it SERVER-SIDE instead , so
` prma server ` hands the same picks to every browser and they survive a report
regeneration. ( A report opened straight off disk over file:// still falls back
to localStorage in the page -- there's no server to talk to there. )

The collections never mix : picking a figure on /images does NOT pick it on
/method-images. The /images page does READ the method-images collection , to
mark ( and filter to ) figures you already picked over there , but it only ever
writes its own.

Per collection , two independent lists , both keyed by ids that are STABLE
across re-runs of the command that builds the report ( so a regenerated report
re-adopts them ) :

  selected : figure ids "<paper-key>#figure-<N>" -- ORDER IS PICK ORDER ( the
             report's "Last pick" walks it newest-last ) , so this is an ordered
             LIST , not a set.
  skipped  : paper keys "<paper-key>" -- order is irrelevant , stored as a sorted
             set for a stable , diff-able file.

Ids for figures / papers NOT in the current report are kept , never pruned ( a
keyword change can hide one ) , so narrowing the search never silently drops
curation -- exactly the contract the page's localStorage already relied on.

Persisted at output/cache/<collection>-state.json , so the historical
output/cache/method-images-state.json keeps both its name and its contents :
  { "selected": [ "10.../...#figure-3" , ... ] , "skipped": [ "10.../..." , ... ] }
"""

from ..utils import utils


# The report collections this store knows about. Each is one JSON file ; the
# names double as the page's data-mode and its /api/<name>/state route , so a
# third report is added here and nowhere else in this module.
METHOD_IMAGES = "method-images"
ALL_IMAGES    = "images"
COLLECTIONS   = ( METHOD_IMAGES , ALL_IMAGES )


def state_path( args , collection=METHOD_IMAGES ):
	"""Path to one collection's persisted curation state."""
	if collection not in COLLECTIONS:
		raise ValueError( f"unknown figure-state collection {collection!r}" )
	return args.output.joinpath( "cache" , f"{collection}-state.json" )


def load( args , collection=METHOD_IMAGES ):
	"""Load ( selected-list , skipped-list ). Empty lists if none / unreadable.
	`selected` keeps its stored order ( = pick order ) ; `skipped` comes back
	sorted. Both are de-duped defensively."""
	p = state_path( args , collection )
	if not p.exists():
		return [] , []
	try:
		data = utils.read_json( p ) or {}
	except Exception:
		return [] , []
	selected = _dedupe( s for s in ( data.get( "selected" ) or [] ) if isinstance( s , str ) )
	skipped  = sorted( { s for s in ( data.get( "skipped" ) or [] ) if isinstance( s , str ) } )
	return selected , skipped


def save( args , selected , skipped , collection=METHOD_IMAGES ):
	"""Persist. `selected` is written in the given ORDER ( pick order , de-duped
	first-seen ) ; `skipped` is sorted."""
	p = state_path( args , collection )
	p.parent.mkdir( parents=True , exist_ok=True )
	utils.write_json( p , {
		"selected": _dedupe( selected ) ,
		"skipped":  sorted( set( skipped ) ) ,
	} )


def _dedupe( items ):
	"""Drop repeats , preserving first-seen order."""
	seen , out = set() , []
	for s in items:
		if s in seen:
			continue
		seen.add( s )
		out.append( s )
	return out
