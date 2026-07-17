# prma : the MODALITY vocabulary.
#
# One list , used everywhere :
#   - ` prma code `          -> the method columns in output/code/code.xlsx
#                               ( tagging both GitHub / OSF records and papers )
#   - ` prma method-images ` -> the pills under each paper title in
#                               output/method-images/report.html , and the
#                               modality filter chips at the top of it
#
# Add a modality here and BOTH pick it up on the next run -- no code change ,
# and no restart needed for a running ` prma server --watch ` .
#
# Each entry is :
#
#   ( label , ( phrase , phrase , ... ) )
#
#   label     what you write is BOTH what's displayed ( casing kept exactly )
#             and what gets searched for. Matched as a WHOLE WORD , so "PET"
#             can't fire inside "dataset" , and case-insensitively , so "fMRI"
#             finds fMRI / FMRI / fmri on its own.
#   phrases   OPTIONAL spelled-out forms , matched as substrings -- so a STEM
#             like "electroencephalogra" covers -phy / -m / -phic in one go.
#             Leave them off and the entry can just be the label string.
#
# WRITE THINGS THE WAY YOU SAY THEM. The lowercasing is handled for you , so
# "functional MRI" is fine -- nothing here needs to be pre-lowercased.
#
# ORDER MATTERS : it's the order the method columns appear in code.xlsx and the
# order pills / chips render in. Appending is free ; reordering reshuffles those
# columns , so only do it on purpose.
#
# What gets scanned is deliberately NOT the full OCR of a paper : a reference
# list names every modality a paper merely cites. See code.paper_methods .

METHODS = (
	( "fMRI"  , ( "functional MRI" , "functional magnetic resonance" ) ) ,
	( "sMRI"  , ( "structural MRI" ) ) ,
	( "rsMRI"  , ( "resting state MRI" ) ) ,
	( "EEG"   , ( "electroencephalogra" , ) ) ,
	( "ECoG"  , ( "electrocorticogra" , ) ) ,
	( "fNIRS" , ( "functional near-infrared" , "functional near infrared" ,
	              "near-infrared spectroscopy" ) ) ,
	( "MEG"   , ( "magnetoencephalogra" , ) ) ,
	( "PET"   , ( "positron emission" , ) ) ,
	( "EMG"   , ( "electromyography" , ) ) ,

	# --- add your own below ----------------------------------------------
	# ( "DTI"  , ( "diffusion tensor" , ) ) ,
	# ( "sEEG" , ( "stereoelectroencephalogra" , "stereo-EEG" ) ) ,
	# ( "TMS"  , "transcranial magnetic stimulation" ) ,   # one phrase : a bare string is fine
	# "MRI" ,                                              # no phrases : just the label
)
