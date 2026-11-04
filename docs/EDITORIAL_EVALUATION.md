# Editorial reliability evaluation

The legacy phrase-match/classification score is a regression measure, not evidence that prose
is faithful or publishable. Editorial reliability is assessed separately from classification.

## Freeze before evaluation

`examples/editorial_holdout_v1.json` contains ten prospectively frozen passages selected with
seed `20261101`. It was frozen before candidate evaluation. Its SHA-256 is
`2caa22bd339e1314cebd7c07aa62e87b0d0d0058fe589368c54b4913fd2d7b41`.
The passages come from Project Gutenberg texts in fiction, science, political argument,
philosophy, and autobiography; each case records its source URL, title, and source hashes.
Five inputs are actual Tesseract readings of rendered reference passages. Five are punctuation
removal transformations, explicitly labeled as metamorphic tests rather than natural ASR.
Both are complementary to the owner's private real transcript, which must remain outside Git.

The editor receives only the observed input. Its independently preserved reference and any
semantic requirements are available only to assessment. Do not insert reference readings,
case-specific patches, rubric answers, or benchmark identifiers into editing prompts.

After a held-out failure is inspected for development, that case is exposed. Keep reporting
the failure and subsequent results, but obtain fresh prospective material for a new claim of
generalization. Never delete a failing case, report only the best trial, or silently relax the
frozen gates after seeing a candidate's output.

## Assessors must pass controls

`examples/editorial_judge_calibration.json` supplies authored positive and deliberately damaged
minimal pairs. They check the assessor, not the editor. The controls include negation,
uncertainty, attribution, rhetoric, quote boundaries, false starts, omitted details, inventions,
code indentation, and table relationships. Faithful paraphrases should pass; fluent distortions
and untouched disfluencies should fail. A judge that misses these controls cannot establish a
reliable editing score. Invalid JSON, fabricated evidence, missing requirements, contradictory
verdicts, and uncertain decisions are assessment failures, not passes.

Initial live calibration on 2026-11-01 exposed assessor errors: the first rubric yielded 19/20
correct controls with GPT-OSS-120B at high reasoning and 18/20 with Qwen3.8-27B at medium reasoning.
The second rubric clarified removal of abandoned speech and exact evidence quoting; it still
produced false positives and invalid evidence. Both prompt versions and all controls remain
available. These observations do not certify either judge. In particular, semantic requirements
must not become exact-wording demands, and preserved tables must not be condemned for lacking
prose. Establish valid assessment and inspect disagreements before claiming editorial reliability.

## Production trials

Run every trial through the actual extraction and processing services in fresh storage.
Cache hits must not count as independent generations. Pin source, prompt, configuration, model,
provider endpoint, and assessor versions. Use at least three fresh trials for a reliability
claim, retain every attempt, and record model calls, elapsed time, rejected edits, and run errors.
Do not hand-edit outputs. Existing deterministic number, code, table, and reference checks remain
mandatory. Classification results are reported separately from editing quality.

The independent assessor sees source, candidate, reference, and oracle requirements, but no
editor identity, prompt version, previous approvals, or benchmark score. Assess fidelity,
completeness, fluency, voice, Chicago style, and structure independently. A full pass requires
all criteria and requirements to pass, valid supporting evidence, and intact protected data.
Unresolved transcription annotations are tracked as review work and do not inflate completed
cleanup success. Returning raw source or failing every run also cannot earn a perfect score.

Report both per-case results and the all-cases pass rate for each trial. A passing run is
evidence on that corpus, not proof that every future corrupted document can be reconstructed.
