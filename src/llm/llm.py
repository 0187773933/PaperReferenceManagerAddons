"""
src/llm/llm.py

Thin REST-only wrapper around the three chat-completion APIs we use for
` prma summarize ` :

  claude  -> https://api.anthropic.com/v1/messages
  openai  -> https://api.openai.com/v1/chat/completions
  gemini  -> https://generativelanguage.googleapis.com/v1beta/models/...

No SDK dependency : every provider is a plain `requests` POST. Keeping
the surface this small means a user with one API key can use that one
provider without having to install the others' SDKs , and adding a new
provider later is just another `_call_*` function.

Config schema ( config.yaml , under top-level `gpts:` ) :

  gpts:
    claude:
      key:   "sk-ant-..."
      model: "claude-opus-4-5"          # optional ; overrides DEFAULT_MODELS
    openai:
      key:   "sk-..."
      model: "gpt-4o"                   # optional
    gemini:
      key:   "AIza..."
      model: "gemini-2.0-flash"         # optional

API keys are resolved in this order :
  1. config.yaml  -> gpts.<provider>.key
  2. environment variable :
       claude  -> ANTHROPIC_API_KEY
       openai  -> OPENAI_API_KEY
       gemini  -> GEMINI_API_KEY ( fallback : GOOGLE_API_KEY )

Model is resolved in this order :
  1. config gpts.<provider>.model
  2. DEFAULT_MODELS[ provider ]
( The `prma summarize` CLI deliberately does NOT expose a --model flag --
  put the model in config.yaml so every run uses the same one and the
  CSV stays consistent across invocations. )

Top-level entry point :

  summarize( provider , model , section_key , section_display , doi ,
             title , text , config , timeout=120 ) -> dict

  Returns { "hashtags": "#a #b ..." , "summary": "..." } . On any error
  ( missing key , HTTP failure , parse failure ) returns {} and prints
  the reason so the caller can skip and move on.

Both halves of the dict come from a single LLM call that is prompted
to emit a strict two-section format ; the parser is forgiving and
falls back to putting the whole response in `summary` with empty
hashtags when the format isn't followed.
"""

import os
import re
import json
import time

import requests


# ---------------------------------------------------------------------------
# Per-provider defaults
# ---------------------------------------------------------------------------

DEFAULT_MODELS = {
	"claude" : "claude-sonnet-4-5"  ,
	"openai" : "gpt-4o"             ,
	"gemini" : "gemini-2.0-flash"   ,
}

# Per-provider INPUT character budget. Conservative so the prompt
# template + system prompt + completion all fit inside the model's
# real context window. We measure in characters ( ~3.5-4 chars / token
# for English prose ) instead of pulling in a tokenizer just for this.
# A book chapter pulled in via 'prma methods' / 'prma md' can easily
# blow past gpt-4o's 128k tokens ; we slice down to the first 60% +
# last 40% with a truncation marker in between so both the methods
# overview and its closing details survive.
INPUT_CHAR_BUDGETS = {
	"claude" :   600_000 ,   # claude-sonnet-4-5 : 200k token window
	"openai" :   320_000 ,   # gpt-4o : 128k token window
	"gemini" : 2_500_000 ,   # gemini-2.0-flash : 1M token window
}

# HTTP status codes that mean "back off and retry" rather than "give up".
_RETRYABLE_STATUS = ( 408 , 425 , 429 , 500 , 502 , 503 , 504 , 529 )
_MAX_RETRIES     = 4     # 5 attempts total ( original + 4 retries )
_BACKOFF_BASE_S  = 5.0   # exponential : 5 , 10 , 20 , 40 ...
_BACKOFF_CAP_S   = 60.0  # never sleep more than a minute

# Same key-name resolution as Anthropic / OpenAI / Google's own CLIs use.
ENV_KEYS = {
	"claude" : ( "ANTHROPIC_API_KEY" ,                    ) ,
	"openai" : ( "OPENAI_API_KEY"    ,                    ) ,
	"gemini" : ( "GEMINI_API_KEY"    , "GOOGLE_API_KEY"   ) ,
}

PROVIDERS = tuple( DEFAULT_MODELS.keys() )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
	"You are a scientific literature summarizer. You are given ONE section "
	"of an academic paper and must produce a STRUCTURED , DETAILED OVERVIEW "
	"of that section. Do not editorialize ; do not introduce information "
	"that is not in the source ; do not omit specifics like sample sizes , "
	"techniques , numerical values , statistical tests , datasets , study "
	"design , patient counts , inclusion / exclusion criteria , or named "
	"variables. A summary that drops these details is a failure mode -- "
	"prefer a longer , richer output over a concise one. If the source is "
	"empty , garbled , or clearly truncated , say so explicitly."
)


