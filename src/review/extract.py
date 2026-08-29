"""
Field extractors for the /review compute.

Everything here is deterministic , regex / rule based , and returns BOTH a
normalised value and the verbatim source span it came from , so every value the
page shows is auditable back to the paper's own words. Knows nothing about this
project : hand it a string , it hands back a Field.
"""
import re
from collections import OrderedDict

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
NUM = r'[0-9]+(?:\.[0-9]+)?'
WS = re.compile(r'\s+')


def clean(s):
	return WS.sub(' ', s or '').strip()


def sentences(text):
	"""Cheap sentence splitter that survives 'et al.', '1.5 s', 'Fig. 3'."""
	protected = re.sub(r'\b(et al|Fig|Eq|ref|vs|approx|cf|i\.e|e\.g|Dr|No)\.', r'\1<DOT>', text)
	protected = re.sub(r'(\d)\.(\d)', r'\1<DOT>\2', protected)
	parts = re.split(r'(?<=[.!?])\s+(?=[A-Z(\[])', protected)
	return [p.replace('<DOT>', '.').strip() for p in parts if p.strip()]


WORDNUM = {
	'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
	'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11, 'twelve': 12, 'sixteen': 16,
	'twenty': 20, 'twenty-four': 24, 'thirty-two': 32, 'forty': 40, 'sixty-four': 64,
}
WORDNUM_RX = '|'.join(sorted(WORDNUM, key=len, reverse=True))


def as_int(tok):
	"""'12' -> 12, 'four' -> 4, else None."""
	if tok is None:
		return None
	t = tok.strip().lower()
	if t.isdigit():
		return int(t)
	return WORDNUM.get(t)


class Field:
	"""One extracted field: value(s), how often each was seen, and source spans."""
	__slots__ = ('counts', 'spans')

	def __init__(self):
		self.counts = OrderedDict()      # value -> times seen
		self.spans = OrderedDict()       # value -> first surrounding quote

	def add(self, value, span=None):
		v = clean(str(value))
		if not v:
			return self
		key = next((k for k in self.counts if k.lower() == v.lower()), v)
		self.counts[key] = self.counts.get(key, 0) + 1
		if span and key not in self.spans:
			self.spans[key] = clean(span)
		return self

	@property
	def values(self):
		"""Values ranked by how often the paper states them (most-reported first)."""
		return [v for v, _ in sorted(self.counts.items(), key=lambda kv: -kv[1])]

	def __bool__(self):
		return bool(self.counts)

	def text(self, sep=' | ', limit=6):
		return sep.join(self.values[:limit])

	def evidence(self, limit=3):
		return ' /// '.join(self.spans.get(v, '') for v in self.values[:limit] if self.spans.get(v))


# Physically plausible ranges for human fMRI.  A "TE" of 565 ms is a parsing
# accident, not an echo time; these gates drop such values before they reach a cell.
PLAUSIBLE = {
	'TR':          (lambda v, u: 0.3 <= v <= 6.0 if u == 's' else 300 <= v <= 6000),
	'TE':          (lambda v, u: 0.005 <= v <= 0.12 if u == 's' else 5 <= v <= 120),
	'Flip_Angle':  (lambda v, u: 1 <= v <= 180),
	'Voxel_Size':  (lambda v, u: 0.3 <= v <= 6.0),
	'Slice_Thickness': (lambda v, u: 0.3 <= v <= 10.0),
}


def plausible(field, value):
	"""False when a parsed number cannot be that acquisition parameter."""
	gate = PLAUSIBLE.get(field)
	if not gate:
		return True
	nums = re.findall(r'\d+(?:\.\d+)?', value)
	if not nums:
		return True
	unit = 'ms' if 'ms' in value else ('s' if re.search(r'\bs\b', value) else '')
	try:
		return all(gate(float(n), unit) for n in nums)
	except (TypeError, ValueError):
		return True


