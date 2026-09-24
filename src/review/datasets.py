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

# Where each of those collections LIVES -- the page a reader would actually go
# to. Keyed by the same names as DATASETS above , because a dataset's home is a
# fact about the dataset , not about any one surface : /datasets links the badges
# it draws through here , and anything else that shows a dataset name can too.
#
# Every URL below was checked against the source itself , not remembered : the
# OpenNeuro accessions against OpenNeuro's own API ( ds001246 = "Generic Object
# Decoding ( fMRI on ImageNet )" , ds001506 = "Deep Image Reconstruction" ,
# ds002345 = "Narratives" , ds000224 = "The Midnight Scan Club ( MSC ) dataset" ,
# ds003020 = the passive natural-language listening data , ds004496 = the
# large-scale naturalistic-scene data ) and the OSF node against OSF's ( crwz7 =
# "Toward a universal decoder of linguistic meaning from brain activation" ).
# A wrong link is worse than none , so anything added here should be checked the
# same way ; a name with no entry simply renders unlinked.
HOMES = {
	'HCP (Human Connectome Project)'         : 'https://www.humanconnectome.org/' ,
	'HCP-EP / HCP-D / HCP-A'                 : 'https://www.humanconnectome.org/' ,
	'NSD (Natural Scenes Dataset)'           : 'https://naturalscenesdataset.org/' ,
	'ABIDE'                                  : 'https://fcon_1000.projects.nitrc.org/indi/abide/' ,
	'ADHD-200'                               : 'https://fcon_1000.projects.nitrc.org/indi/adhd200/' ,
	'ADNI'                                   : 'https://adni.loni.usc.edu/' ,
	'UK Biobank'                             : 'https://www.ukbiobank.ac.uk/' ,
	'ABCD'                                   : 'https://abcdstudy.org/' ,
	'BOLD5000'                               : 'https://bold5000-dataset.github.io/website/' ,
	'THINGS-fMRI'                            : 'https://things-initiative.org/' ,
	'Algonauts'                              : 'http://algonauts.csail.mit.edu/' ,
	'CNeuroMod / Courtois'                   : 'https://www.cneuromod.ca/' ,
	'StudyForrest'                           : 'https://www.studyforrest.org/' ,
	'Forrest Gump 7T'                        : 'https://www.studyforrest.org/' ,
	'REST-meta-MDD'                          : 'http://rfmri.org/REST-meta-MDD' ,
	'SRPBS / Japanese multi-site'            : 'https://bicr-resource.atr.jp/srpbsopen/' ,
	'OpenNeuro / ds00xxxx'                   : 'https://openneuro.org/' ,
	'Nifty/Neuromark ICA templates'          : 'https://trendscenter.org/data/' ,
	'Generic Object Decoding (Kamitani)'     : 'https://openneuro.org/datasets/ds001246' ,
	'Deep Image Reconstruction (Shen)'       : 'https://openneuro.org/datasets/ds001506' ,
	'Narratives (Nastase)'                   : 'https://openneuro.org/datasets/ds002345' ,
	'MSC (Midnight Scan Club)'               : 'https://openneuro.org/datasets/ds000224' ,
	'Moth Radio Hour / Huth-LeBel story data': 'https://openneuro.org/datasets/ds003020' ,
	'NOD (Natural Object Dataset)'           : 'https://openneuro.org/datasets/ds004496' ,
	'Pereira et al. 2018'                    : 'https://osf.io/crwz7/' ,
}


def home(name):
	"""Where a detected dataset lives, or '' when we have no checked URL for it."""
	return HOMES.get(name, '')


