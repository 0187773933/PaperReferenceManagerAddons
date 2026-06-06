"""
Surface papers that use machine-learning / deep-learning methods --
especially for time-series / sequence data.

Predicate groups :

  ARCHITECTURES_LONG  -- spelled-out architecture names ( "convolutional
                         neural network" , "long short-term memory" , ... ) .
                         Safe at the default fuzz threshold ( 80 ) .

  ACRONYMS_EXACT      -- short architecture acronyms ( "cnn" , "rnn" ,
                         "lstm" , "gru" , "gan" , "vae" , ... ) . Pinned
                         to threshold = 100 so we only fire on a literal
                         substring -- 3-letter fuzzy matches at 80 will
                         hit way too much body prose ( "knn" inside
                         "unknown" , etc. ) .

  TIME_SERIES         -- generic time-series / sequence vocabulary that
                         signals the paper actually works on time-series
                         data , not just mentions one of these
                         architectures in passing.

  TS_ARCHITECTURES    -- modern time-series-specific architectures
                         ( Informer , Autoformer , PatchTST , N-BEATS ,
                           Temporal Convolutional Network , Neural ODE ,
                           State-Space Model , ... ) .

  TRAINING_PARADIGMS  -- vocabulary that signals a real ML training
                         pipeline ( self-supervised , contrastive ,
                           transfer , fine-tuning , data augmentation ,
                           dropout , batch norm , ... ) .

The combined predicates favor PRECISION over recall : papers only show
up in "Time-series ML" if BOTH a time-series cue AND an architecture
cue fire , so a paper that says "transformer-based reading-comprehension
study" doesn't get pulled in.
"""


