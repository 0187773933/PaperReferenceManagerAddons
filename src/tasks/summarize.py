"""
prma summarize {section} : feed each paper's section text to an LLM and
write one Markdown file per ( section , paper ) at
  args.output / summaries / {section} / { doi_to_filename }.md

Each file looks like :

  # {SectionDisplay} — {paper title}

  **DOI:** [10.xxxx/yyy](https://doi.org/10.xxxx/yyy)
  **Provider:** claude
  **Model:** claude-sonnet-4-5

  **Hashtags:** #tag1 #tag2 #tag3

  ## Summary

  ...the LLM's detailed structured overview...

Section resolution ( see src/pdf/section_text.py ) :
  1. If ` prma {section} ` has been run and
       args.output / {section} / {doi_to_filename}.txt
     exists , use that ;
  2. Otherwise slice the "## {SectionDisplay}" block out of
       args.output / md / {doi_to_filename}.md .
  3. Skip the paper if neither path turns up anything.

Behavior :
  - Default section is "all" -> iterate every summarizable section
    ( abstract , introduction , background , methods , results ,
    conclusions , future ) and write one folder per section.
  - Skip-if-exists is the default : a paper whose .md is already on
    disk under the section folder is left alone. Pass --force to
    re-summarize and overwrite.
  - Providers : 'claude' ( default ) , 'openai' , 'gemini' ; model is
    read from config.yaml gpts.<provider>.model , no CLI flag.

This task expects ` prma md ` to have been run first. It does NOT run
the upstream pipeline itself -- snapshot / yolo / ocr / preprocess / md
are heavy , and re-walking the LLM calls is not free either , so we
intentionally make the user opt in to the rendering step before
summarizing. Run order :
  prma snapshot      # if your library changed
  prma md            # render Markdown for every paper
  prma methods       # ( optional ) faster /cleaner methods source
  prma summarize     # this command
"""

from tqdm import tqdm

from ..db   import papers
from ..llm  import llm
from ..pdf  import section_text as ST
from ..utils import utils


# ---------------------------------------------------------------------------
# Manager filter helpers ( same shape as the other tasks )
# ---------------------------------------------------------------------------

def _resolve_managers( args ):
	name = ( getattr( args , "manager" , None ) or "zotero" ).lower()
	if getattr( args , "mendeley" , False ):
		name = "mendeley"
	elif getattr( args , "zotero" , False ):
		name = "zotero"
	if name == "all":
		return ( papers.SOURCE_ZOTERO , papers.SOURCE_MENDELEY )
	return ( name , )


def _paper_matches_managers( paper , managers ):
	if not managers:
		return True
	srcs = paper.get( "sources" ) or {}
	return any( m in srcs for m in managers )


# ---------------------------------------------------------------------------
# Markdown render
# ---------------------------------------------------------------------------

def _render_md(
	section_display , doi , title ,
	hashtags , summary ,
	provider , model ,
):
	"""Build the per-paper Markdown document. Title falls back to
	'(untitled)' so the H1 is never empty. Hashtags line is dropped
	when the LLM didn't emit any."""
	title    = ( title    or "(untitled)" ).strip()
	hashtags = ( hashtags or "" ).strip()
	summary  = ( summary  or "" ).strip()

	lines = [ f"# {section_display} — {title}" , "" ]
	if doi:
		lines.append( f"**DOI:** [{doi}](https://doi.org/{doi})" )
	lines.append( f"**Provider:** {provider}" )
	lines.append( f"**Model:** {model}" )
	lines.append( "" )
	if hashtags:
		lines.append( f"**Hashtags:** {hashtags}" )
		lines.append( "" )
	lines.append( "## Summary" )
	lines.append( "" )
	lines.append( summary )
	# Single trailing newline.
	return "\n".join( lines ).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Section selection
# ---------------------------------------------------------------------------

def _resolve_sections( args ):
	"""Build the list of ( key , display ) pairs we'll process. The user
	can pass a single section key or 'all'."""
	raw = ( getattr( args , "summarize_section" , None ) or "all" ).lower()
	all_sections = ST.list_sections()
	if raw == "all":
		return all_sections
	# Look up the requested section in the canonical list. Be forgiving
	# about display vs key ( 'Methods' / 'methods' / 'future_work' ).
	wanted = raw.replace( " " , "_" )
	for key , display in all_sections:
		if key == wanted or display.lower().replace( " " , "_" ) == wanted:
			return ( ( key , display ) , )
	valid = ", ".join( k for k , _ in all_sections )
	raise SystemExit(
		f"prma summarize : unknown section {raw!r} ; "
		f"valid choices : all , {valid}"
	)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config( args ):
	"""Read config.yaml if present so gpts.<provider>.{key,model} work.
	Missing file is fine -- the LLM module will fall back to env vars
	and built-in default models."""
	try:
		cfg_path = args.config.joinpath( "config.yaml" )
		if cfg_path.exists():
			return utils.read_yaml( cfg_path ) or {}
	except Exception as e:
		print( f"SUMMARIZE :: could not read config.yaml ( {e} ) ; "
		       f"falling back to env vars" )
	return {}