# What each collection IS , in one line. A list of names like ` SRPBS ` ,
# ` REST-meta-MDD ` , ` NOD ` tells you nothing about what you are looking at ,
# and /datasets shows one row per collection -- so the row has to be able to say
# it. Deliberately WHAT and WHO , not how many : subject counts and scanner
# details drift between releases , and this file is not the place anyone should
# be reading them from ( the acquisition consensus below is , and it cites the
# corpus for every value ). A name with no entry simply shows no description.
DESCRIPTIONS = {
	'HCP (Human Connectome Project)':
		'Large-scale multimodal MRI of healthy young adults — the field\'s default resting-state and task reference.' ,
	'HCP-EP / HCP-D / HCP-A':
		'The Connectome lifespan and early-psychosis extensions — development, aging and clinical cohorts on HCP protocols.' ,
	'NSD (Natural Scenes Dataset)':
		'7T fMRI of eight subjects viewing tens of thousands of COCO images — the standard for image reconstruction work.' ,
	'ABIDE':
		'Aggregated resting-state fMRI from autism and control participants, pooled across many independent sites.' ,
	'ADHD-200':
		'Multi-site resting-state fMRI of children and adolescents with ADHD alongside typically-developing controls.' ,
	'ADNI':
		'Longitudinal MRI, PET, genetics and clinical follow-up on Alzheimer\'s disease and mild cognitive impairment.' ,
	'UK Biobank':
		'Population-scale UK cohort pairing brain MRI with genetics, health records and lifestyle measures.' ,
	'ABCD':
		'Longitudinal US study following adolescent brain development with repeated imaging and behavioural batteries.' ,
	'BOLD5000':
		'fMRI of subjects viewing 5,000 real-world scene images drawn from COCO, ImageNet and SUN.' ,
	'THINGS-fMRI':
		'Responses to the THINGS image database, which spans thousands of everyday object concepts.' ,
	'Algonauts':
		'A recurring challenge pairing brain responses to naturalistic images or video with model predictions.' ,
	'CNeuroMod / Courtois':
		'Deeply-sampled individuals scanned for many hours each on movies, video games and audio.' ,
	'StudyForrest':
		'Extensive 3T and 7T fMRI of participants hearing and watching the film Forrest Gump.' ,
	'Forrest Gump 7T':
		'The high-field arm of StudyForrest — 7T responses to the film\'s audio-visual narrative.' ,
	'REST-meta-MDD':
		'Aggregated resting-state fMRI from major-depression patients and controls across Chinese sites.' ,
	'SRPBS / Japanese multi-site':
		'Japanese multi-site resting-state fMRI spanning several psychiatric diagnoses and healthy controls.' ,
	'OpenNeuro / ds00xxxx':
		'The open BIDS archive itself — a paper citing a bare ds###### accession is pointing here.' ,
	'Nifty/Neuromark ICA templates':
		'TReNDS\'s spatially-constrained ICA templates, for extracting comparable networks across studies.' ,
	'Generic Object Decoding (Kamitani)':
		'fMRI while subjects viewed ImageNet objects — the classic object-decoding benchmark.' ,
	'Deep Image Reconstruction (Shen)':
		'Kamitani-lab fMRI of seen and imagined images, built for reconstructing what was viewed.' ,
	'Narratives (Nastase)':
		'A large collection of fMRI datasets in which subjects listened to spoken stories.' ,
	'MSC (Midnight Scan Club)':
		'Ten subjects scanned repeatedly across many sessions, for individual-level network mapping.' ,
	'Moth Radio Hour / Huth-LeBel story data':
		'Hours of fMRI per subject during passive listening to natural spoken stories.' ,
	'NOD (Natural Object Dataset)':
		'Large-scale fMRI of many subjects viewing naturalistic object images.' ,
	'Pereira et al. 2018':
		'fMRI of sentence and concept reading, built to train a general decoder of linguistic meaning.' ,
}


def describe(name):
	"""One line on what a detected dataset is, or '' when we have none."""
	return DESCRIPTIONS.get(name, '')


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


def detect(text, min_hits=2):
	"""Which public datasets does this paper use?  Returns list of names.

	min_hits is the bar for believing a mention. Two is right for a full paper --
	50,000 characters in which "HCP" turns up once in a sentence about somebody
	else's work is not a paper that uses HCP. It is wrong for an ABSTRACT, which
	names its data once and moves on, so /review-missing passes 1.
	"""
	out = []
	for name, pats in DATASETS.items():
		n = sum(len(re.findall(p, text)) for p in pats)
		if n >= min_hits:
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
