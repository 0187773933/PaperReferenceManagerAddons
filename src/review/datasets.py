"""
Public-dataset detection + corpus-mined acquisition consensus.

Most deep-learning fMRI papers never state TR/TE/voxel size: they say
"we use HCP" and move on.  So we (a) detect which public datasets each paper
uses, and (b) mine the WHOLE corpus for sentences where a dataset name sits
near acquisition parameters, then report the consensus value together with how
many papers reported it.  Nothing is hand-asserted: every value in the
dataset reference is traceable to papers in this corpus.
"""
import re
from collections import Counter, defaultdict

DATASETS = {
	'HCP (Human Connectome Project)': [r'\bHuman Connectome Project\b', r'\bHCP\b(?!-?MMP)', r'\bHCP-?(?:YA|1200|S1200|Aging|D)\b'],
	'NSD (Natural Scenes Dataset)': [r'\bNatural Scenes Dataset\b', r'\bNSD\b'],
	'ABIDE': [r'\bABIDE\s*(?:I{1,2}|1|2)?\b', r'\bAutism Brain Imaging Data Exchange\b'],
	'ADNI': [r'\bADNI\b', r"\bAlzheimer'?s Disease Neuroimaging Initiative\b"],
	'UK Biobank': [r'\bUK\s?Biobank\b', r'\bUKB\b'],
	'ADHD-200': [r'\bADHD-?200\b'],
	'Generic Object Decoding (Kamitani)': [r'\bGeneric Object Decoding\b', r'\bGOD dataset\b', r'\bDeepRecon\b', r'\bKamitani\b'],
	'BOLD5000': [r'\bBOLD5000\b'],
	'Narratives (Nastase)': [r'\bNarratives\b(?!\s+of)', r'\bNastase\b'],
	'Moth Radio Hour / Huth-LeBel story data': [r'\bMoth Radio Hour\b', r'\bLeBel\b', r'\bHuth\b'],
	'Pereira et al. 2018': [r'\bPereira\b'],
	'THINGS-fMRI': [r'\bTHINGS-?(?:fMRI|data)?\b'],
	'Algonauts': [r'\bAlgonauts\b'],
	'CNeuroMod / Courtois': [r'\bCNeuroMod\b', r'\bCourtois\b'],
	'StudyForrest': [r'\bStudy\s?Forrest\b'],
	'REST-meta-MDD': [r'\bREST-?meta-?MDD\b'],
	'OpenNeuro / ds00xxxx': [r'\bOpenNeuro\b', r'\bds\d{6}\b'],
	'SRPBS / Japanese multi-site': [r'\bSRPBS\b'],
	'ABCD': [r'\bABCD\b', r'\bAdolescent Brain Cognitive Development\b'],
	'MSC (Midnight Scan Club)': [r'\bMidnight Scan Club\b', r'\bMSC\b'],
	'Forrest Gump 7T': [r'\bForrest Gump\b'],
	'HCP-EP / HCP-D / HCP-A': [r'\bHCP-?(?:EP|D|A)\b'],
	'Deep Image Reconstruction (Shen)': [r'\bDeep Image Reconstruction\b'],
	'NOD (Natural Object Dataset)': [r'\bNatural Object Dataset\b', r'\bNOD\b'],
	'Nifty/Neuromark ICA templates': [r'\bNeuroMark\b'],
}

# parameters we try to attach to a dataset name
PARAM_RX = {
	'TR': re.compile(r'\b(?:TR|repetition time)\s*(?:\([A-Z]+\))?\s*[=:of ]{1,4}\s*(\d+(?:\.\d+)?)\s*(ms|msec|s\b|sec|seconds?)?', re.I),
	'TE': re.compile(r'\b(?:TE|echo time)\s*(?:\([A-Z]+\))?\s*[=:of ]{1,4}\s*(\d+(?:\.\d+)?)\s*(ms|msec|s\b|sec)?', re.I),
	'Flip_Angle': re.compile(r'\bflip angle\s*(?:\(FA\))?\s*[=:of ]{1,4}\s*(\d+(?:\.\d+)?)\s*(?:°|deg)?', re.I),
	'Voxel_Size': re.compile(r'\b(\d+(?:\.\d+)?\s*(?:[x×]\s*\d+(?:\.\d+)?\s*){0,2}mm)(?:\s*3|³)?\s*(?:isotropic|iso)?', re.I),
	'Field_Strength': re.compile(r'\b(1\.5|3|4|7|9\.4)\s*[- ]?(?:T|Tesla)\b', re.I),
	'Multiband': re.compile(r'\bmulti-?band\s*(?:factor)?\s*(?:of|=|:)?\s*(\d)\b', re.I),
	'N_Slices': re.compile(r'\b(\d{2,3})\s+slices\b', re.I),
	'Scanner': re.compile(r'\b(Siemens\s+\w+|Philips\s+\w+|GE\s+\w+)\b'),
}


