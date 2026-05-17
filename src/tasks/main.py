def main( args ):
	from ..utils import utils
	from . import snapshot
	from pathlib import Path
	from ..openalex.openalex import OpenAlex
	# from ..openalex.search import any_of , all_of , combine_and , combine_or , none_of , fuzzy_has , normalize , weighted_any , at_least

	# 0.) Prep
	cache_dir = args.output.joinpath( "cache" )
	cache_dir.mkdir( parents=True , exist_ok=True )

	# 1.) Get Snapshot
	snapshot = snapshot.get_common( args )

	# 2.) Update OpenAlex Cache
	oa = OpenAlex( args )
	oa.update_cache( snapshot )

	# 3.) Stats
	oa_index = oa.Stats.compute( snapshot )

	searches = utils.load_searches( args.searches )
	oa.search( oa_index , searches )

	# 4.) Searches
	# oa.search( oa_index , [
	# 	( "Inner speech (any)" , any_of( "inner speech" , "imagined speech" , "covert speech" , "silent speech" , "subvocalized speech" , "inner monologue" , "imagined phonemes" , "silent communication" , "covert articulation" ) ),
	# 	( "fMRI + inner speech" , combine_and( all_of( "fMRI" , "speech" ) , any_of( "inner" , "imagined" , "covert" ) ) ) ,
	# ])

	# --- Concept groups (built once, reused) ---
	# INNER_SPEECH = any_of(
	# 	"inner speech", "imagined speech", "covert speech", "silent speech",
	# 	"subvocalized speech", "subvocal speech", "inner monologue", "inner dialogue",
	# 	"imagined phonemes", "imagined phoneme", "covert articulation",
	# 	"silent communication", "private speech", "self-talk", "self talk",
	# 	"covert articulatory", "imagined hearing", "imagined word",
	# )

	# FMRI = any_of("fmri", "functional mri", "functional magnetic resonance", "bold signal")

	# # Catches studies that use inner-speech-adjacent constructs even if they
	# # don't use the exact phrase (auditory hallucinations, speech arrest, CMD)
	# ADJACENT = any_of(
	# 	"auditory verbal hallucination", "alien voices",
	# 	"intraoperative speech arrest", "speech arrest",
	# 	"cognitive motor dissociation", "comatose", "covert command",
	# )

	# # Decode / classifier / paradigm angle
	# DECODING = any_of(
	# 	"decoding", "classifier", "classification", "prediction", "regression",
	# 	"brain-computer interface", "bci", "neural decoding", "speech bci",
	# )

	# PARADIGM = any_of(
	# 	"paradigm", "block design", "task", "experiment", "procedure",
	# 	"monosyllabic prompt", "monosyllabic", "phoneme production",
	# )

	# # Negative filter: drop pure overt-production / lip-reading / acoustic studies
	# NOT_OVERT = none_of(
	# 	"overt speech production", "lip reading", "lipreading",
	# 	"articulatory kinematics", "acoustic analysis of speech",
	# )

	# oa.search( oa_index , [
	# 	# 1. Broad concept hit — anything inner-speech-ish
	# 	("Inner speech (any)", INNER_SPEECH),

	# 	# 2. Core target: inner speech AND fMRI, excluding overt-production noise
	# 	("fMRI + inner speech", combine_and(INNER_SPEECH, FMRI, NOT_OVERT)),

	# 	# 3. fMRI inner-speech with a decoding/classifier angle (BCI-relevant)
	# 	("fMRI + inner speech + decoding",
	# 	combine_and(INNER_SPEECH, FMRI, DECODING, NOT_OVERT)),

	# 	# 4. fMRI inner-speech that is clearly an experimental paradigm
	# 	("fMRI + inner speech + paradigm",
	# 	combine_and(INNER_SPEECH, FMRI, PARADIGM, NOT_OVERT)),

	# 	# 5. Wider net: fMRI + (inner speech OR adjacent constructs),
	# 	#    requiring at least 2 of {inner-speech, adjacent, decoding} to cut noise
	# 	("fMRI + inner-speech-related (wide)",
	# 	combine_and(FMRI, at_least(2, INNER_SPEECH, ADJACENT, DECODING), NOT_OVERT)),

	# 	# 6. Adjacent-only (hallucinations / CMD / speech arrest) + fMRI
	# 	("fMRI + adjacent constructs", combine_and(ADJACENT, FMRI, NOT_OVERT)),
	# ])