_USER_TEMPLATE = """\
Paper title: {title}
DOI: {doi}
Section: {section_display}

Produce EXACTLY two blocks in this exact format , in this order , with the
literal "HASHTAGS:" and "SUMMARY:" labels :

HASHTAGS: <3 to 8 single-token hashtags , each starting with '#' , CamelCase
or lowercased , no spaces inside a tag , categorizing what THIS section is
about ( methodology types , disease , modality , statistical framework ,
study design , key technique , etc. ) >

SUMMARY:
<a detailed structured overview of the section. Use short paragraphs or
bullet points. Preserve specific details : sample sizes , cohorts , inclusion
criteria , equipment , statistical tests , thresholds , imaging modalities ,
named scales / scores , primary and secondary endpoints , whatever is
actually written in the source. Do not condense away the specifics. Do
not invent or extrapolate. >

--- BEGIN {section_display} SECTION ---
{text}
--- END {section_display} SECTION ---
"""


def _build_prompt( section_display , doi , title , text ):
	return _USER_TEMPLATE.format(
		section_display = section_display ,
		doi             = doi   or "(unknown)" ,
		title           = title or "(untitled)" ,
		text            = text ,
	)


# ---------------------------------------------------------------------------
# Input truncation
# ---------------------------------------------------------------------------

# Approximate per-template overhead ( system prompt + section header
# wrapper + completion budget ) we reserve OUT of the provider's input
# budget. ~8k chars covers our system prompt + the user template
# scaffolding + a generous max_tokens completion.
_TEMPLATE_OVERHEAD_CHARS = 8_000


def _truncate_for_provider( text , provider ):
	"""Slice `text` to fit the provider's input budget. Preserves the
	first 60% and last 40% with a clearly marked drop in between so the
	LLM can still see how the section opens AND closes -- a tail-only
	truncation loses the opening paragraphs ( definitions , cohort
	description ) and a head-only truncation loses results / final
	subsection details. Returns ( truncated_text , was_truncated )."""
	if not text:
		return text , False
	budget = INPUT_CHAR_BUDGETS.get( provider , 300_000 ) - _TEMPLATE_OVERHEAD_CHARS
	if budget <= 0 or len( text ) <= budget:
		return text , False
	head_chars = int( budget * 0.60 )
	tail_chars = budget - head_chars
	dropped    = len( text ) - budget
	marker = (
		f"\n\n[ ... {dropped} characters truncated to fit {provider} "
		f"context window ... ]\n\n"
	)
	return text[ :head_chars ] + marker + text[ -tail_chars : ] , True


# ---------------------------------------------------------------------------
# Retry / backoff
# ---------------------------------------------------------------------------

_RETRY_AFTER_BODY_RE = re.compile(
	r"try\s+again\s+in\s+([\d.]+)\s*s"   ,
	re.IGNORECASE                         ,
)


def _parse_retry_after( response ):
	"""Best-effort 'how long to sleep' read from a 429 / 503 response.
	Tries the Retry-After header first ( seconds ; HTTP-date not
	supported -- providers we hit don't use it ) , then scrapes
	'try again in N.Ns' out of the body. Returns None if nothing
	usable is found ( caller falls back to exponential backoff )."""
	# 1. Retry-After header.
	hdr = response.headers.get( "Retry-After" ) if response is not None else None
	if hdr:
		try:
			return max( 0.0 , float( hdr ) )
		except ( TypeError , ValueError ):
			pass
	# 2. Free-form body hint.
	try:
		body = response.text if response is not None else ""
	except Exception:
		body = ""
	m = _RETRY_AFTER_BODY_RE.search( body or "" )
	if m:
		try:
			return max( 0.0 , float( m.group( 1 ) ) )
		except ValueError:
			pass
	return None


