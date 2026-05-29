def search():
    INNER_SPEECH = any_of(
        "inner speech", "imagined speech", "covert speech", "silent speech",
        "subvocalized speech", "subvocal speech", "subvocalization",
        "inner monologue", "inner dialogue",
        "imagined phonemes", "imagined phoneme", "imagined word", "imagined words",
        "covert articulation", "covert articulatory",
        "verbal imagery", "auditory verbal imagery", "phonological imagery",
        "imagined hearing",
    )
    FMRI = any_of(
        "fmri", "functional mri", "functional magnetic resonance",
        "bold signal", "bold response", "blood-oxygen-level",
        "blood oxygen level dependent",
    )
    DECODING = any_of(
        "decoding", "decoder", "classifier", "classification",
        "multivariate pattern", "mvpa",
        "representational similarity", "rsa",
        "brain-computer interface", "brain computer interface", "bci",
        "neural decoding", "speech bci", "speech decoding",
        "pattern analysis", "pattern classifier",
        "searchlight",
    )
    NOT_OVERT = none_of(
        "overt speech production", "lip reading", "lipreading",
        "articulatory kinematics", "acoustic analysis of speech",
    )

    STRICT     = combine_and( INNER_SPEECH , FMRI , DECODING , NOT_OVERT )
    INNER_FMRI = combine_and( INNER_SPEECH , FMRI , NOT_OVERT )
    INNER_DEC  = combine_and( INNER_SPEECH , DECODING , NOT_OVERT )

    return [
        ( "inner speech + fMRI + decoding" , STRICT ) ,
        ( "inner speech + fMRI"             , INNER_FMRI ) ,
        ( "inner speech + decoding"         , INNER_DEC ) ,
    ]
