"""
The papers Aim 3 can borrow from : modern fMRI DECODERS -- transformers ,
state-space models ( Mamba / S4 / SSM ) , hybrids and fine-tuned foundation
models -- that end in a DECISION ( a class , a brain / task state , a trial
label ) rather than a map or a reconstruction.

Kept apart from decoder-foundation-models.py so ` prma missing ` gives these
their own sheets. Classical MVPA / GLM-only work is deliberately out : the
architecture group has to fire. The inner-speech vocabulary comes from
research/Synonyms.md ( its psycholinguistic , neuroimaging-task and BCI
sections ).

Predicate groups :

  FMRI        -- the recording has to be fMRI ( "bold" pinned to 100 ).
  ARCH        -- the model family : attention / transformer , state-space ,
                 hybrid , pretrained-then-fine-tuned. "mamba" , "s4" and "ssm"
                 are pinned to 100.
  DECISION    -- the model resolves to a label : decoding , classification ,
                 brain / task / cognitive state , single-trial.
  STATE       -- task-state / brain-state decoding vocabulary specifically.
  INNER       -- inner / covert / imagined speech , from Synonyms.md.
  CLINICAL    -- disorder diagnosis ; split out because subject-level
                 diagnosis transfers less to trial-level yes / no.
"""


def search():

	FMRI = combine_or(
		any_of( "fmri" , "functional mri" , "functional magnetic resonance" ,
			"blood oxygen level dependent" , "blood-oxygen-level" ) ,
		any_of( "bold" , threshold=100 ) ,
	)

	ARCH = combine_or(
		any_of(
			"transformer" , "self-attention" , "self attention" , "multi-head attention" ,
			"attention mechanism" , "attention-based" , "cross-attention" ,
			"vision transformer" , "swin" , "perceiver" ,
			"state space model" , "state-space model" , "state space" , "selective scan" ,
			"structured state space" , "linear recurrence" ,
			"foundation model" , "pretrained model" , "pre-trained model" ,
			"fine-tuning" , "fine tuning" , "finetuning" , "low-rank adaptation" ,
			"masked autoencoder" , "self-supervised" , "contrastive learning" ,
			"temporal convolutional network" , "spatiotemporal transformer" ,
			"spatio-temporal transformer" , "mixture of experts" ,
		) ,
		any_of( "mamba" , "s4" , "ssm" , "vit" , "jamba" , "lora" , threshold=100 ) ,
	)

	DECISION = any_of(
		"decoding" , "decoder" , "classification" , "classifier" , "classify" ,
		"brain state" , "brain-state" , "cognitive state" , "task state" ,
		"task-state" , "mental state" , "single-trial" , "single trial" ,
		"trial-level" , "trial-wise" , "recognition" , "discriminat" ,
		"binary" , "yes/no" , "yes or no" ,
	)

	STATE = any_of(
		"brain state decoding" , "brain-state decoding" , "cognitive state decoding" ,
		"task state decoding" , "task-state decoding" , "task decoding" ,
		"mental state decoding" , "cognitive task classification" ,
		"task fmri classification" , "task-fmri classification" ,
		"brain decoding" , "neural decoding" ,
	)

	INNER = any_of(
		"inner speech" , "covert speech" , "silent speech" , "imagined speech" ,
		"subvocal" , "sub-vocal" , "subvocalization" , "covert articulation" ,
		"silent articulation" , "articulatory imagery" , "imagined articulation" ,
		"speech imagery" , "speech motor imagery" , "auditory verbal imagery" ,
		"phonological imagery" , "inner voice" , "internal speech" ,
		"inner monologue" , "internal monologue" , "inner dialogue" ,
		"verbal thought" , "covert naming" , "silent naming" ,
		"covert verb generation" , "silent verb generation" ,
		"silent word generation" , "covert language production" ,
		"internal answering" , "mental verbal response" , "imagined verbal response" ,
		"silent speech interface" , "speech imagery classification" ,
		"internal speech classification" , "neural speech prosthesis" ,
	)

	CLINICAL = combine_or(
		any_of( "autism" , "alzheimer" , "schizophrenia" , "depression" ,
			"major depressive" , "parkinson" , "mild cognitive impairment" ,
			"disorder diagnosis" , "brain disorder" , "psychiatric" ) ,
		any_of( "asd" , "adhd" , "mci" , threshold=100 ) ,
	)
	NOT_CLINICAL = lambda h: not CLINICAL( h )

	return [
		( "Aim 3: fMRI transformer / SSM decoders (non-clinical)" ,
			combine_and( FMRI , ARCH , DECISION , NOT_CLINICAL ) ) ,
		( "Aim 3: fMRI task / brain-state decoding with deep models" ,
			combine_and( FMRI , STATE , ARCH ) ) ,
		( "Aim 3: inner / covert speech + fMRI + deep model" ,
			combine_and( FMRI , INNER , combine_or( ARCH , DECISION ) ) ) ,
		( "Aim 3: fMRI transformer / SSM classifiers (clinical, lower priority)" ,
			combine_and( FMRI , ARCH , DECISION , CLINICAL ) ) ,
	]
