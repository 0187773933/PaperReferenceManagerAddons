# Decoder / foundation-model literature gaps (fMRI only)

This is a running list of papers that would help the yes/no inner-speech fMRI project: its Aims 2–3, the "first foundation model fine-tuned on inner speech" claim, and few-shot handling of session drift. To qualify, a paper must have both properties below:

- **It involves fMRI.** Bimodal work (EEG-fMRI, fMRI + MEG) counts. EEG-only, MEG-only, fNIRS-only and modality-free papers do not.
- **It is not already in the library.**

**Focus since 2026-10-15: Aim 3.** The sweep looks for modern fMRI decoders that end in a decision (a class, a task or brain state, a trial label):
- transformers / attention;
- state-space models (Mamba / S4);
- hybrids;
- fine-tuned foundation models;
- inner / covert speech deep decoders, using the vocabulary in [Synonyms.md](Synonyms.md).

Classical MVPA / GLM-only work, encoding-only models and reconstruction / captioning are out of scope. The saved search for this focus is [`searches/aim3-decoders.py`](../searches/aim3-decoders.py).

## How it works

- **One document:** the Google Sheet [Inner Speech - Literature Gap Sweep](https://docs.google.com/spreadsheets/d/160yheZieArx8Y5QK1yx14WbGUhIXnQMjYmqUfH-1Cfc/edit). This file is the write-up around it. The sheet has four tabs:
  - **Sweep:** the papers. It's laid out for /sort's *⇩ Import CSV / Sheet*, matching on DOI_or_Key, Title, Year and Tags.
  - **Rejected:** papers that were looked at and turned down, each with a reason. They're kept so they're never suggested again, and kept off Sweep so an import never brings them in.
  - **Datasets** and **Code Examples:** the same columns as the Inner Speech - Datasets sheet, for fMRI resources worth copying over.
- **Daily sweep:** a scheduled agent (`inner-speech-gap-sweep`) runs at about 7 AM and appends to the sheet. It never edits the Inner Speech - Datasets sheet.
- **Guardrails in [`tools/gap-sweep.py`](../tools/gap-sweep.py):**
  - `add` refuses any row without an fMRI tag.
  - `code` and `dataset` refuse any row that doesn't say where the fMRI is.
  - `add` and `check` dedupe against the **live Zotero library**. They read a copy of zotero.sqlite through the repo's fail-safe snapshot, so this covers every item as of that run, not only the /sort board. They also dedupe against the live /sort list and both sheet tabs.
- **Saved search:** [`searches/decoder-foundation-models.py`](../searches/decoder-foundation-models.py) requires fMRI in every search. It runs on every `prma missing` and `prma search`.

## Already well covered (don't re-search)

The library already holds almost every fMRI foundation model from 2024–26:

- BrainLM, Brain-JEPA, BrainMass, NeuroSTORM, Omni-fMRI, SLIM-Brain, BrainGFM, fMRI-LM, BrainHarmonix, the flat-map ViTs, STPTF, RABBiT, BrainTaskonomy and TRIBE v2.
- Cross-subject alignment: MindEye2, MindBridge, MindAligner, MindTuner and MindShot.
- Other key papers: Kunz 2025, Tang & Huth 2025, LITcoder, and Yin 2025 on data splitting.

The gaps are in the methods around those models: single-trial estimation, validation, adaptation, denoising and data scaling.

## On the Sweep tab (all fMRI, none in the library)

**Single-trial estimation and preprocessing**

- [ ] **Prince et al., 2022, GLMsingle.** eLife, [10.7554/eLife.77599](https://doi.org/10.7554/eLife.77599). Single-trial betas, which would be the inputs to Aim 2. The code is in the Code Examples tab.
- [ ] **Vizioli et al., 2021, NORDIC.** Nat Commun, [10.1038/s41467-021-25431-8](https://doi.org/10.1038/s41467-021-25431-8). Thermal-noise removal for the MB6 / R=9.6 protocol. The code is in the Code Examples tab.

**Validation**

- [ ] **Varoquaux et al., 2017, Assessing and tuning brain decoders.** NeuroImage, [10.1016/j.neuroimage.2016.10.038](https://doi.org/10.1016/j.neuroimage.2016.10.038). Covers leave-session-out validation and time-separated test sets.

**Data and scaling**

- [ ] **Banville et al., 2025, Scaling laws for decoding images from brain activity.** [arXiv 2501.15322](https://arxiv.org/abs/2501.15322). Decoding scales with data **per subject**, which supports the dense single-participant design.
- [ ] **Boosting brain-to-image decoding with TRIBE v2 data augmentation (2026).** [arXiv 2606.06345](https://arxiv.org/abs/2606.06345). Synthetic fMRI from TRIBE v2 gives up to +68% on small datasets.

**Foundation models and adaptation**

- [ ] **Brain-OF (2026).** [arXiv 2602.23410](https://arxiv.org/abs/2602.23410). Jointly pretrained on fMRI, EEG and MEG.
- [ ] **StableMind (2026).** [arXiv 2605.02586](https://arxiv.org/abs/2605.02586). Source-free cross-subject fMRI adaptation; the closest fMRI analogue to test-time adaptation.

**Decoding state of the art**

- [ ] **Brain-to-Language Decoding: Tasks, Signals, Methods, Evaluation (2026 survey).** [arXiv 2609.27650](https://arxiv.org/abs/2609.27650)
- [ ] **Real-time reconstruction of human visual perception from fMRI (2026).** [arXiv 2607.22753](https://arxiv.org/abs/2607.22753). Real-time single-trial fMRI decoding, relevant to the Phase 3 pipeline.

**Significance**

- [ ] **Bodien et al., 2024, Cognitive motor dissociation.** NEJM, [10.1056/NEJMoa2400645](https://doi.org/10.1056/NEJMoa2400645). Task fMRI and EEG found covert command-following in 25% of unresponsive patients.

## Rejected (2026-10-14)

17 papers are in the Rejected tab. Most are EEG-only (Stacked LoRA, EEG test-time adaptation, EDAPT, CBraMod, LUNA, EEG-FM-Bench and others) or MEG-only. MOMENT has no brain data, one paper is fNIRS-only, the leakage preprint uses non-fMRI recordings, the Brain commentary centres on EEG and TMS-EEG, and the systematic review is mostly structural MRI and CT.

The ideas from the EEG papers (subject or session LoRA adapters, test-time adaptation) still feed the open questions below. The Sweep tab only lists them once an fMRI version exists.

## In Zotero but not on the main /sort list

These are already in the library and only need adding to /sort:

- Mumford et al., 2012 (LSS)
- d'Ascoli et al., 2025 (723-participant word decoding)
- Lévy et al., 2025 (Brain2Qwerty)
- Zero-shot imagined speech via MEG (2605.08075)
- MindVoice (2605.31173)
- Nalborczyk et al., 2020 (EMG during inner speech)
- Brain4FMs (2602.11558)

## Open questions (searched, nothing found yet)

These are worth re-checking on every sweep. While they stay empty they also back the Innovation claims.

- **Test-time adaptation for fMRI decoding.** Every TTA/drift paper found is on EEG, iEEG or EMG. StableMind (2605.02586) is the closest, but it still needs labelled target data. Partial answer (2026-10-15): co-adaptive DecNef (Neuroinformatics 2026, 10.1007/s12021-026-09768-3) updates a real-time fMRI decoder online from its own predictions and reports a drift analysis. It is a neurofeedback decoder, not a foundation model, so the claim still stands for deep or foundation fMRI models.
- **LoRA/PEFT on an fMRI-native foundation model for a decoding task.** Only fMRI-LM (already in the library) and LLM-side LoRA in the Algonauts 2025 entries.
- **Chronos, MOMENT or TimesFM applied to BOLD classification.** Nothing beyond Crimi 2025.
- **A head-to-head of GLMsingle vs LSS on MVPA accuracy.**
- **NORDIC with multiband and single-trial MVPA.**

## Sweep log

- **2026-10-14:** First sweep. Found 29 candidates, of which 22 were not in the library. GLMsingle and NORDIC went into Code Examples.
- **2026-10-14 (scheduled):** 5 added, including StableMind (2605.02586), a Brain-to-Language survey (2609.27650) and real-time fMRI reconstruction (2607.22753).
- **2026-10-14:** Applied the fMRI-only rule. 17 rows moved to Rejected and 10 remain on Sweep. Added the Datasets and Code Examples tabs to the sweep sheet. GLMsingle and NORDIC moved there from the Inner Speech - Datasets sheet, which is back as it was.
- **2026-10-15:** 2 added (top: co-adaptive real-time fMRI decoder, Neuroinformatics 2026, the closest fMRI analogue to test-time adaptation; Bressler et al. bioRxiv 2024 on motion, drift and class-order inflation of fMRI MVPA). code/data: none. Note: the Sweep tab had been cleared and the Rejected tab removed. 9 of the 10 earlier Sweep rows are now in Zotero; NORDIC (10.1038/s41467-021-25431-8) is the only one still missing. With the Rejected tab gone, its 17 earlier rejections no longer block re-suggestion.
- **2026-10-15:** Aim 3 backfill (2024-01-01 to 2026-10-15; arXiv about 350, OpenAlex about 2,070, PubMed 45, 4 WebSearch). 11 added (tier 1: 0, tier 2: 9, tier 3: 0, tier 4: 2; top: TABLeT image-AE fMRI tokenizer 2604.03619, multi-source cognitive taskonomy 2606.26279, ScaPT prompt tuning 2408.10567, measurement-aware SSM HRF head 2601.15344, BFM variance audit 2606.04010). About 45 screened keepers were already in the library, including every tier-1 and tier-3 hit (Causal fMRI-Mamba, STDA-SwiFT, NeuroSSM, MAMBAxBrain, the speech-intelligibility SSM, MindAlign, ISRCP). BrainMT was skipped by add as library. NeuroMamba (NeurIPS-W 2025) and BrainMoE (NeurIPS 2025) were not added because no DOI or arXiv ID could be verified. code: TABLeT (github.com/beotborry/TABLeT).
- **2026-10-15 (manual, OpenAlex + PubMed connectors):**
  - **What was searched:** an OpenAlex harvest of 1,457 unique works, drawn from four query families plus works citing 18 anchor decoder papers. Also searched: PubMed (111 records) and a Springer Nature / Nature-portfolio filter. Google Scholar was blocked by its robot check and not used.
  - **Result:** 28 added (tier 1: 14, tier 2: 5, tier 4: 9). About 275 hits were already in the library.
  - **Top finds:** Brain-TM, a transformer-Mamba brain-state decoder; the ICASSP 2025 moment-wise cognitive-state attention network; ASFFNet, a single-trial dynamic decoder; the BSPC 2026 soft-prompt few-shot FM decoding paper; BrainMoE (NeurIPS 2025).
- **2026-10-16 (scheduled):** 1 added (tier 1: 0, tier 2: 0, tier 3: 0, tier 4: 1; top: fMRINet explainable arousal decoding, J Neural Eng 2026, 10.1088/1741-2552/aead27). Window was 2026-10-08 to 10-16, with arXiv, OpenAlex text facets, citations of 18 anchors, PubMed and 2 WebSearch queries. Already in the library: the Frontiers Neuroinf "Transformers for fMRI" review, MedARC fMRI-FM 2510.13768, BRAID-fMRI 2610.09352 and the MICCAI spatio-temporal pretrained FM. Skipped NOEMA 2610.22049 (perceived-speech retrieval, temporal CNN, not a decision decoder). code: none. Note: Synonyms.md lives at the repo root, not under research/.
