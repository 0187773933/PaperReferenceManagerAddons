"""
Inclusion screening for the /review compute.

Inclusion requires ALL THREE:
  1. the paper analyses fMRI/BOLD data,
  2. it performs a DECODING task (fMRI in -> stimulus / mental content / label out),
  3. transformer or attention machinery sits inside the model THEY BUILT
     (not merely cited in related work).

Everything is scored with quotable evidence so the decisions are auditable.
"""
import bisect
import re
from collections import OrderedDict

# ---------------------------------------------------------------- modality
FMRI_CORE = [
	r'\bfMRI\b', r'\bfunctional MRI\b', r'\bfunctional magnetic resonance imaging\b',
	r'\bBOLD\b', r'\bblood[- ]oxygen[- ]level', r'\bvoxel', r'\bTR\b', r'\bhemodynamic\b',
]
FMRI_STRONG = [
	r'\bfMRI (?:data|signal|scan|time ?series|volume|recording|response|activity|acquisition)',
	r'\bBOLD (?:signal|response|time ?series|activity)', r'\bvoxel[- ](?:wise|level|space)',
	r'\b(?:whole[- ]brain|cortical) (?:fMRI|BOLD)', r'\b4D fMRI\b', r'\brs-?fMRI\b', r'\bresting[- ]state fMRI\b',
]

# words that only count as fMRI evidence (BOLD/voxel/hemodynamics are fMRI-specific)
FMRI_EVIDENCE = [
	r'\bfMRI\b', r'\bfunctional MRI\b', r'\bfunctional magnetic resonance\b', r'\bBOLD\b',
	r'\bblood[- ]oxygen[- ]level', r'\bvoxel', r'\bhemodynamic\b', r'\bh(?:a)?emodynamic response function\b',
	r'\bHRF\b', r'\bTR\s*[=:]', r'\brepetition time\b', r'\becho[- ]planar\b', r'\bcortical surface\b',
	r'\bgrayordinate', r'\bMNI\b', r'\bparcellat', r'\bfunctional connectivity\b',
]
# rival modalities -- if these dominate the methods, it is not an fMRI decoding paper
OTHER_MODALITY = [
	r'\bEEG\b', r'\belectroencephalograph', r'\bMEG\b', r'\bmagnetoencephalograph',
	r'\bECoG\b', r'\belectrocorticograph', r'\bsEEG\b', r'\bstereo-?EEG\b',
	r'\bfNIRS\b', r'\bnear[- ]infrared\b', r'\boptode', r'\bmicroelectrode',
	r'\bUtah array\b', r'\bspike (?:rate|count|train|band)', r'\bintracranial\b', r'\bintracortical\b',
	r'\belectrode', r'\bimplanted array', r'\bpenetrating array', r'\bfiring rate',
	r'\bthreshold crossing', r'\bneural spik', r'\bBCI implant', r'\bneuroprosthes',
	r'\bEMG\b', r'\bEOG\b', r'\bMUA\b', r'\bLFP\b',
]

# ---------------------------------------------------------------- attention
ATTN_BUILT = [
	r'\bmulti-?head (?:self-?|cross-?)?attention\b', r'\bself-?attention\b', r'\bcross-?attention\b',
	r'\bco-?attention\b', r'\battention (?:heads?|module|block|mechanism|layer|weights?|map|pooling)\b',
	r'\btransformer (?:encoder|decoder|block|layer|backbone|architecture|module|model)\b',
	r'\bvision transformer\b', r'\bViT\b', r'\bSwin\b', r'\bPerceiver\b', r'\bconformer\b',
	r'\bquery (?:tokens?|vectors?)\b', r'\bkey[-,/ ]value\b', r'\bQKV\b', r'\bscaled dot[- ]product\b',
	r'\bpositional (?:encoding|embedding)\b', r'\bgraph attention\b', r'\bdeformable attention\b',
	r'\battentive\b', r'\bCLS token\b', r'\bclass token\b', r'\btoken(?:iz|is)ation\b',
]
# pretrained transformer stacks used as part of the decoding pipeline
ATTN_PRETRAINED = [
	r'\bCLIP\b', r'\bOpenCLIP\b', r'\bBLIP-?2?\b', r'\bLLaVA\b', r'\bGPT\b', r'\bGPT-?[234]\b',
	r'\bGenerative Pre-?[Tt]rained Transformer\b', r'\bLLaMA\b', r'\bLlama\b',
	r'\bVicuna\b', r'\bMistral\b', r'\bBERT\b', r'\bRoBERTa\b', r'\bDeBERTa\b', r'\bT5\b', r'\bBART\b',
	r'\bOPT-\d', r'\bWhisper\b', r'\bwav2vec\b', r'\bHuBERT\b', r'\bStable Diffusion\b',
	r'\bVersatile Diffusion\b', r'\bunCLIP\b', r'\bDINOv2\b', r'\bBEiT\b', r'\bSigLIP\b', r'\bQwen\b',
	r'\bfoundation model\b', r'\blarge language model\b', r'\bLLM\b', r'\bvision[- ]language model\b',
	# NB: bare "language model" is NOT listed -- in this literature it often means the
	# Lichtheim/Geschwind *neuroanatomical* language model, not an NLP one.
	r'\bpre-?trained language model\b', r'\bneural language model\b', r'\bcausal language model\b',
	r'\bautoregressive language model\b', r'\bmasked language model\b', r'\blanguage model (?:prior|head|decoder|embeddings?)\b',
	r'\blanguage model\b(?=[^.]{0,80}\b(?:GPT|token|embedding|transformer|fine-?tun|pre-?train|beam|perplexity)\b)',
	r'\bsentence (?:encoder|embedding|transformer)\b', r'\bSentenceBERT\b', r'\bSBERT\b',
	r'\bcontextual(?:ized)? embeddings?\b', r'\bword embeddings? from\b', r'\bBERTScore\b',
]
ATTN_ANY = ATTN_BUILT + ATTN_PRETRAINED   # built once; see _alt() cache note