def search():

	# Spelled-out architectures + technique names. Safe at the default
	# rapidfuzz threshold ( 80 ) because they're long enough that a
	# partial-ratio match is unambiguous.
	ARCHITECTURES_LONG = any_of(
		"transformer" ,
		"self-attention" , "self attention" , "multi-head attention" ,
		"convolutional neural network" , "convolutional network" ,
		"recurrent neural network" ,
		"long short-term memory" , "long short term memory" ,
		"gated recurrent unit" ,
		"graph neural network" , "graph convolutional" ,
		"autoencoder" , "variational autoencoder" ,
		"generative adversarial network" , "generative adversarial" ,
		"encoder-decoder" , "encoder decoder" ,
		"sequence-to-sequence" , "sequence to sequence" ,
		"residual network" , "resnet" ,
		"u-net" , "unet" ,
		"deep neural network" , "deep learning" , "deep network" ,
		"feedforward network" , "feed-forward network" ,
		"multilayer perceptron" , "multi-layer perceptron" ,
		"attention mechanism" ,
		"diffusion model" ,
		"foundation model" ,
		"large language model" ,
		"pretrained transformer" ,
	)

	# Short acronyms. PINNED to threshold = 100 so only literal substring
	# matches fire ; otherwise "cnn"/"rnn"/"gnn"/"knn" hit a ton of
	# spurious 3-letter fuzz matches in body prose.
	ACRONYMS_EXACT = any_of(
		"cnn" , "rnn" , "lstm" , "gru" , "gnn" , "gan" , "vae" ,
		"tcn" , "mlp" , "ann" ,
		"bert" , "gpt" , "t5" , "vit" ,
		threshold = 100 ,
	)

	# Generic time-series / sequence vocabulary. We use this to qualify
	# the architecture hits ( "transformer" alone could be an NLP paper ;
	# "transformer + time series" is what we want ) .
	TIME_SERIES = any_of(
		"time series" , "time-series" , "timeseries" ,
		"temporal sequence" , "sequential data" ,
		"sliding window" , "windowed" ,
		"longitudinal data" ,
		"sequence modeling" , "sequence modelling" ,
		"sequence prediction" ,
		"multivariate time series" , "univariate time series" ,
		"forecasting" , "time-series forecasting" ,
		"signal processing" ,
		"event-related" , "event related" ,
		"epoched" , "epochs" ,
	)

	# Time-series-specific architectures published since ~2018. Spelled
	# out long enough to be safe at the default threshold.
	TS_ARCHITECTURES = any_of(
		"temporal convolutional network" ,
		"wavenet" ,
		"neural ode" , "neural odes" , "neural ordinary differential" ,
		"state space model" , "state-space model" ,
		"deep state space" ,
		"informer" , "autoformer" , "fedformer" , "patchtst" , "patch tst" ,
		"n-beats" , "nbeats" , "n-hits" , "nhits" ,
		"timesnet" , "itransformer" , "dlinear" , "tide" ,
		"reformer" ,
	)

	# Training-pipeline vocabulary -- strong signal that a paper is
	# actually doing ML , not just citing it.
	TRAINING_PARADIGMS = any_of(
		"self-supervised" , "self supervised" ,
		"contrastive learning" , "contrastive loss" ,
		"few-shot" , "zero-shot" , "one-shot" ,
		"transfer learning" ,
		"pretrained" , "pre-trained" , "pretraining" ,
		"fine-tuning" , "fine tuning" , "finetuning" ,
		"data augmentation" ,
		"dropout" ,
		"batch normalization" , "batch norm" ,
		"layer normalization" ,
		"cross-validation" , "cross validation" , "k-fold" ,
		"early stopping" ,
		"hyperparameter" , "hyperparameters" ,
		"adam optimizer" , "adamw" ,
		"gradient descent" , "stochastic gradient descent" , "sgd" ,
		"loss function" ,
	)

	# Classical / non-deep baselines that often share a methods section
	# with the deep-learning piece -- useful for finding " they compared
	# DL vs SVM " style ablations.
	CLASSICAL_ML = any_of(
		"support vector machine" , "svm" ,
		"random forest" ,
		"gradient boosting" , "xgboost" , "lightgbm" , "catboost" ,
		"logistic regression" ,
		"k-nearest neighbor" , "k nearest neighbor" , "knn" ,
		"naive bayes" ,
		"hidden markov model" , "hmm" ,
		"arima" , "sarima" , "prophet" ,
	)

	# A real ML paper almost always exposes one of these task framings.
	ML_TASKS = any_of(
		"classification" ,
		"regression" ,
		"anomaly detection" ,
		"clustering" ,
		"representation learning" ,
		"decoding" ,
		"prediction" ,
		"forecasting" ,
		"segmentation" ,
	)

	# The "any architecture" predicate combines the spelled-out and
	# acronym lists -- so it fires whether the paper says "convolutional
	# neural network" or just "CNN" .
	ANY_ARCH = combine_or( ARCHITECTURES_LONG , ACRONYMS_EXACT , TS_ARCHITECTURES )

	return [
		# Catch-alls .
		( "ML architectures (any)"      , ANY_ARCH                         ) ,
		( "Deep learning (broad)"       , combine_or(
			ANY_ARCH ,
			any_of( "deep learning" , "deep neural" , "neural network" ) ,
		) ) ,
		( "Classical ML baselines"      , CLASSICAL_ML                     ) ,
		( "ML training paradigms"       , TRAINING_PARADIGMS               ) ,
		( "ML tasks"                    , ML_TASKS                         ) ,

		# Time-series cuts -- these are the ones the user actually wants.
		( "Time series (any)"           , TIME_SERIES                      ) ,
		( "Time-series ML (any arch)"   , combine_and( TIME_SERIES , ANY_ARCH ) ) ,
		( "Time-series + Transformer"   , combine_and(
			TIME_SERIES ,
			any_of( "transformer" , "self-attention" , "self attention" , "multi-head attention" ) ,
		) ) ,
		( "Time-series + CNN"           , combine_and(
			TIME_SERIES ,
			combine_or(
				any_of( "convolutional neural network" , "convolutional network" , "temporal convolutional" ) ,
				any_of( "cnn" , "tcn" , threshold=100 ) ,
			) ,
		) ) ,
		( "Time-series + RNN/LSTM/GRU"  , combine_and(
			TIME_SERIES ,
			combine_or(
				any_of(
					"recurrent neural network" ,
					"long short-term memory" , "long short term memory" ,
					"gated recurrent unit" ,
				) ,
				any_of( "rnn" , "lstm" , "gru" , threshold=100 ) ,
			) ,
		) ) ,
		( "Time-series + Attention"     , combine_and(
			TIME_SERIES ,
			any_of( "attention" , "self-attention" , "self attention" , "multi-head attention" ) ,
		) ) ,
		( "TS-specific architectures"   , TS_ARCHITECTURES                 ) ,

		# A high-precision "is this paper a TS-ML paper" cut : needs a
		# TS cue AND an architecture cue AND a training-paradigm or
		# task cue. Cuts out review papers that mention these terms but
		# don't actually train a model.
		( "Time-series ML (strict)"     , combine_and(
			TIME_SERIES ,
			ANY_ARCH ,
			combine_or( TRAINING_PARADIGMS , ML_TASKS ) ,
		) ) ,
	]
