"""
Surface papers that use BOTH fMRI AND machine learning. Combines an
fMRI vocabulary group with the same architecture / training-paradigm
groups that ` searches/machine-learning.py ` uses , and emits sliced
predicates for the cross-product cuts a reviewer typically wants :

  - fMRI + ANY ML architecture
  - fMRI + Transformer
  - fMRI + CNN
  - fMRI + RNN / LSTM / GRU
  - fMRI + attention
  - fMRI + Classical ML baselines ( SVM , random forest , logistic , ... )
  - fMRI + decoding ( the classic neuroscience pairing )
  - fMRI + Deep learning ( strict ) -- needs an architecture cue AND a
                                       training-pipeline cue , so review
                                       papers that just CITE ML drop out

This file is intentionally self-contained ( duplicates the FMRI + ML
vocabulary instead of importing from sibling files ) because the loader
runs each .py in isolation -- a cross-file import would silently fail.

To tune : edit the vocabulary tuples below ; the combined predicates
automatically pick up the changes on the next ` prma search ` run.
"""


def search():

	# -------------------------------------------------------------------
	# fMRI vocabulary
	# -------------------------------------------------------------------

	FMRI = any_of(
		"fmri" , "f-mri" , "f mri" ,
		"functional mri" , "functional magnetic resonance" ,
		"functional magnetic resonance imaging" ,
		"bold signal" , "bold response" , "bold contrast" ,
		"blood-oxygen-level dependent" , "blood oxygen level dependent" ,
		"task fmri" , "task-fmri" , "task-based fmri" ,
		"resting-state fmri" , "resting state fmri" , "rs-fmri" ,
		"event-related fmri" , "event related fmri" ,
		"fmri decoding" , "fmri data" , "fmri experiment" , "fmri study" ,
	)

	# -------------------------------------------------------------------
	# ML architecture vocabulary -- same shape as machine-learning.py.
	# -------------------------------------------------------------------

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

	# Short acronyms pinned to threshold = 100 so they only fire on a
	# literal substring -- otherwise 3-char fuzzy matches at 80 hit
	# body prose constantly.
	ACRONYMS_EXACT = any_of(
		"cnn" , "rnn" , "lstm" , "gru" , "gnn" , "gan" , "vae" ,
		"tcn" , "mlp" , "ann" ,
		"bert" , "gpt" , "vit" ,
		threshold = 100 ,
	)

	# Classical / non-deep ML methods commonly paired with fMRI in early
	# decoding work and in modern baselines.
	CLASSICAL_ML = any_of(
		"support vector machine" , "svm" ,
		"random forest" ,
		"gradient boosting" , "xgboost" , "lightgbm" ,
		"logistic regression" ,
		"linear discriminant analysis" , "lda" ,
		"k-nearest neighbor" , "k nearest neighbor" , "knn" ,
		"naive bayes" ,
		"ridge regression" , "lasso regression" , "elastic net" ,
		"partial least squares" , "pls" ,
		"multivariate pattern analysis" , "mvpa" ,
		"searchlight analysis" , "representational similarity analysis" , "rsa" ,
	)

	# Training-pipeline vocabulary -- strong signal that the paper is
	# actually training a model , not just citing ML in the introduction.
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
		"leave-one-out" , "leave-one-subject-out" , "loso" ,
		"early stopping" ,
		"hyperparameter" , "hyperparameters" ,
		"adam optimizer" , "adamw" ,
		"gradient descent" , "stochastic gradient descent" , "sgd" ,
		"loss function" ,
	)

	# Classic neuroscience-side framings that pair cleanly with ML.
	DECODING = any_of(
		"decoding" , "decoder" ,
		"brain decoding" , "neural decoding" ,
		"brain-computer interface" , "bci" ,
		"brain-machine interface" ,
		"classifier" , "classification" , "predictor" , "prediction" ,
	)

	# Composite "any ML" cue.
	ANY_ML_ARCH = combine_or( ARCHITECTURES_LONG , ACRONYMS_EXACT )
	ANY_ML      = combine_or( ANY_ML_ARCH , CLASSICAL_ML )

	# -------------------------------------------------------------------
	# Output predicates
	# -------------------------------------------------------------------

	return [

		# Catch-all : fMRI + anything machine-learning ( deep OR classical ) .
		( "fMRI + ML (any)"             , combine_and( FMRI , ANY_ML            ) ) ,

		# fMRI + deep-learning only ( no classical-ML hits ) .
		( "fMRI + Deep learning"        , combine_and( FMRI , ANY_ML_ARCH       ) ) ,

		# Architecture-family slices.
		( "fMRI + Transformer"          , combine_and(
			FMRI ,
			any_of( "transformer" , "self-attention" , "self attention" , "multi-head attention" ) ,
		) ) ,
		( "fMRI + CNN"                  , combine_and(
			FMRI ,
			combine_or(
				any_of( "convolutional neural network" , "convolutional network" ) ,
				any_of( "cnn" , threshold=100 ) ,
			) ,
		) ) ,
		( "fMRI + RNN/LSTM/GRU"         , combine_and(
			FMRI ,
			combine_or(
				any_of(
					"recurrent neural network" ,
					"long short-term memory" , "long short term memory" ,
					"gated recurrent unit" ,
				) ,
				any_of( "rnn" , "lstm" , "gru" , threshold=100 ) ,
			) ,
		) ) ,
		( "fMRI + Attention"            , combine_and(
			FMRI ,
			any_of( "attention" , "self-attention" , "self attention" , "multi-head attention" ) ,
		) ) ,
		( "fMRI + Autoencoder"          , combine_and(
			FMRI ,
			any_of( "autoencoder" , "variational autoencoder" , "encoder-decoder" , "encoder decoder" ) ,
		) ) ,
		( "fMRI + GNN / graph"          , combine_and(
			FMRI ,
			combine_or(
				any_of( "graph neural network" , "graph convolutional" , "functional connectivity graph" ) ,
				any_of( "gnn" , threshold=100 ) ,
			) ,
		) ) ,

		# Classical-ML cuts ( common in neuro decoding ) .
		( "fMRI + Classical ML"         , combine_and( FMRI , CLASSICAL_ML      ) ) ,
		( "fMRI + SVM"                  , combine_and(
			FMRI ,
			combine_or(
				any_of( "support vector machine" ) ,
				any_of( "svm" , threshold=100 ) ,
			) ,
		) ) ,
		( "fMRI + MVPA / decoding"      , combine_and(
			FMRI ,
			combine_or(
				any_of( "multivariate pattern analysis" , "searchlight analysis" , "representational similarity analysis" ) ,
				any_of( "mvpa" , "rsa" , threshold=100 ) ,
				DECODING ,
			) ,
		) ) ,

		# Strict cut : needs fMRI + architecture cue + training-pipeline
		# OR neuro-decoding cue. Drops introduction-only mentions.
		( "fMRI + ML (strict)"          , combine_and(
			FMRI ,
			ANY_ML ,
			combine_or( TRAINING_PARADIGMS , DECODING ) ,
		) ) ,
	]