# phrases meaning "WE built it"
OURS = [
	r'\bwe (?:propose|present|introduce|design|develop|build|employ|adopt|use|utilize|implement|train|apply)\b',
	r'\bour (?:model|method|framework|architecture|approach|network|encoder|decoder|pipeline)\b',
	r'\bthe proposed\b', r'\bthis (?:paper|work|study) (?:proposes|presents|introduces)\b',
	r'\bconsists? of\b', r'\bis composed of\b', r'\bcomprises\b', r'\bfollowed by\b', r'\bwe first\b',
]

# ---------------------------------------------------------------- task type
TASK_RULES = OrderedDict([
	('inner/imagined speech', [
		r'\binner speech\b', r'\bimagined speech\b', r'\bcovert (?:speech|articulation|naming|reading|rehearsal)\b',
		r'\bsilent (?:reading|naming|speech|verb generation|word generation)\b', r'\bimagined (?:words?|sentences?|語)\b',
		r'\bverbal imagery\b', r'\bself-?generated (?:speech|language|thought)\b', r'\bsubvocal',
	]),
	('language / semantic decoding', [
		r'\bbrain-?to-?text\b', r'\bsemantic (?:decoding|reconstruction)\b', r'\bdecod\w+ (?:language|text|words?|sentences?|semantics?|meaning|narrative|story|speech)\b',
		r'\bcontinuous language\b', r'\bword[- ]level decoding\b', r'\bcloze\b', r'\bfMRI-?to-?text\b',
		r'\bopen[- ]vocabulary\b', r'\blanguage (?:decoder|decoding)\b', r'\bcaption(?:ing|s)? (?:from|of) (?:brain|fMRI|neural)\b',
	]),
	('visual reconstruction / decoding', [
		r'\bimage reconstruction\b', r'\bvisual (?:reconstruction|decoding|stimulus decoding)\b',
		r'\bfMRI-?to-?image\b', r'\breconstruct\w* (?:the )?(?:images?|visual stimuli|seen images?|natural images?|video)\b',
		r'\bbrain-?to-?image\b', r'\bimage retrieval\b', r'\bdecod\w+ (?:visual|images?|objects?|scenes?|faces?)\b',
		r'\bstimulus reconstruction\b', r'\bmind[- ]reading of images\b',
	]),
	('audio / music decoding', [
		r'\bbrain-?to-?(?:music|audio|speech signal)\b', r'\breconstruct\w* (?:music|audio|sound|speech waveform)\b',
		r'\bmusic (?:reconstruction|retrieval) from\b', r'\bdecod\w+ (?:music|audio|sound|heard speech)\b',
	]),
	('clinical / psychiatric classification', [
		r'\b(?:autism|ASD|Alzheimer|AD\b|MCI\b|schizophrenia|depression|MDD\b|ADHD|bipolar|Parkinson|epilep\w+)\b'
		r'[^.]{0,80}\b(?:classif|diagnos|detect|predict|identif)',
		r'\b(?:classif|diagnos|detect)\w*[^.]{0,60}\b(?:autism|ASD|Alzheimer|schizophrenia|MDD|ADHD|bipolar|disorder|disease|patients? vs)\b',
		r'\bneuropsychiatric (?:disorder|disease)\b', r'\bdisease (?:classification|diagnosis|prediction)\b',
	]),
	('brain state / cognitive task decoding', [
		r'\btask (?:state )?(?:classification|decoding|identification)\b', r'\bcognitive state (?:decoding|classification)\b',
		r'\bbrain state (?:decoding|classification|prediction)\b', r'\bmental state decoding\b',
		r'\bdecod\w+ (?:task|cognitive states?|mental states?|conditions?)\b', r'\bMVPA\b',
	]),
	('individual fingerprinting / trait prediction', [
		r'\bfingerprint\w*\b', r'\bsubject identification\b', r'\bindividual identification\b',
		r'\b(?:age|sex|gender|IQ|intelligence|fluid intelligence|behaviou?ral trait)[^.]{0,40}\bpredict\w*',
		r'\bphenotype prediction\b',
	]),
])