def _post_with_retry( url , *, headers , json_body , timeout , label ):
	"""POST with exponential backoff on retryable HTTP statuses. `label`
	is a short string ( provider name ) used only for log lines. Returns
	the final Response. Raises the final HTTPError if all retries are
	exhausted ( caller catches it )."""
	last_exc = None
	for attempt in range( _MAX_RETRIES + 1 ):
		try:
			r = requests.post(
				url , headers=headers , json=json_body , timeout=timeout ,
			)
		except requests.RequestException as e:
			# Network-level failure ( connection reset , DNS , etc. ).
			# Treat like a retryable status : sleep + retry.
			last_exc = e
			if attempt >= _MAX_RETRIES:
				raise
			wait = min( _BACKOFF_CAP_S , _BACKOFF_BASE_S * ( 2 ** attempt ) )
			print( f"LLM :: {label} network error ( {e} ) ; "
			       f"sleeping {wait:.1f}s ( attempt {attempt + 1}/{_MAX_RETRIES + 1} )" )
			time.sleep( wait )
			continue

		if r.status_code not in _RETRYABLE_STATUS:
			# Either success ( 2xx ) or a real client error ( 4xx other
			# than 408/425/429 ) -- let the caller .raise_for_status().
			return r
		if attempt >= _MAX_RETRIES:
			# Out of retries -- return the bad response so the caller's
			# raise_for_status() surfaces a meaningful error.
			return r
		hinted = _parse_retry_after( r )
		# Add a small jitter pad to the server's hint so we don't all
		# wake up at exactly the same instant the limit resets.
		wait = ( hinted + 1.0 ) if hinted is not None else min(
			_BACKOFF_CAP_S , _BACKOFF_BASE_S * ( 2 ** attempt )
		)
		print(
			f"LLM :: {label} throttled ( HTTP {r.status_code} ) ; "
			f"sleeping {wait:.1f}s ( attempt {attempt + 1}/{_MAX_RETRIES + 1} )"
		)
		time.sleep( wait )
	# Shouldn't fall out , but be defensive.
	if last_exc is not None:
		raise last_exc
	return r


# ---------------------------------------------------------------------------
# API-key resolution
# ---------------------------------------------------------------------------

def _provider_config( config , provider ):
	"""Return the dict at `gpts.<provider>` in config.yaml , or {}."""
	return ( ( config or {} ).get( "gpts" , {} ) or {} ).get( provider , {} ) or {}


def _resolve_api_key( provider , config ):
	"""config.yaml beats env vars. Returns None if neither is set."""
	cfg = _provider_config( config , provider )
	k = cfg.get( "key" )
	if k:
		return k
	for var in ENV_KEYS.get( provider , () ):
		v = os.environ.get( var )
		if v:
			return v
	return None


def resolve_model( provider , model_override , config ):
	"""Pick the model name to send :
	  CLI --model  >  gpts.<provider>.model  >  DEFAULT_MODELS[ provider ]"""
	if model_override:
		return model_override
	cfg = _provider_config( config , provider )
	m = cfg.get( "model" )
	if m:
		return m
	return DEFAULT_MODELS.get( provider )


# ---------------------------------------------------------------------------
# Per-provider HTTP calls
# ---------------------------------------------------------------------------

def _call_claude( api_key , model , system_prompt , user_prompt , timeout ):
	r = _post_with_retry(
		"https://api.anthropic.com/v1/messages" ,
		headers = {
			"x-api-key"         : api_key ,
			"anthropic-version" : "2023-06-01" ,
			"content-type"      : "application/json" ,
		} ,
		json_body = {
			"model"      : model ,
			"max_tokens" : 4096  ,
			"system"     : system_prompt ,
			"messages"   : [ { "role": "user" , "content": user_prompt } ] ,
		} ,
		timeout = timeout ,
		label   = "claude" ,
	)
	r.raise_for_status()
	data = r.json()
	# data[ 'content' ] is a list of content blocks ; concatenate text blocks.
	parts = []
	for block in data.get( "content" , [] ) or []:
		if block.get( "type" ) == "text":
			parts.append( block.get( "text" , "" ) )
	return "".join( parts ).strip()


def _call_openai( api_key , model , system_prompt , user_prompt , timeout ):
	r = _post_with_retry(
		"https://api.openai.com/v1/chat/completions" ,
		headers = {
			"Authorization" : f"Bearer {api_key}" ,
			"Content-Type"  : "application/json" ,
		} ,
		json_body = {
			"model"    : model ,
			"messages" : [
				{ "role": "system" , "content": system_prompt } ,
				{ "role": "user"   , "content": user_prompt   } ,
			] ,
			"temperature" : 0.2 ,
		} ,
		timeout = timeout ,
		label   = "openai" ,
	)
	r.raise_for_status()
	data = r.json()
	choices = data.get( "choices" ) or []
	if not choices:
		return ""
	return ( choices[ 0 ].get( "message" , {} ).get( "content" ) or "" ).strip()