def detect(text):
	"""Which public datasets does this paper use?  Returns list of names."""
	out = []
	for name, pats in DATASETS.items():
		n = sum(len(re.findall(p, text)) for p in pats)
		if n >= 2:
			out.append(name)
	return out


def mine_corpus(papers, window=700):
	"""
    papers: iterable of (key, text).
    For every dataset mention, look in a +/-window char neighbourhood for
    acquisition parameters and tally them.  Returns
    {dataset: {param: Counter(value -> set_of_paper_keys)}}
    """
	tally = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
	for key, text in papers:
		for name, pats in DATASETS.items():
			spots = [m.start() for p in pats for m in re.finditer(p, text)]
			if not spots:
				continue
			for s in spots[:40]:
				chunk = text[max(0, s - window): s + window]
				for pname, rx in PARAM_RX.items():
					for m in rx.finditer(chunk):
						if _is_noise(pname, chunk, m):
							continue
						val = _norm_param(pname, m)
						if val:
							tally[name][pname][val].add(key)
	return tally


_NOISE_LEFT = re.compile(r'(smooth\w*|FWHM|kernel|field of view|FOV|matrix|thickness|gap|'
						 r'in-?plane|slice)\W{0,20}$', re.I)


def _is_noise(pname, chunk, m):
	"""Reject a voxel-size match that is really a smoothing kernel or an FOV."""
	if pname != 'Voxel_Size':
		return False
	left = chunk[max(0, m.start() - 40):m.start()]
	if _NOISE_LEFT.search(left):
		return True
	nums = [float(x) for x in re.findall(r'\d+(?:\.\d+)?', m.group(1))]
	return not nums or max(nums) > 5.0        # fMRI voxels are <= ~5 mm


def _norm_param(pname, m):
	g = m.group(1)
	if pname in ('TR', 'TE'):
		try:
			v = float(g)
		except ValueError:
			return None
		unit = (m.group(2) or '').lower()
		if unit.startswith('m') or (not unit and v > 50):
			return f"{v:g} ms"
		return f"{v:g} s" if v < 50 else None
	if pname == 'Flip_Angle':
		return f"{g}°"
	if pname == 'Field_Strength':
		return f"{g}T"
	if pname == 'Multiband':
		return f"MB {g}"
	if pname == 'N_Slices':
		return f"{g} slices"
	if pname == 'Voxel_Size':
		v = re.sub(r'\s+', '', g)
		return v if re.match(r'^\d', v) else None
	return re.sub(r'\s+', ' ', g).strip()


def consensus_rows(tally, min_papers=2, top_k=3):
	"""Flatten the tally into rows for the dataset reference."""
	rows = []
	for ds in sorted(tally):
		for pname in ['Field_Strength', 'Scanner', 'TR', 'TE', 'Flip_Angle',
					  'Voxel_Size', 'N_Slices', 'Multiband']:
			vals = tally[ds].get(pname)
			if not vals:
				continue
			ranked = sorted(vals.items(), key=lambda kv: -len(kv[1]))
			ranked = [(v, ks) for v, ks in ranked if len(ks) >= min_papers][:top_k]
			if not ranked:
				continue
			best, keys = ranked[0]
			alts = '; '.join(f"{v} (n={len(k)})" for v, k in ranked[1:])
			rows.append(dict(
				Dataset=ds, Parameter=pname, Consensus_Value=best,
				N_Papers_Reporting=len(keys),
				Other_Reported_Values=alts,
				Example_Sources='; '.join(sorted(keys)[:4]),
			))
	return rows