# ---------------------------------------------------------------- exclusions
EXCLUDE_RULES = OrderedDict([
	('review / survey (not a primary model paper)', [
		r'\b(?:this|our|the present)\s+(?:comprehensive\s+|systematic\s+|scoping\s+|narrative\s+)?'
		r'(?:review|survey|overview|taxonomy|meta-?analysis)\b',
		r'\bin this (?:review|survey)\b', r'\bwe (?:review|survey|summarize the literature)\b',
		r'\bwe provide a (?:comprehensive )?(?:review|survey|overview)\b',
		r'\bPRISMA\b', r'\bsystematic literature (?:review|search)\b',
	]),
	('encoding model only (stimulus -> brain, not brain -> stimulus)', [
		r'\bencoding model\b[^.]{0,120}\bpredict\w*[^.]{0,40}\b(?:brain|neural|voxel|BOLD)\s+(?:response|activity)',
		r'\bwe (?:build|train|fit) (?:an? )?encoding model',
		r'\bpredict\w* (?:voxel|brain|neural|BOLD) (?:responses?|activity) from\b',
	]),
	('fMRI image reconstruction / acceleration (signal processing, not decoding)', [
		r'\b(?:accelerat\w+|undersampl\w+|k-?space|compressed sensing)\b[^.]{0,100}\breconstruction\b',
		r'\bimage reconstruction\b[^.]{0,80}\b(?:k-?space|MRI acquisition|scan time)\b',
		r'\bdenoising of fMRI\b', r'\bmotion (?:correction|artifact removal)\b',
	]),
	('non-fMRI modality (EEG/MEG/ECoG/sEEG only)', []),  # decided numerically
])

SURVEY_TITLE = re.compile(
	r'\b(?:a )?(?:comprehensive |systematic |scoping |brief |short |critical )?'
	r'(?:review|survey|overview|taxonomy|perspective|tutorial|primer|meta-?analysis)\b', re.I)


_ALT_CACHE = {}


def _alt(pats):
	"""
    Compile a list of patterns into one alternation (big speed win for counting).

    The cache key MUST be the pattern tuple, never id(pats): callers pass
    temporary concatenated lists, and a freed list's address can be reused by a
    different list, which would silently return the wrong compiled regex.
    """
	key = tuple(pats)
	rx = _ALT_CACHE.get(key)
	if rx is None:
		rx = re.compile('|'.join('(?:%s)' % p for p in pats), re.I)
		_ALT_CACHE[key] = rx
	return rx


def count(text, pats):
	if not pats:
		return 0
	return len(_alt(pats).findall(text))