def _call_gemini( api_key , model , system_prompt , user_prompt , timeout ):
	# Gemini's REST endpoint takes the model in the path and the key as a
	# query param ( v1beta API ).
	url = (
		f"https://generativelanguage.googleapis.com/v1beta/models/"
		f"{model}:generateContent?key={api_key}"
	)
	r = _post_with_retry(
		url ,
		headers = { "Content-Type": "application/json" } ,
		json_body = {
			"systemInstruction" : {
				"parts" : [ { "text": system_prompt } ] ,
			} ,
			"contents" : [
				{ "role": "user" , "parts": [ { "text": user_prompt } ] } ,
			] ,
			"generationConfig" : { "temperature": 0.2 } ,
		} ,
		timeout = timeout ,
		label   = "gemini" ,
	)
	r.raise_for_status()
	data = r.json()
	cands = data.get( "candidates" ) or []
	if not cands:
		return ""
	parts = cands[ 0 ].get( "content" , {} ).get( "parts" ) or []
	return "".join( p.get( "text" , "" ) for p in parts ).strip()


_DISPATCH = {
	"claude" : _call_claude ,
	"openai" : _call_openai ,
	"gemini" : _call_gemini ,
}


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

_HASHTAGS_RE = re.compile(
	r"^\s*HASHTAGS\s*:\s*(.+?)$"   ,
	re.IGNORECASE | re.MULTILINE   ,
)
_SUMMARY_RE  = re.compile(
	r"^\s*SUMMARY\s*:\s*\n?(.*)\Z" ,
	re.IGNORECASE | re.DOTALL | re.MULTILINE ,
)


def _parse_response( raw ):
	"""Pull HASHTAGS: and SUMMARY: blocks out of the LLM response.
	Forgiving : if the format isn't followed , dumps everything into
	`summary` with empty hashtags so the caller can still write a row."""
	if not raw:
		return { "hashtags": "" , "summary": "" }

	# HASHTAGS : single line.
	htag_m = _HASHTAGS_RE.search( raw )
	hashtags = htag_m.group( 1 ).strip() if htag_m else ""

	# SUMMARY : everything after the SUMMARY: label until end-of-string.
	sum_m = _SUMMARY_RE.search( raw )
	if sum_m:
		summary = sum_m.group( 1 ).strip()
	else:
		# Format wasn't followed -- best effort : strip the HASHTAGS line
		# if we found one , keep the rest as summary.
		if htag_m:
			summary = ( raw[ : htag_m.start() ] + raw[ htag_m.end() : ] ).strip()
		else:
			summary = raw.strip()

	# Normalize hashtags : keep only space-separated tokens that start
	# with '#'. Other inline text the model sometimes adds gets dropped.
	tokens   = [ t for t in hashtags.split() if t.startswith( "#" ) ]
	hashtags = " ".join( tokens )

	return { "hashtags": hashtags , "summary": summary }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def summarize(
	provider , model , section_key , section_display ,
	doi , title , text ,
	config = None , timeout = 120 ,
):
	"""Single LLM round-trip for one ( paper , section ) pair. Returns
	{ 'hashtags' , 'summary' } on success ; {} on any failure ( the
	caller skips and moves on )."""
	provider = ( provider or "claude" ).lower()
	if provider not in _DISPATCH:
		print( f"LLM :: unknown provider {provider!r} ; "
		       f"expected one of {PROVIDERS}" )
		return {}

	api_key = _resolve_api_key( provider , config )
	if not api_key:
		env_hint = " / ".join( ENV_KEYS[ provider ] )
		print( f"LLM :: no API key for {provider} "
		       f"( set {env_hint} or config.yaml gpts.{provider}.key )" )
		return {}

	model = resolve_model( provider , model , config )

	# Slice the section text down to fit the provider's context window
	# before building the prompt. A book chapter pulled in via 'prma md'
	# / 'prma methods' can easily blow past gpt-4o's 128k tokens.
	clipped , was_truncated = _truncate_for_provider( text , provider )
	if was_truncated:
		print(
			f"LLM :: {provider} input for {doi} ( section={section_key} ) "
			f"truncated {len( text )} -> {len( clipped )} chars to fit context window"
		)

	system_prompt = _SYSTEM_PROMPT
	user_prompt   = _build_prompt( section_display , doi , title , clipped )

	try:
		raw = _DISPATCH[ provider ](
			api_key , model , system_prompt , user_prompt , timeout ,
		)
	except requests.HTTPError as e:
		body = ""
		try:
			body = e.response.text[ :500 ]
		except Exception:
			pass
		print( f"LLM :: {provider} HTTP error for {doi}: {e} {body}" )
		return {}
	except Exception as e:
		print( f"LLM :: {provider} call failed for {doi}: {e}" )
		return {}

	parsed = _parse_response( raw )
	if not parsed[ "summary" ]:
		print( f"LLM :: {provider} returned empty summary for {doi}" )
		return {}
	return parsed
