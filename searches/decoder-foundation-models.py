"""
Surface the papers the yes / no inner-speech project needs for its MODEL
side -- building or borrowing a brain foundation model , adapting it to one
dense single-participant dataset , and keeping it accurate across sessions.

Companion to research/decoder-gaps.md : that file is the hand-checked list ,
this is the net that keeps catching new ones every ` prma missing ` ( the
OpenAlex pool of papers you cite / that cite you ) and ` prma search ` run.

Predicate groups :

  FMRI          -- every search requires it : the project only takes papers
                   that involve fMRI ( bimodal EEG-fMRI etc. is fine ; EEG- or
                   MEG-only is not ). "bold" is pinned to threshold = 100 so it
                   doesn't fuzz into "hold".

  FOUNDATION    -- large-scale / self-supervised pretraining vocabulary.

  ADAPT         -- how a pretrained model gets bent to a new subject or
                   task : fine-tuning , PEFT / adapters , few-shot , TTA.
                   Never bare "lora" -- it is a literal substring of
                   "exploration".

  DRIFT         -- session-to-session non-stationarity and recalibration.

  SINGLE_TRIAL  -- per-trial response estimation ( LSS , GLMsingle , HRF
                   fitting ) , which is what Aim 2's beta maps rest on.

  LEAKAGE       -- validation pitfalls that inflate decoding accuracy ,
                   worst for autocorrelated single-subject time series.

  TS_FM         -- general time-series foundation models ( Chronos ,
                   TimesFM , Moirai , ... ) that could be pointed at BOLD.
"""


def search():

	FMRI = combine_or(
		any_of(
			"fmri" , "functional mri" , "functional magnetic resonance" ,
			"blood oxygen level dependent" , "blood-oxygen-level" ,
		) ,
		any_of( "bold" , threshold=100 ) ,
	)

	FOUNDATION = any_of(
		"foundation model" , "brain foundation" ,
		"self-supervised pretraining" , "self-supervised pre-training" ,
		"large-scale pretraining" , "large-scale pre-training" ,
		"pretrained on" , "pre-trained on" ,
		"masked autoencoder" , "masked signal modeling" , "masked modeling" ,
		"joint-embedding predictive" , "neural tokenizer" ,
	)

	LLM = any_of(
		"large language model" , "language model" ,
		"multimodal language model" , "vision-language model" ,
	)

	ADAPT = any_of(
		"fine-tuning" , "fine tuning" , "finetuning" ,
		"parameter-efficient" , "parameter efficient" ,
		"low-rank adaptation" , "lora adapter" , "adapter" ,
		"prompt tuning" , "prefix tuning" , "linear probing" ,
		"few-shot" , "few shot" , "zero-shot" , "zero shot" ,
		"transfer learning" , "domain adaptation" ,
		"test-time adaptation" , "continual learning" ,
		"subject-specific" , "calibration-free" ,
	)

	DRIFT = any_of(
		"session drift" , "cross-session" , "inter-session" , "across sessions" ,
		"non-stationarity" , "nonstationarity" , "nonstationary" ,
		"day-to-day variability" , "recalibration" , "signal drift" ,
		"test-time adaptation" , "domain shift" , "distribution shift" ,
	)

	DECODE = any_of(
		"decoding" , "decoder" , "brain-computer interface" ,
		"brain computer interface" , "neuroprosthesis" , "brain-to-text" ,
		"classification" , "classifier" ,
	)

	SPEECH = any_of(
		"speech" , "inner speech" , "imagined speech" , "covert speech" ,
		"language" , "words" , "sentences" , "yes/no" , "yes or no" ,
	)

	SINGLE_TRIAL = any_of(
		"single-trial" , "single trial" , "trial-wise" , "trialwise" ,
		"beta series" , "least squares separate" , "least-squares separate" ,
		"glmsingle" , "glmdenoise" , "hrf estimation" ,
		"hemodynamic response function" , "haemodynamic response function" ,
		"rapid event-related" ,
	)

	LEAKAGE = any_of(
		"data leakage" , "information leakage" , "circular analysis" ,
		"double dipping" , "inflated accuracy" , "overestimate" ,
		"overoptimistic" , "cross-validation scheme" , "temporal autocorrelation" ,
	)

	TS_FM = combine_or(
		any_of(
			"time series foundation model" , "time-series foundation model" ,
			"lag-llama" ,
		) ,
		any_of( "timesfm" , "moirai" , "chronos-bolt" , threshold=100 ) ,
	)

	return [
		( "Brain foundation models" ,
			combine_and( FOUNDATION , FMRI ) ) ,
		( "Foundation model + adaptation for decoding" ,
			combine_and( FOUNDATION , ADAPT , DECODE , FMRI ) ) ,
		( "Pretrained / LLM speech-language decoding" ,
			combine_and( combine_or( FOUNDATION , LLM ) , SPEECH , DECODE , FMRI ) ) ,
		( "Session drift + decoding" ,
			combine_and( DRIFT , DECODE , FMRI ) ) ,
		( "Single-trial fMRI estimation" ,
			combine_and( SINGLE_TRIAL , FMRI ) ) ,
		( "Decoding leakage / validation pitfalls" ,
			combine_and( LEAKAGE , DECODE , FMRI ) ) ,
		( "Time-series foundation models on fMRI" ,
			combine_and( TS_FM , FMRI ) ) ,
	]