def first_hits(text, pats, limit=3, width=240):
	out = []
	for p in pats:
		for m in re.finditer(p, text, re.I):
			a, b = max(0, m.start() - width // 2), min(len(text), m.end() + width // 2)
			out.append(re.sub(r'\s+', ' ', text[a:b]).strip())
			break
		if len(out) >= limit:
			break
	return out


def near(text, pat_a, pat_b, window=400):
	"""True if any pat_a match sits within `window` chars of a pat_b match."""
	bs = sorted(m.start() for m in _alt(pat_b).finditer(text))
	if not bs:
		return False
	for m in _alt(pat_a).finditer(text):
		i = bisect.bisect_left(bs, m.start())
		for j in (i - 1, i):
			if 0 <= j < len(bs) and abs(m.start() - bs[j]) <= window:
				return True
	return False


def classify(title, abstract, methods, body, tags, modalities):
	"""Return dict with include flag, task category, exclusion reason, scores, evidence."""
	tags = {t.lower() for t in tags}
	mods = {m.lower() for m in modalities}
	core = methods if len(methods) > 400 else body
	full = body + '\n' + methods

	# --- 1. fMRI? -- decided by modality DOMINANCE, not mere mention.
	fmri_ev_full = count(full, FMRI_EVIDENCE)
	fmri_ev_core = count(core, FMRI_EVIDENCE)
	other_ev_full = count(full, OTHER_MODALITY)
	other_ev_core = count(core, OTHER_MODALITY)
	fmri_hits = fmri_ev_full
	fmri_strong = count(full, FMRI_STRONG)

	# fMRI must carry at least as much methodological weight as any rival modality
	dom_core = fmri_ev_core >= max(5, other_ev_core * 0.9)
	dom_full = fmri_ev_full >= max(10, other_ev_full * 0.75)
	is_fmri = (fmri_ev_full >= 8) and fmri_ev_core >= 4 and (dom_core or dom_full)
	multimodal = other_ev_core >= 5 and fmri_ev_core >= 5

	# --- 2. attention in THEIR model?
	attn_methods = count(core, ATTN_BUILT)
	attn_pre_methods = count(core, ATTN_PRETRAINED)
	attn_body = count(body, ATTN_BUILT)
	attn_ours = near(core, ATTN_ANY, OURS, window=500)
	attn_total = attn_methods + attn_pre_methods
	attn_built = (attn_methods >= 2) or (attn_total >= 3) or \
				 (attn_total >= 2 and attn_ours) or \
				 (attn_methods >= 1 and attn_ours)
	attn_role = []
	if count(core, [r'\btransformer (?:encoder|backbone|block|layer)', r'\bvision transformer\b', r'\bViT\b',
					r'\bSwin\b', r'\bBrain ?Network ?Transformer\b']):
		attn_role.append('transformer backbone over fMRI')
	if count(core, [r'\bcross-?attention\b', r'\bco-?attention\b']):
		attn_role.append('cross-attention fusion')
	if count(core, [r'\bself-?attention\b', r'\bmulti-?head']):
		attn_role.append('self-attention layers')
	if count(core, [r'\battention pooling\b', r'\battention weights? (?:were|are) (?:used|analy)']):
		attn_role.append('attention pooling / interpretability')
	if count(core, ATTN_PRETRAINED) >= 1:
		attn_role.append('pretrained transformer (LM/VLM/CLIP) in pipeline')

	# --- 3. decoding task?
	task, task_ev = None, []
	scores = {}
	for name, pats in TASK_RULES.items():
		scores[name] = 3 * count(title, pats) + 2 * count(abstract, pats) + count(full, pats)
	ranked = sorted(scores.items(), key=lambda kv: -kv[1])
	if ranked[0][1] > 0:
		task = ranked[0][0]
		task_ev = first_hits(abstract + '\n' + full, TASK_RULES[task], limit=2)
	is_decoding = ranked[0][1] >= 2

	# --- exclusions
	reason = None
	_sv = EXCLUDE_RULES['review / survey (not a primary model paper)']
	survey_score = 4 * count(title, _sv) + 2 * count(abstract, _sv) + count(body[:6000], _sv)
	if SURVEY_TITLE.search(title) or survey_score >= 4:
		reason = 'review / survey (not a primary model paper)'
	elif not is_fmri:
		reason = 'non-fMRI modality (EEG/MEG/ECoG/fNIRS dominant)'
	elif not attn_built:
		reason = 'no transformer/attention inside the authors\' own model'
	elif not is_decoding:
		enc = count(full, EXCLUDE_RULES['encoding model only (stimulus -> brain, not brain -> stimulus)'])
		rec = count(full, EXCLUDE_RULES['fMRI image reconstruction / acceleration (signal processing, not decoding)'])
		if enc >= 2:
			reason = 'encoding model only (stimulus -> brain, not brain -> stimulus)'
		elif rec >= 2:
			reason = 'fMRI image reconstruction / acceleration (signal processing, not decoding)'
		else:
			reason = 'no clear fMRI decoding task'

	return dict(
		include=(reason is None),
		exclusion_reason=reason or '',
		task_category=task or 'unclassified',
		task_scores=scores,
		fmri_score=fmri_hits, fmri_strong=fmri_strong,
		fmri_evidence_methods=fmri_ev_core, rival_modality_methods=other_ev_core,
		multimodal=multimodal,
		attn_methods=attn_methods, attn_pretrained=attn_pre_methods, attn_body=attn_body,
		attn_total_methods=attn_total,
		attn_attributed_to_authors=attn_ours,
		attention_role='; '.join(dict.fromkeys(attn_role)) or 'unspecified',
		decode_score=ranked[0][1],
		other_modality_score=other_ev_full,
		task_evidence=' /// '.join(task_ev),
		attn_evidence=' /// '.join(first_hits(core, ATTN_BUILT, limit=2)),
	)
