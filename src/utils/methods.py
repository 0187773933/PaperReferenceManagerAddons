"""
The MODALITY vocabulary -- fMRI / EEG / ECoG / fNIRS / MEG / PET / EMG -- that
` prma code ` tags repos + papers with ( the method columns in code.xlsx ) and
` prma method-images ` shows as pills under each paper title and as filter
chips. ONE list , loaded from < --config >/methods.py , so adding a modality is
a config edit rather than a code change and both commands pick it up.

The config file is plain Python , written in NATURAL casing :

    METHODS = (
        ( "fMRI" , ( "functional MRI" , "functional magnetic resonance" ) ) ,
        ( "EEG"  , ( "electroencephalogra" , ) ) ,
        "MEG" ,
    )

  label    what you write is BOTH the display label ( casing kept exactly ) and
           the term searched for -- matched as a WHOLE WORD , case-insensitively ,
           so "fMRI" finds fMRI / FMRI / fmri but 'PET' can't fire inside
           'dataset'. There is no second column to keep in sync.
  phrases  optional spelled-out forms , matched as substrings -- so a STEM like
           'electroencephalogra' covers -phy / -m / -phic in one go. Give a
           tuple , a bare string for one , or leave it off entirely ( then the
           whole entry can just be the label string ).

NOTHING here has to be pre-lowercased : scan() lowercases the haystack and the
loader lowercases the needles , so the config reads the way a human writes.

Labels match as whole words rather than by edit distance ON PURPOSE : 'fMRI'
and 'MRI' are one character apart and would score ~86% on the fuzzy scorer
` prma method-images ` uses for captions , so every structural-MRI paper would
come back tagged fMRI. Case-insensitive + whole-word is the loose-but-safe line.

Entries are cached per ( path , mtime ) , so editing methods.py takes effect on
the next run without restarting a long-lived ` prma server --watch ` .

If the file is missing or unparseable we fall back to DEFAULT ( the same
vocabulary , inlined below ) and say so ONCE -- a typo in a config file must
not take the whole pipeline down.
"""

import hashlib
import importlib.util
import re
from pathlib import Path


# The shipped vocabulary. Also the fallback when < --config >/methods.py is
# missing or broken -- keep it in step with config/methods.py.
DEFAULT = (
	( "fMRI"  , ( "functional MRI" , "functional magnetic resonance" ) ) ,
	( "EEG"   , ( "electroencephalogra" , ) ) ,
	( "ECoG"  , ( "electrocorticogra" , ) ) ,
	( "fNIRS" , ( "functional near-infrared" , "functional near infrared" ,
	              "near-infrared spectroscopy" ) ) ,
	( "MEG"   , ( "magnetoencephalogra" , ) ) ,
	( "PET"   , ( "positron emission" , ) ) ,
	( "EMG"   , ( "electromyography" , ) ) ,
)

_CACHE  = {}      # cache-key -> ( mtime , labels , compiled )
_WARNED = set()   # paths we've already complained about , so we say it once


def _normalize( entries ):
	"""Turn whatever the config declared into ( labels , compiled ) , doing the
	case-folding the human shouldn't have to. Raises ValueError on a shape we
	can't read , so load() can fall back and explain."""
	labels , compiled = [] , []
	for e in entries or ():
		if isinstance( e , str ):            # "MEG" -- label only , no phrases
			label , phrases = e , ()
		elif len( e ) == 1:                  # ( "MEG" , )
			label , phrases = e[ 0 ] , ()
		elif len( e ) == 2:                  # ( "EEG" , ( "electroencephalogra" , ) )
			label , phrases = e
		else:
			# Almost certainly the retired ( label , acronym , phrases ) shape.
			# Say so , rather than silently reading the acronym as a phrase.
			raise ValueError(
				f"entry {e!r} has {len( e )} parts -- write ( label , ( phrase , ... ) ) . "
				f"There is no acronym column any more : the label is what gets searched"
			)
		label = str( label ).strip()
		if not label:
			raise ValueError( f"entry {e!r} has an empty label" )
		if isinstance( phrases , str ):      # a bare string , not a 1-tuple
			phrases = ( phrases , )
		if label in labels:
			raise ValueError( f"duplicate label {label!r}" )
		labels.append( label )
		compiled.append( (
			label ,
			# The label IS the needle : whole word , case-insensitive , so 'fMRI' ,
			# 'fmri' and 'FMRI' all hit while 'PET' can't fire inside 'dataset'.
			re.compile( r"\b" + re.escape( label.lower() ) + r"\b" , re.IGNORECASE ) ,
			tuple( str( p ).strip().lower() for p in phrases if str( p ).strip() ) ,
		) )
	if not labels:
		raise ValueError( "METHODS is empty" )
	return tuple( labels ) , tuple( compiled )


def _config_path( args ):
	cfg = getattr( args , "config" , None )
	return Path( cfg ).joinpath( "methods.py" ) if cfg else None


def _load( args ):
	"""( labels , compiled ) for this run , from < --config >/methods.py with
	DEFAULT as the fallback. Cached on the file's mtime."""
	path = _config_path( args )
	if path and path.exists():
		try:
			mtime = path.stat().st_mtime
		except OSError:
			mtime = None
		key    = str( path )
		cached = _CACHE.get( key )
		if cached and cached[ 0 ] == mtime:
			return cached[ 1 ] , cached[ 2 ]
		try:
			spec = importlib.util.spec_from_file_location( "prma_config_methods" , path )
			mod  = importlib.util.module_from_spec( spec )
			spec.loader.exec_module( mod )
			labels , compiled = _normalize( getattr( mod , "METHODS" , None ) )
			_CACHE[ key ] = ( mtime , labels , compiled )
			return labels , compiled
		except Exception as e:
			if key not in _WARNED:
				print( f"METHODS   :: could not read {path} ( {e} ) ; using the built-in vocabulary" )
				_WARNED.add( key )

	cached = _CACHE.get( "<default>" )
	if not cached:
		labels , compiled = _normalize( DEFAULT )
		_CACHE[ "<default>" ] = ( None , labels , compiled )
		return labels , compiled
	return cached[ 1 ] , cached[ 2 ]


def labels( args ):
	"""Every modality label , in the order the config lists them ( which is the
	order pills / columns / chips are rendered in )."""
	return _load( args )[ 0 ]


def scan( args , text ):
	"""Labels whose acronym ( whole word ) or phrases appear in `text`. Lowercases
	`text` itself , so no caller has to remember to -- pass raw prose."""
	if not text:
		return []
	hay = text.lower()
	return [
		label for label , acro_re , phrases in _load( args )[ 1 ]
		if acro_re.search( hay ) or any( p in hay for p in phrases )
	]


def order( args , found ):
	"""Sort matched labels into the config's order , so 'fMRI , EEG' always reads
	the same regardless of which matched first."""
	return [ m for m in labels( args ) if m in found ]


def signature( args ):
	"""Short fingerprint of the loaded vocabulary ( labels + phrases , in order ).
	The ` modalities ` stage stamps this next to each paper's tags so a stamp
	minted under a DIFFERENT methods.py is recognizably stale : edit the config
	and papers re-tag on their next pass , no --force required."""
	parts = [
		label + "=" + "|".join( phrases )
		for label , _ , phrases in _load( args )[ 1 ]
	]
	return hashlib.sha1( ";".join( parts ).encode( "utf-8" ) ).hexdigest()[ :12 ]
