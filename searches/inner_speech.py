def search():
    INNER_SPEECH = any_of(
        "inner speech", "imagined speech", "covert speech", "silent speech",
        "subvocalized speech", "subvocal speech", "inner monologue", "inner dialogue",
        "imagined phonemes", "imagined phoneme", "covert articulation",
        "silent communication", "private speech", "self-talk", "self talk",
        "covert articulatory", "imagined hearing", "imagined word",
    )
    FMRI = any_of("fmri", "functional mri", "functional magnetic resonance", "bold signal")
    ADJACENT = any_of(
        "auditory verbal hallucination", "alien voices",
        "intraoperative speech arrest", "speech arrest",
        "cognitive motor dissociation", "comatose", "covert command",
    )
    DECODING = any_of(
        "decoding", "classifier", "classification", "prediction", "regression",
        "brain-computer interface", "bci", "neural decoding", "speech bci",
    )
    PARADIGM = any_of(
        "paradigm", "block design", "task", "experiment", "procedure",
        "monosyllabic prompt", "monosyllabic", "phoneme production",
    )
    NOT_OVERT = none_of(
        "overt speech production", "lip reading", "lipreading",
        "articulatory kinematics", "acoustic analysis of speech",
    )

    return [
        ("Inner speech (any)", INNER_SPEECH),
        ("fMRI + inner speech", combine_and(INNER_SPEECH, FMRI, NOT_OVERT)),
        ("fMRI + inner speech + decoding",
            combine_and(INNER_SPEECH, FMRI, DECODING, NOT_OVERT)),
        ("fMRI + inner speech + paradigm",
            combine_and(INNER_SPEECH, FMRI, PARADIGM, NOT_OVERT)),
        ("fMRI + inner-speech-related (wide)",
            combine_and(FMRI, at_least(2, INNER_SPEECH, ADJACENT, DECODING), NOT_OVERT)),
        ("fMRI + adjacent constructs", combine_and(ADJACENT, FMRI, NOT_OVERT)),
    ]