def find(text, patterns, group=0, post=None, window=200):
	"""Run patterns over text, collect group() values + surrounding context."""
	f = Field()
	for pat in patterns:
		for m in re.finditer(pat, text, re.I):
			try:
				raw = m.group(group) if m.lastindex or group == 0 else m.group(0)
			except (IndexError, error_types):
				raw = m.group(0)
			if raw is None:
				continue
			val = post(raw, m) if post else raw
			if val is None:
				continue
			a = max(0, m.start() - window // 2)
			b = min(len(text), m.end() + window // 2)
			f.add(val, text[a:b])
	return f


error_types = re.error


# --------------------------------------------------------------------------
# fMRI ACQUISITION
# --------------------------------------------------------------------------
def _sec_or_ms(raw, m):
	"""Normalise a TR/TE number + unit to a canonical string."""
	val, unit = m.group('v'), (m.group('u') or '').lower()
	v = float(val)
	if unit.startswith('ms') or (not unit and v > 50):
		return f"{v:g} ms"
	return f"{v:g} s"


ACQ_PATTERNS = OrderedDict([
	('Field_Strength', ([
		r'\b(?P<v>1\.5|3|4|7|9\.4|10\.5)\s*[- ]?\s*(?:T|Tesla)\b(?!\w)',
		r'\b(?P<v>1\.5|3|7)\s*[- ]?\s*T\s+(?:MRI|scanner|Siemens|Philips|GE|magnet)',
	], lambda raw, m: f"{m.group('v')}T")),

	('Scanner_Vendor_Model', ([
		r'\b(?:Siemens)\s+(?:MAGNETOM\s+)?(Prisma(?:\s*fit)?|Skyra|Trio(?:\s*Tim)?|TrioTim|Verio|Terra|Vida|Allegra|Avanto|Aera|Sola|Cima|Magnetom|Connectome)\b',
		r'\b(?:Philips)\s+(Achieva(?:\s*TX)?|Ingenia(?:\s*Elition)?|Intera|Elition)\b',
		r'\b(?:GE|General Electric)\s+(Signa(?:\s*(?:HDxt|Premier|Architect|Excite))?|Discovery\s*MR\d+|MR750w?|Premier)\b',
		r'\b(Siemens|Philips|General Electric|GE Healthcare|Bruker|United Imaging|Canon|Toshiba)\b(?=[^.]{0,60}(?:scanner|MRI|magnet|system|3\s?T|7\s?T))',
	], lambda raw, m: clean(m.group(0)))),

	('Head_Coil', ([
		r'\b(?P<v>\d{1,3})[- ]channel\s+(?:head|volume|receive|phased[- ]array|head[- ]?neck)?\s*(?:coil|head coil|array)',
		r'\bhead coil\b[^.]{0,40}?(?P<v>\d{1,3})[- ]channel',
	], lambda raw, m: f"{m.group('v')}-channel coil")),

	('TR', ([
		r'\b(?:TR|repetition\s+time)\s*(?:\(TR\))?\s*[=:of]{1,3}\s*(?P<v>' + NUM + r')\s*(?P<u>ms|msec|milliseconds?|s\b|sec|seconds?)?',
		r'\brepetition\s+time\s*\(\s*TR\s*\)\s*(?:was|of|=|:)\s*(?P<v>' + NUM + r')\s*(?P<u>ms|msec|s\b|sec|seconds?)?',
		r'\bTR\s*/\s*TE\s*=\s*(?P<v>' + NUM + r')\s*/\s*' + NUM + r'\s*(?P<u>ms|s\b)?',
	], _sec_or_ms)),

	('TE', ([
		r'\b(?:TE|echo\s+time)\s*(?:\(TE\))?\s*[=:of]{1,3}\s*(?P<v>' + NUM + r')\s*(?P<u>ms|msec|milliseconds?|s\b|sec)?',
		r'\becho\s+time\s*\(\s*TE\s*\)\s*(?:was|of|=|:)\s*(?P<v>' + NUM + r')\s*(?P<u>ms|msec|s\b)?',
		r'\bTR\s*/\s*TE\s*=\s*' + NUM + r'\s*/\s*(?P<v>' + NUM + r')\s*(?P<u>ms|s\b)?',
	], _sec_or_ms)),

	('Flip_Angle', ([
		r'\b(?:flip\s+angle|FA)\s*(?:\(FA\))?\s*[=:of]{1,3}\s*(?P<v>' + NUM + r')\s*(?:°|deg|degrees?)?',
		r'\b(?P<v>' + NUM + r')\s*(?:°|deg\b|degree)\s+flip\s+angle',
	], lambda raw, m: f"{m.group('v')}°")),

	('Voxel_Size', ([
		r'\b(?:voxel\s*(?:size|resolution|dimensions?)|spatial\s+resolution|isotropic\s+voxels?)\s*(?:of|was|were|=|:)?\s*'
		r'(?P<v>' + NUM + r'\s*(?:[x×*]\s*' + NUM + r'\s*){0,2}\s*mm(?:\s*3|³)?(?:\s*isotropic)?)',
		r'\b(?P<v>' + NUM + r'\s*[x×]\s*' + NUM + r'\s*[x×]\s*' + NUM + r'\s*mm\s*3?)\b',
		r'\b(?P<v>' + NUM + r'\s*mm\s*(?:isotropic|iso)\b)',
	], lambda raw, m: clean(m.group('v')))),

	('Slices', ([
		r'\b(?P<v>\d{1,3})\s+(?:axial|transverse|oblique|coronal|sagittal|interleaved|contiguous)?\s*slices\b',
		r'\bnumber\s+of\s+slices\s*[=:]\s*(?P<v>\d{1,3})',
		r'\bslices\s*[=:]\s*(?P<v>\d{1,3})',
	], lambda raw, m: f"{m.group('v')} slices")),

	('Slice_Thickness', ([
		r'\bslice\s+thickness\s*(?:of|was|=|:)?\s*(?P<v>' + NUM + r')\s*mm',
		r'\b(?P<v>' + NUM + r')\s*mm\s+(?:thick\s+)?slices?\b',
	], lambda raw, m: f"{m.group('v')} mm")),

	('Slice_Gap', ([
		r'\b(?:slice\s+)?gap\s*(?:of|was|=|:)?\s*(?P<v>' + NUM + r')\s*mm',
		r'\bno\s+(?:slice\s+)?gap\b',
	], lambda raw, m: 'no gap' if 'no' in m.group(0).lower()[:4] else f"{m.group('v')} mm gap")),

	('FOV', ([
		r'\b(?:FOV|field\s+of\s+view)\s*(?:\(FOV\))?\s*(?:of|was|=|:)\s*(?P<v>' + NUM + r'\s*(?:[x×]\s*' + NUM + r')?\s*(?:mm|cm)\s*2?)',
	], lambda raw, m: clean(m.group('v')))),

	('Matrix_Size', ([
		r'\b(?:matrix(?:\s+size)?|acquisition\s+matrix|image\s+matrix)\s*(?:of|was|=|:)\s*(?P<v>\d{2,4}\s*[x×]\s*\d{2,4})',
		r'\b(?P<v>(?:64|80|96|100|104|112|128|200|220|256)\s*[x×]\s*(?:64|80|96|100|104|112|128|200|220|256))\s+matrix',
	], lambda raw, m: clean(m.group('v')))),

	('Multiband_SMS', ([
		r'\b(?:multi[- ]?band|simultaneous\s+multi[- ]?slice|SMS|MB)\s*(?:factor|acceleration)?\s*(?:of|=|:)?\s*(?P<v>\d)\b',
		r'\bmulti[- ]?band\s+factor\s+(?P<v>\d)',
	], lambda raw, m: f"MB factor {m.group('v')}")),

	('Parallel_Imaging', ([
		r'\b(?:GRAPPA|iPAT|SENSE|ASSET|ARC)\s*(?:factor|acceleration)?\s*(?:of|=|:)?\s*(?P<v>\d(?:\.\d)?)\b',
		r'\b(?:in[- ]plane\s+acceleration)\s*(?:factor)?\s*(?:of|=|:)?\s*(?P<v>\d)',
	], lambda raw, m: clean(m.group(0)))),

	('Sequence', ([
		r'\b(gradient[- ]echo\s+echo[- ]planar\s+imaging|gradient\s+echo\s+EPI|GE[- ]EPI|T2\*[- ]weighted\s+(?:gradient[- ]echo\s+)?EPI|'
		r'echo[- ]planar\s+imaging|EPI\s+sequence|spin[- ]echo\s+EPI|SE[- ]EPI|MP-?RAGE|MPRAGE|MP2RAGE|'
		r'multi[- ]echo\s+EPI|multi[- ]?band\s+EPI|blipped[- ]CAIPI)\b',
	], lambda raw, m: clean(m.group(1)))),

	('Phase_Encoding', ([
		r'\bphase[- ]encod(?:ing|e)\s+(?:direction|dir)\s*(?:of|was|=|:)?\s*(?P<v>[APLRIS]{1,2}(?:\s*>>\s*[APLRIS]{1,2})?|anterior[- ]to[- ]posterior|posterior[- ]to[- ]anterior|LR|RL|AP|PA)',
		r'\b(?P<v>AP\s*/\s*PA|LR\s*/\s*RL)\s+phase[- ]encoding',
	], lambda raw, m: clean(m.group('v')))),

	('N_Volumes', ([
		r'\b(?P<v>\d{2,5})\s+(?:functional\s+)?(?:volumes|TRs|time\s*points|timepoints|frames|brain\s+volumes)\b',
		r'\b(?:volumes|TRs|time\s*points)\s*[=:]\s*(?P<v>\d{2,5})',
	], lambda raw, m: f"{m.group('v')} volumes/TRs")),

	('N_Runs_Sessions', ([
		r'\b(?P<v>\d{1,3})\s+(?:functional\s+)?(?:runs|sessions|scanning\s+sessions|scan\s+sessions|blocks)\b',
		r'\b(?:runs|sessions)\s*[=:]\s*(?P<v>\d{1,3})',
	], lambda raw, m: clean(m.group(0)))),

	('Scan_Duration', ([
		r'\b(?:scan(?:ning)?\s+(?:time|duration|length)|run\s+(?:duration|length)|session\s+(?:duration|lasted))\s*'
		r'(?:of|was|=|:)?\s*(?P<v>' + NUM + r'\s*(?:min(?:utes)?|s\b|sec(?:onds)?|h(?:ours)?))',
		r'\b(?:lasted|lasting)\s+(?:approximately\s+|about\s+)?(?P<v>' + NUM + r'\s*(?:min(?:utes)?|sec(?:onds)?|h(?:ours)?))',
	], lambda raw, m: clean(m.group('v')))),

	('N_Subjects', ([
		r'\b(?:N|n)\s*=\s*(?P<v>\d{1,5})\s*(?:subjects|participants|healthy|individuals|patients)?',
		r'\b(?P<v>\d{1,5})\s+(?:healthy\s+)?(?:human\s+)?(?:subjects|participants|volunteers|individuals|patients)\b',
		r'\bdata\s+from\s+(?P<v>\d{1,5})\s+(?:subjects|participants)',
	], lambda raw, m: clean(m.group(0)))),
])

PREPROC_PATTERNS = OrderedDict([
	('Preprocessing_Pipeline', ([
		r'\b(fMRIPrep|FMRIPREP|SPM\s*\d{1,2}|SPM12|SPM8|FSL\s*(?:FEAT|MELODIC)?|AFNI|FreeSurfer|Nilearn|'
		r'CONN\s+toolbox|DPARSF|DPABI|C-PAC|CPAC|HCP\s+minimal\s+preprocessing|ciftify|ANTs|BrainVoyager|'
		r'pypreprocess|nipype)\b',
	], lambda raw, m: clean(m.group(1)))),
	('Smoothing_FWHM', ([
		r'\b(?:smooth(?:ed|ing)?)[^.]{0,60}?(?P<v>' + NUM + r')\s*[- ]?mm\s*(?:FWHM|full[- ]width)',
		r'\bFWHM\s*(?:of|=|:)?\s*(?P<v>' + NUM + r')\s*mm',
		r'\b(?:no|without)\s+(?:spatial\s+)?smoothing\b',
	], lambda raw, m: 'no smoothing' if re.match(r'\b(no|without)', m.group(0), re.I) else f"{m.group('v')} mm FWHM")),
	('Atlas_Parcellation', ([
		r'\b(Schaefer(?:[- ]\d{3})?|Desikan[- ]?Killiany|Destrieux|AAL\s*3?|Automated\s+Anatomical\s+Labeling|'
		r'Craddock\s*\d*|Harvard[- ]Oxford|Power\s*264|Gordon\s*\d*|Glasser|HCP[- ]MMP1?|Brainnetome|'
		r'Dosenbach|Shen\s*268|Yeo\s*\d*|DiFuMo|NeuroMark|Julich|Talairach)\b',
	], lambda raw, m: clean(m.group(1)))),
	('Registration_Space', ([
		r'\b(MNI152(?:NLin\d{4}[a-zA-Z]*)?|MNI\s+space|fsaverage\d?|fsLR(?:32k)?|fs_?LR|native\s+space|'
		r'Talairach\s+space|CIFTI|grayordinate)\b',
	], lambda raw, m: clean(m.group(1)))),
	('Task_Paradigm', ([
		r'\b(resting[- ]state|rest[- ]fMRI|task[- ]based|block\s+design|event[- ]related|'
		r'naturalistic\s+(?:viewing|listening|stimuli)|movie[- ]watching|story\s+listening|'
		r'silent\s+(?:reading|naming|word\s+generation|verb\s+generation)|covert\s+(?:speech|naming|reading|articulation)|'
		r'inner\s+speech|imagined\s+speech|verbal\s+fluency|picture\s+naming|semantic\s+decision|'
		r'n[- ]back|working\s+memory\s+task|visual\s+imagery|mental\s+imagery)\b',
	], lambda raw, m: clean(m.group(1)).lower())),
])

# --------------------------------------------------------------------------
# MODEL ARCHITECTURE
# --------------------------------------------------------------------------
ARCH_PATTERNS = OrderedDict([
	('N_Transformer_Layers', ([
		r'\b(?P<v>\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|sixteen|twenty|twenty-four|thirty-two|forty|sixty-four)\s+(?:stacked\s+|consecutive\s+|cascaded\s+)?'
		r'(?:transformer|encoder|decoder|attention|self-?attention|ViT|Swin|conformer)\s+'
		r'(?:encoder\s+|decoder\s+)?(?:layers|blocks|modules|stages)\b',
		r'\b(?:cascade|stack|sequence|series)\s+of\s+(?P<v>\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|sixteen|twenty|twenty-four|thirty-two|forty|sixty-four)\s+'
		r'(?:transformer\s+|attention\s+|encoder\s+|decoder\s+)?(?:layers|blocks|modules)\b',
		r'\b(?:transformer|encoder|decoder|backbone|model)\s+'
		r'(?:has|with|of|consists?\s+of|comprises?|contains?|is\s+composed\s+of|uses?|employs?)\s+'
		r'(?P<v>\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|sixteen|twenty|twenty-four|thirty-two|forty|sixty-four)\s+(?:transformer\s+)?(?:layers|blocks|modules)\b',
		r'\b(?:number\s+of\s+)?(?:transformer\s+|encoder\s+|hidden\s+)?layers?\s*'
		r'(?:is|was|are|were|=|:)\s*(?P<v>\d{1,3})\b',
		r'\b(?:num_?layers|n_?layers|depth|num_?blocks|n_?blocks)\s*[=:]\s*(?P<v>\d{1,3})\b',
		r'\bdepth\s+(?:of|is|was|=|:)\s*(?P<v>\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|sixteen|twenty|twenty-four|thirty-two|forty|sixty-four)\b',
		r'\b(?P<v>\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|sixteen|twenty|twenty-four|thirty-two|forty|sixty-four)[- ]layer(?:ed)?\s+'
		r'(?:transformer|encoder|decoder|ViT|BERT|GPT|network|architecture|MLP)\b',
		r'\bL\s*=\s*(?P<v>\d{1,3})\s*(?:transformer\s+)?layers?\b',
		r'\b\|\s*(?:#\s*)?(?:layers|depth|blocks)\s*\|\s*(?P<v>\d{1,3})\s*\|',
		r'\b(?:layer|block|stage)\s+numbers?\s*(?:of|is|was|=|:)\s*(?P<v>\d{1,3})\b',
	], lambda raw, m: (lambda n: f"{n} layers" if n else None)(as_int(m.group('v'))))),

	('Layer_Depths_Per_Stage', ([
		r'\bnumbers?\s+of\s+layers?\b[^.]{0,60}?\{\s*(?P<v>\d{1,2}(?:\s*,\s*\d{1,2}){1,5})\s*\}',
		r'\b(?:depths?|stage\s+depths?|layers?\s+per\s+stage)\s*(?:=|:|of)\s*[\{\[]\s*(?P<v>\d{1,2}(?:\s*,\s*\d{1,2}){1,5})\s*[\}\]]',
		r'\bL\s*1\s*,\s*L\s*2[^=]{0,20}=\s*\{\s*(?P<v>\d{1,2}(?:\s*,\s*\d{1,2}){1,5})\s*\}',
	], lambda raw, m: '{' + clean(m.group('v')) + '} blocks per stage')),

	('N_Attention_Heads', ([
		r'\b(?P<v>\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|sixteen|twenty|twenty-four|thirty-two|forty|sixty-four)\s+(?:parallel\s+)?'
		r'(?:attention|self-?attention|cross-?attention|multi-?head)\s+heads\b',
		r'\b(?:number\s+of\s+)?(?:attention\s+)?heads?\s*(?:is|was|are|were|=|:)\s*(?P<v>\d{1,3})\b',
		r'\b(?:num_?heads|n_?heads|nhead|num_?attention_?heads)\s*[=:]\s*(?P<v>\d{1,3})\b',
		r'\b(?:multi-?head\s+)?(?:self-?|cross-?)?attention\s+with\s+(?P<v>\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|sixteen|twenty|twenty-four|thirty-two|forty|sixty-four)\s+heads\b',
		r'\bh\s*=\s*(?P<v>\d{1,2})\s+(?:attention\s+)?heads\b',
		r'\b(?P<v>\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|sixteen|twenty|twenty-four|thirty-two|forty|sixty-four)[- ]head(?:ed|s)?\s+'
		r'(?:self-?attention|cross-?attention|multi-?head|attention|MSA|MHA|MHSA)\b',
		r'\b(?:with|using|of)\s+(?P<v>\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|sixteen|twenty|twenty-four|thirty-two|forty|sixty-four)\s+heads\b',
		r'\b\|\s*(?:#\s*)?(?:heads|attention heads)\s*\|\s*(?P<v>\d{1,3})\s*\|',
		r'\bhead\s+numbers?\s*(?:of|is|was|=|:)\s*(?P<v>\d{1,3})\b',
		r'\bthe\s+(?:cross-?attention|self-?attention|attention)\s+layers?\s+with\s+(?P<v>\d{1,3}|WORDS)[- ]head'.replace('WORDS', WORDNUM_RX),
	], lambda raw, m: (lambda n: f"{n} heads" if n else None)(as_int(m.group('v'))))),

	('Hidden_Embed_Dim', ([
		r'\b(?:hidden|embedding|embed|latent|feature|model|token|channel|output)\s+'
		r'(?:dimension(?:ality)?|dim|size|width|number)\s*(?:is|was|are|were|of|set\s+to|=|:)\s*(?P<v>\d{2,5})\b',
		r'\bd_?(?:model|emb|embed|hidden|h)\s*[=:]\s*(?P<v>\d{2,5})\b',
		r'\b(?:hidden_?size|hidden_?dim|embed_?dim|embedding_?dim|d_model|n_?embd|width)\s*[=:]\s*(?P<v>\d{2,5})\b',
		r'\bdimension(?:ality)?\s+of\s+(?:the\s+)?(?:hidden|embedding|latent|feature|token)\s+'
		r'(?:state|space|vector|representation)s?\s*(?:is|was|=|:|of)?\s*(?P<v>\d{2,5})\b',
		r'\b(?:embedding|hidden|latent|feature)\s+(?:size|dimension(?:ality)?)\s+of\s+(?P<v>\d{2,5})\b',
		r'\bC\s*=\s*(?P<v>\d{1,4})\b(?=[^.]{0,60}channel)',
		r'\bchannel\s+numbers?\s+of\s+C\s*=\s*(?P<v>\d{1,4})\b',
		r'\bchannel\s+numbers?\s*(?:of|is|was|=|:)\s*(?P<v>\d{1,4})\b',
		r'\b\|\s*(?:hidden(?:\s+dim\w*)?|d_model|embedding\s+dim\w*)\s*\|\s*(?P<v>\d{2,5})\s*\|',
	], lambda raw, m: f"dim {m.group('v')}")),

	('FFN_MLP_Dim', ([
		r'\b(?:feed[- ]?forward|FFN|MLP)\s+(?:hidden\s+)?(?:dimension|dim|size|width|layer)\s*(?:is|was|of|=|:)\s*(?P<v>\d{2,5})',
		r'\bMLP\s+ratio\s*(?:of|=|:)?\s*(?P<v>' + NUM + r')',
		r'\bexpansion\s+(?:factor|ratio)\s*(?:of|=|:)?\s*(?P<v>' + NUM + r')',
	], lambda raw, m: clean(m.group(0)))),

	('Patch_Token_Size', ([
		r'\bpatch\s+size\s*(?:of|is|was|=|:)?\s*(?P<v>\d{1,3}\s*(?:[x×]\s*\d{1,3}){0,3})',
		r'\b(?P<v>\d{1,2}\s*[x×]\s*\d{1,2}(?:\s*[x×]\s*\d{1,2})?)\s+patches\b',
		r'\bpatches?\s+of\s+size\s+(?P<v>\d{1,3}\s*(?:[x×]\s*\d{1,3})+)',
		r'\bwindow\s+size\s*(?:of|is|was|=|:)?\s*(?P<v>\d{1,3}\s*(?:[x×]\s*\d{1,3}){0,3})',
		r'\b(?:sequence|context|token)\s+length\s*(?:of|is|was|=|:)?\s*(?P<v>\d{1,5})',
		r'\bT\s*=\s*(?P<v>\d{1,4})\s*TRs?\b',
	], lambda raw, m: clean(m.group(0)))),

	('Attention_Variant', ([
		r'\b(multi[- ]head\s+self[- ]attention|multi[- ]head\s+cross[- ]attention|multi[- ]head\s+attention|'
		r'self[- ]attention|cross[- ]attention|co[- ]attention|windowed\s+attention|shifted\s+window\s+attention|'
		r'fused\s+window\s+attention|sparse\s+attention|linear\s+attention|deformable\s+attention|axial\s+attention|'
		r'spatial[- ]temporal\s+attention|spatiotemporal\s+attention|channel\s+attention|graph\s+attention|'
		r'hypergraph\s+attention|masked\s+(?:multi[- ]head\s+)?attention|flash\s?attention|'
		r'scaled\s+dot[- ]product\s+attention|additive\s+attention|gated\s+attention|attention\s+pooling|'
		r'temporal\s+attention|spatial\s+attention|region[- ]aware\s+attention|memory[- ]efficient\s+attention)\b',
	], lambda raw, m: clean(m.group(1)).lower())),

	('Positional_Encoding', ([
		r'\b(sinusoidal\s+(?:positional\s+)?(?:encoding|embedding)s?|learnable\s+positional\s+(?:encoding|embedding)s?|'
		r'learned\s+positional\s+(?:encoding|embedding)s?|absolute\s+positional\s+(?:encoding|embedding)s?|'
		r'relative\s+positional\s+(?:encoding|embedding|bias)s?|rotary\s+(?:positional\s+)?embeddings?|RoPE|ALiBi|'
		r'gradient\s+positioning|positional\s+(?:encoding|embedding)s?|position\s+(?:encoding|embedding)s?)\b',
	], lambda raw, m: clean(m.group(1)).lower())),

	('Normalization', ([
		r'\b(layer\s*norm(?:alization|alisation)?|LayerNorm|LN\b|batch\s*norm(?:alization|alisation)?|BatchNorm|'
		r'instance\s*norm(?:alization)?|group\s*norm(?:alization)?|RMSNorm|pre[- ]norm|post[- ]norm)\b',
	], lambda raw, m: clean(m.group(1)))),

	('Activation', ([
		r'\b(GELU|ReLU|LeakyReLU|Leaky\s+ReLU|SiLU|Swish|ELU|Tanh|Sigmoid|Softmax|SwiGLU|GEGLU|Mish|PReLU)\b',
	], lambda raw, m: clean(m.group(1)))),

	('Dropout', ([
		r'\bdropout\s*(?:rate|probability|ratio|of|=|:|with)?\s*(?:of|=|:)?\s*(?P<v>0?\.\d+|\d{1,2}\s*%)',
		r'\b(?P<v>0?\.\d+)\s+dropout\b',
		r'\bdrop[- ]?path\s*(?:rate)?\s*(?:of|=|:)?\s*(?P<v>0?\.\d+)',
	], lambda raw, m: f"dropout {m.group('v')}")),

	('Pooling', ([
		r'\b(global\s+average\s+pooling|global\s+max\s+pooling|average\s+pooling|mean\s+pooling|max\s+pooling|'
		r'attention\s+pooling|CLS\s+token|class\s+token|\[CLS\]|GAP\b|adaptive\s+average\s+pooling|'
		r'READOUT|readout\s+(?:layer|token)|SUM\s?POOL|concat(?:enation)?\s+pooling|'
		r'temporal\s+pooling|spatial\s+pooling|hierarchical\s+pooling|SortPool|DiffPool|TopK\s+pooling)\b',
	], lambda raw, m: clean(m.group(1)))),

	('Conv_GNN_Components', ([
		r'\b(1D\s+convolution(?:al)?|2D\s+convolution(?:al)?|3D\s+convolution(?:al)?|temporal\s+convolution|'
		r'depthwise\s+separable\s+convolution|dilated\s+convolution|ResNet-?\d*|U-?Net|VGG-?\d*|Inception|'
		r'graph\s+convolution(?:al)?(?:\s+network)?|GCN\b|GAT\b|GraphSAGE|GIN\b|BiLSTM|Bi-?LSTM|LSTM|GRU|'
		r'bidirectional\s+LSTM|TCN\b|EfficientNet|DenseNet|MobileNet)\b',
	], lambda raw, m: clean(m.group(1)))),

	('Pretrained_Backbone', ([
		r'\b(CLIP[- ]?ViT[- ]?[BLHg]/?\d*|ViT[- ]?[BLHgS]/?\d*|CLIP\b|OpenCLIP|BLIP-?2?|LLaVA(?:-?[\d.]+)?(?:-\w+)*|'
		r'GPT-?2(?:-\w+)?|GPT-?3(?:\.5)?|GPT-?4[ov]?|LLaMA-?[\d.]*(?:-\w+)?|Llama\s?[\d.]+|Vicuna(?:-?\d+[Bb])?|'
		r'Mistral(?:-\w+)?|BERT(?:-base|-large)?(?:-\w+)?|RoBERTa(?:-\w+)?|DeBERTa|ALBERT|DistilBERT|'
		r'T5(?:-\w+)?|FLAN-?T5|BART(?:-\w+)?|OPT-?[\d.]+[Bb]?|Whisper(?:-\w+)?|wav2vec\s?2\.0|HuBERT|'
		r'Stable\s+Diffusion(?:\s*(?:v?[\d.]+|XL))?|Versatile\s+Diffusion|unCLIP|DALL-?E\s?\d?|'
		r'DINOv2|MAE\b|SwinT?(?:-\w+)?|BEiT|EVA-?CLIP|SigLIP|Qwen[\d.-]*(?:VL)?|InternVL|Flamingo|'
		r'MusicLM|MuLan|AudioLDM|Imagen|IP-?Adapter|ControlNet)\b',
	], lambda raw, m: clean(m.group(1)))),

	('Frozen_Finetuned', ([
		r'\b(frozen|freeze|kept\s+fixed|not\s+updated|fine[- ]?tun(?:e|ed|ing)|end[- ]to[- ]end\s+training|'
		r'LoRA|low[- ]rank\s+adaptation|prefix[- ]tuning|soft\s+prompt|prompt\s+tuning|adapter\s+layers?|'
		r'linear\s+prob(?:e|ing)|train(?:ed)?\s+from\s+scratch|zero[- ]shot|parameter[- ]efficient)\b',
	], lambda raw, m: clean(m.group(1)).lower())),

	('Loss_Functions', ([
		r'\b(InfoNCE|NT-?Xent|contrastive\s+loss|CLIP\s+loss|triplet\s+loss|cross[- ]entropy(?:\s+loss)?|'
		r'mean\s+squared\s+error|MSE(?:\s+loss)?|L1\s+loss|L2\s+loss|smooth\s+L1|Huber\s+loss|'
		r'cosine\s+(?:similarity|embedding)\s+loss|KL\s+divergence|Kullback[- ]Leibler|'
		r'adversarial\s+loss|GAN\s+loss|reconstruction\s+loss|diffusion\s+loss|denoising\s+loss|'
		r'BiMixCo|SoftCLIP|MixCo|focal\s+loss|dice\s+loss|negative\s+log[- ]likelihood|NLL|'
		r'binary\s+cross[- ]entropy|BCE|masked\s+(?:reconstruction|prediction)\s+loss|perceptual\s+loss)\b',
	], lambda raw, m: clean(m.group(1)))),

	('Optimizer', ([
		r'\b(AdamW|Adam\b|SGD|RMSProp|Adagrad|Adadelta|Adafactor|LAMB|Lion|Ranger|Nadam)\b',
	], lambda raw, m: clean(m.group(1)))),

	('Learning_Rate', ([
		# 2e-4 / 1e−4 / 0.0001 / 2 × 10-4 / 3 · 10^{-5}
		# handles: 1e-4 | 0.0001 | 2 x 10^-4 | 2x10-4 | 10-4 (superscript lost by pdf2txt)
		r'\b(?:learning\s+rate|lr)\b[^.;\n]{0,40}?'
		r'(?P<v>\d+(?:\.\d+)?\s*(?:[x×*·]\s*)?10\s*(?:\^|\*\*)?\s*\{?\s*[-−–]\s*\d+\s*\}?'
		r'|\d+(?:\.\d+)?\s*[eE]\s*[-−–]\s*\d+'
		r'|0\.\d{3,8})',
		r'\blr\s*=\s*(?P<v>\d+(?:\.\d+)?(?:[eE][-−–]?\d+)?)',
	], lambda raw, m: 'lr ' + clean(m.group('v')).replace('−','-'))),

	('LR_Schedule', ([
		r'\b(cosine\s+(?:annealing|decay|schedule)|linear\s+(?:decay|warm-?up|schedule)|step\s+decay|'
		r'OneCycle|one[- ]cycle|warm-?up|ReduceLROnPlateau|exponential\s+decay|polynomial\s+decay|'
		r'constant\s+learning\s+rate)\b',
	], lambda raw, m: clean(m.group(1)).lower())),

	('Weight_Decay', ([
		r'\bweight\s+decay\s*(?:of|is|was|=|:)?\s*(?P<v>[\d.e-]+)',
		r'\bL2\s+regular(?:ization|isation)\s*(?:of|=|:)?\s*(?P<v>[\d.e-]+)',
	], lambda raw, m: f"weight decay {clean(m.group('v'))}")),

	('Batch_Size', ([
		r'\bbatch\s+size\s*(?:of|is|was|set\s+to|=|:)?\s*(?P<v>\d{1,5})',
		r'\bmini[- ]?batch(?:es)?\s+of\s+(?P<v>\d{1,5})',
		r'\bbatch\s*=\s*(?P<v>\d{1,5})',
	], lambda raw, m: f"batch {m.group('v')}")),

	('Epochs_Steps', ([
		r'\b(?:for\s+)?(?P<v>\d{1,6})\s+epochs\b',
		r'\bepochs?\s*(?:is|was|=|:)\s*(?P<v>\d{1,6})',
		r'\b(?P<v>\d{1,7})\s+(?:training\s+)?(?:steps|iterations|updates)\b',
	], lambda raw, m: clean(m.group(0)))),

	('Params_Count', ([
		r'\b(?P<v>[\d.]+\s*[MBK]?)\s*(?:million|billion)?\s+(?:trainable\s+|learnable\s+|model\s+)?parameters\b',
		r'\bparameters?\s*(?:count|:|=)\s*(?P<v>[\d.,]+\s*[MBK]?)',
		r'\b(?P<v>\d+(?:\.\d+)?[MB])\s+params\b',
	], lambda raw, m: clean(m.group(0)))),

	('Hardware', ([
		r'\b(NVIDIA\s+\w+[\w -]*|A100(?:\s*\d*\s*GB)?|H100|V100|RTX\s*\d{3,4}\s*(?:Ti|Super)?|GTX\s*\d{3,4}|'
		r'Tesla\s+[PVAK]\d{2,3}|TPU(?:\s*v\d)?|L40S?|A6000|3090|4090|Titan\s+X[Pp]?)\b',
	], lambda raw, m: clean(m.group(1)))),

	('Augmentation_Regularization', ([
		r'\b(MixUp|Mixup|CutMix|masking\s+ratio|random\s+masking|noise\s+injection|Gaussian\s+noise|'
		r'label\s+smoothing|early\s+stopping|gradient\s+clipping|stochastic\s+depth|data\s+augmentation|'
		r'temporal\s+jitter(?:ing)?|random\s+crop(?:ping)?|dropout\s+regular(?:ization|isation)|'
		r'weight\s+averaging|EMA\b|ensembl(?:e|ing))\b',
	], lambda raw, m: clean(m.group(1)))),

	('Validation_Scheme', ([
		r'\b(\d{1,2}[- ]fold\s+cross[- ]validation|k[- ]fold\s+cross[- ]validation|leave[- ]one[- ](?:subject|run|session|out)[- ]out|'
		r'LOSO|nested\s+cross[- ]validation|stratified\s+(?:k[- ]fold|split)|hold[- ]?out|'
		r'train/?val(?:idation)?/?test\s+split|cross[- ]subject|within[- ]subject|repeated\s+cross[- ]validation)\b',
	], lambda raw, m: clean(m.group(1)).lower())),

	('Code_URL', ([
		r'(https?://(?:www\.)?(?:github\.com|gitlab\.com|codeberg\.org|huggingface\.co|osf\.io|zenodo\.org)/[^\s)\]\},;"\']+)',
	], lambda raw, m: clean(m.group(1)).rstrip('.,;')))
])


def extract_block(text, patterns):
	"""Run a whole OrderedDict of (patterns, post) over text, dropping implausible values."""
	out = OrderedDict()
	for name, (pats, post) in patterns.items():
		f = find(text, pats, post=post)
		if name in PLAUSIBLE:
			keep = Field()
			for v in f.values:
				if plausible(name, v):
					keep.counts[v] = f.counts[v]
					if v in f.spans:
						keep.spans[v] = f.spans[v]
			f = keep
		out[name] = f
	return out