# ---------------------------------------------------------------------------
# One section pass
# ---------------------------------------------------------------------------

def _run_one_section(
	args , section_key , section_display ,
	provider , config ,
	managers , out_root , force ,
):
	# Resolve once so the planning print and the YAML-ish header in each
	# .md file show what's actually being sent ( config gpts.<provider>
	# .model > built-in default ).
	resolved_model = llm.resolve_model( provider , None , config )

	# Load the prompt template ONCE for this section ( same template
	# applies to every paper in this pass ). Lookup :
	#   1. {args.config}/llm-prompts/{section_key}.txt
	#   2. {args.config}/llm-prompts/all.txt
	#   3. built-in default
	prompt_pair = llm.load_prompt_template( args.config , section_key )

	# One folder per section : output/summaries/{section}/.
	section_dir = out_root.joinpath( section_key )
	section_dir.mkdir( parents=True , exist_ok=True )

	# Plan : every paper in the DB ( in the selected managers ) that has
	# text available for this section AND whose .md isn't on disk yet.
	jobs = []
	skip_other , skip_no_text , skip_done , skip_no_doi = 0 , 0 , 0 , 0
	for doi , paper in papers.iter_all( args ):
		if not _paper_matches_managers( paper , managers ):
			skip_other += 1
			continue
		prefix = utils.doi_to_filename( doi )
		if not prefix:
			skip_no_doi += 1
			continue
		out_path = section_dir.joinpath( f"{prefix}.md" )
		if not force and out_path.exists():
			skip_done += 1
			continue
		text = ST.resolve_section_text( args , doi , section_key )
		if not text:
			skip_no_text += 1
			continue
		jobs.append( ( doi , paper.get( "title" ) or "" , text , out_path ) )

	print(
		f"SUMMARIZE :: {section_key:<13} {len(jobs)} papers to summarize "
		f"( provider={provider} model={resolved_model} ) "
		f"-> {section_dir} "
		f"( skipped: no-text={skip_no_text} already-done={skip_done} "
		f"other-manager={skip_other} no-doi={skip_no_doi} )"
	)

	if jobs:
		bar = tqdm( jobs , desc=f"summarize {section_key}" , unit="paper" )
		n_ok , n_skip = 0 , 0
		for doi , title , text , out_path in bar:
			bar.set_postfix_str( ( doi or "" )[ :60 ] )
			result = llm.summarize(
				provider , None , section_key , section_display ,
				doi , title , text , config=config ,
				prompts=prompt_pair ,
			)
			if not result:
				n_skip += 1
				continue
			doc = _render_md(
				section_display , doi , title ,
				result.get( "hashtags" , "" ) ,
				result.get( "summary"  , "" ) ,
				provider , resolved_model ,
			)
			try:
				out_path.write_text( doc , encoding="utf-8" )
			except Exception as e:
				print( f"SUMMARIZE :: {doi}: write failed ( {e} )" )
				n_skip += 1
				continue
			n_ok += 1

		print(
			f"SUMMARIZE :: {section_key:<13} wrote {n_ok} new .md files "
			f"( failed: {n_skip} ) -> {section_dir}"
		)

	# A cross-paper xlsx rollup of this section is a separate command :
	#   prma rollup {section}
	# It scans args.output / summaries / {section} / *.md and writes
	# args.output / summaries / {section}.xlsx with one row per paper
	# and one column per discovered "**Subsection.**" header. Kept out
	# of this task so it can be re-run / debugged without paying for
	# another round of LLM calls.


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run( args ):
	sections = _resolve_sections( args )
	provider = ( getattr( args , "summarize_provider" , None ) or "claude" ).lower()
	force    = getattr( args , "summarize_force" , False )
	managers = _resolve_managers( args )
	config   = _load_config( args )

	out_root = args.output.joinpath( "summaries" )
	out_root.mkdir( parents=True , exist_ok=True )

	manager_label = " + ".join( managers ) if managers else "all"
	section_label = ", ".join( k for k , _ in sections )
	print(
		f"SUMMARIZE :: ({manager_label}) sections=[{section_label}] "
		f"provider={provider}"
	)

	for key , display in sections:
		_run_one_section(
			args , key , display ,
			provider , config ,
			managers , out_root , force ,
		)
