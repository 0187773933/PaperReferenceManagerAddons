"""
Curation state for the method-images report ( selected figures + skipped papers ).

The /method-images report ( src/dashboard/method_images.html ) is a triage view :
you click individual FIGURES to build a curated selection , and SKIP whole PAPERS
you've reviewed and don't want. Historically both lived only in the browser's
localStorage , which trapped the curation in one browser and lost it on a switch.
This persists them SERVER-SIDE instead , so ` prma server ` hands the same picks
to every browser and they survive a report regeneration. ( A report opened
straight off disk over file:// still falls back to localStorage in the page --
there's no server to talk to there. )

Two independent collections , both keyed by ids that are STABLE across re-runs of
` prma method-images ` ( so a regenerated report re-adopts them ) :

  selected : figure ids "<paper-key>#figure-<N>" -- ORDER IS PICK ORDER ( the
             report's "Last pick" walks it newest-last ) , so this is an ordered
             LIST , not a set.
  skipped  : paper keys "<paper-key>" -- order is irrelevant , stored as a sorted
             set for a stable , diff-able file.

Ids for figures / papers NOT in the current report are kept , never pruned ( a
keyword change can hide one ) , so narrowing the search never silently drops
curation -- exactly the contract the page's localStorage already relied on.

Persisted at output/cache/method-images-state.json :
  { "selected": [ "10.../...#figure-3" , ... ] , "skipped": [ "10.../..." , ... ] }
"""

from ..utils import utils


def state_path( args ):
	"""Path to the persisted curation state."""
	return args.output.joinpath( "cache" , "method-images-state.json" )


def load( args ):
	"""Load ( selected-list , skipped-list ). Empty lists if none / unreadable.
	`selected` keeps its stored order ( = pick order ) ; `skipped` comes back
	sorted. Both are de-duped defensively."""
	p = state_path( args )
	if not p.exists():
		return [] , []
	try:
		data = utils.read_json( p ) or {}
	except Exception:
		return [] , []
	selected = _dedupe( s for s in ( data.get( "selected" ) or [] ) if isinstance( s , str ) )
	skipped  = sorted( { s for s in ( data.get( "skipped" ) or [] ) if isinstance( s , str ) } )
	return selected , skipped


def save( args , selected , skipped ):
	"""Persist. `selected` is written in the given ORDER ( pick order , de-duped
	first-seen ) ; `skipped` is sorted."""
	p = state_path( args )
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
