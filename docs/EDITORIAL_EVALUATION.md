# Editorial reliability evaluation

> **Legacy document.** This file describes the Python/Swift application, version 1.10.3, kept under `src/`, `apps/` and `tests/` for reference. It does not describe the Rust terminal program on this branch. See [RUST_FOUNDATION.md](RUST_FOUNDATION.md).

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

This corpus has now been evaluated and is exposed development material. The prospectively
frozen experiment at revision `ba09e0a8ccff3db5821f17f7731e75ccc4f921d1` produced 4/10, 5/10,
and 3/10 full model-assessed passes in three fresh trials, including two processing failures
in the third. `examples/editorial_trials_v1_summary.json` preserves the original case verdicts,
configuration, and identities without private documents or provider traces. No trial passed
the entire corpus. These are automated approval counts, not human quality ratings.

Inspection found real failures (duplicated source continuations, name substitutions, changed
clause attachment, broken quotation boundaries) and assessor false positives (demanding original
paragraph breaks or period orthography where a valid contemporary copyedit could differ).
Do not rewrite those original scores. Calibrate additional style controls and report any
secondary adjudication separately. Future generalization claims need new prospective material.

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

The third rubric made semantic requirements explicit and exempted intact nonprose material.
Qwen3.8-27B at medium reasoning returned 20/20 valid, correct controls once and 19/20 on a
fresh repeat (one invalid evidence report, rejected rather than passed). This assessor is
useful research instrumentation, not an infallible oracle. Retain its validation errors and
inspect disagreements. Bounded schema repairs receive no desired verdict; failed editorial
judgments are not retried until a pass appears.

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

## Reproduce a trial

From a source checkout with provider credentials configured locally:

```sh
librarian maintainer editorial-eval examples/editorial_holdout_v1.json \
  --output-dir /private/path/editorial-trials --judge-model qwen-3.8-27b \
  --judge-reasoning medium --repeats 3
```

This command uses the configured editor. References enter only after generation. Each case
gets a separate database, and cached cleaned chunks invalidate a trial. Candidate Markdown,
quality reports, metrics, errors, assessments, and incremental trial records stay in the output
directory. The result records both normalized suite identity and the original file hash.
Do not put private transcripts, output artifacts, provider traces, or credentials in Git.

`cmos_v8` is an opt-in experimental profile, not the release default. It keeps code and tables
outside model generation, masks each numeric occurrence reversibly, renders bounded source
units as plain Markdown, and repairs semantic errors with literal patches scoped to candidate
spans. `LIBRARIAN_CLEANING_SOURCE_READING=true` adds an advisory source inventory, checked
separately from subsequent review. Neither the inventory nor internal approval is a quality
score. All relevant settings and prompts participate in cache invalidation.

Short OCR blocks are packed together, preserving the observed source, so that a continuation
can be edited with the words it belongs to. Long multiline prose and lists remain bounded.
New adjacent copied phrases are flagged against source occurrence counts rather than silently
deleted; intentional source repetition remains permitted. Document-level failures that cannot
be attributed to a single source unit are routed back to prose generation, so retries perform
real work instead of repeatedly assembling the same rejected draft. In the experimental profile,
spelling proposals remain advisory and are withheld from the independent reviewer.

Reviewer JSON and truncation failures have a separate format budget with the candidate
frozen. In advisory mode, exhaustion records `review-unavailable` and does not trigger a
copyedit or fail publication. Strict mode retains blocking review. With partial publication
enabled, exhausted content remains explicitly marked for review instead of aborting the
document. Each provider request also has a total attempt deadline in addition to HTTP timeouts.

## Second prospective corpus

`examples/editorial_holdout_v2.json` was frozen on 2026-11-05 before OpenAI candidate evaluation.
Its SHA-256 is `b38d05b6dc5b6aa2dfd46b601f3809245f3ccff2d26494d2d418c3748d6a20c5`.
Seed `20261105` selects ten different passages from the same five source books, excluding every
exposed reference hash. It retains the five real OCR readings/five punctuation transformations
design. This tests new passages within these genres, not new authors, languages, or every
real-world corruption process. References remain withheld from editing.

`examples/editorial_style_calibration.json` adds twelve authored assessor controls covering
legitimate reflow and spelling modernization, authentic quotation, pending events, unfamiliar
names, and wrapped source text. These are assessor checks, not editor performance results.

OpenAI access was verified with `gpt-6.1-terra` and `gpt-6-luna`. Both passed the twenty meaning
controls and twelve style controls once using medium reasoning and the unchanged `editorial_v3`
assessor. This is calibration evidence, not a guarantee. The subsequent Terra trial was blocked
by the provider's `credit_balance_exhausted` / `insufficient_quota` response after three new
passages passed. The full private transcript did not finish. The cohort remains incomplete;
neither 3/3 nor the later billing errors establish full editing reliability.

Terminal credit and credential failures now stop a benchmark rather than issuing the remaining
requests. Completed trials remain recorded, while planned and unattempted trials stay explicit.
An interrupted or blocked batch cannot pass. Ordinary transient rate limits remain retryable.

The funded continuation at `355aa6ac16fbc5ea4ceee1943ae7ecf6f3a6d337` completed all thirty
V2 attempts: 10/10, 8/10, and 9/10, with no processing failures. The original verdicts are
in `examples/editorial_terra_v2_summary.json`. Two failures moved a narrator's connective
inside quoted speech; one changed an identifying relative clause to a parenthetical clause.
The grader remains unchanged. The private full transcript completed with five source queries;
independent review still identified a broken join and a lowercase paragraph opening. That
output is a review draft, not evidence of fully completed prose.

The next revision reviews actual edited neighbours separately from source context. Review
approval depends on that context as well as the active text, so a changed neighbour invalidates
an earlier approval. It also flags newly introduced lowercase paragraph openings and expands
numeric validation to unit-attached values and versions, while allowing OCR letter/digit repairs
inside words. All changes stay opt-in under the experimental profile until qualified.

V3 is frozen before evaluation in `examples/editorial_holdout_v3.json`, SHA-256
`8d9d4f90df1008dc2e2a3169073f10ce739aba523a88bcf1958a03d037e95859`.
Seed `2026110503` excludes V1 and V2 references; its scope remains new passages from the same
five books. `examples/editorial_discourse_calibration.json` contains separate, authored
source-role controls, not phrases from the evaluated corpus.

The production reviewer is calibrated separately from the independent assessor. With only
source and candidate (no oracle requirements or reference), Luna handled 25/26 meaning/discourse
controls under `prose_review_v2`. The false positive demanded retention of a spoken question
tag despite an equivalent question. `prose_review_v3` compares pragmatic force instead of
requiring the tag verbatim; it handled all 26 controls once. Both results are retained in
`examples/editorial_production_gate_calibration_summary.json`. The independent evaluation
prompt remains `editorial_v3`; its gates have not been relaxed.


## Recovery contract verification (2026-11-05)

The outside diagnosis at `c24c391` correctly identified blocking reviewer feedback, fragile
acceptance, and document-wide publication failures. Two qualifications matter: completed
chunks already had caches, so their work was withheld rather than deleted; and deterministic
guards are fallible heuristics, not a proof of semantic fidelity. In particular, a raw
word-count threshold can reject legitimate removal of transcript filler; the content-loss guard
therefore measures filler-aware content-word retention (with credit for spelling repairs and a
bounded share of word substitutions), which accepts filler removal and rejects abbreviation,
and it is never cleared by a reviewer verdict. A reviewer-approved reduction retains that
approval without weakening numeric or structural checks.

The current application defaults to advisory review and explicit partial publication.
Strict review remains available. A source fallback is never described as cleaned: its exact
source body has a machine-readable marker and a quality-report item. Infrastructure failures
still fail the run and preserve accepted-unit checkpoints. Complete chunks use clean caches;
partial checkpoints retry unresolved units. Accepted bodies are immutable within the active
review contract. A quotation continuation whose predecessor is unresolved stays provisional.
Upgrading an old approval that lacks a required review facet is explicitly logged, rather
than silently called an immutable acceptance.

The private 7,879-word transcript was run through the actual processing service at
`37a14790bb01f842915ffd38dca35a05e8253085`, using `cmos_v8`, Terra writing and strict review at
medium reasoning, source reading disabled, 1,200-character source-unit targets, and three unit
retries. The final recovery completed four chunks with no source fallbacks or unit review
markers and three retained transcription queries. This recovery used 11 editing calls and
42 review/confirmation calls; it is not a cold-start timing result. A subsequent full-cache
run produced byte-identical Markdown with zero editing calls, zero review calls, and one
classification call. The audit examined 93 recorded editor requests and 15 active accepted
windows: no unapproved post-acceptance editing or changed accepted bytes. Eight older approvals
were explicitly reopened across contract upgrades (one quotation dependency and seven missing
editorial audits); these are reported separately, not hidden as zero rechecks.

The independent Luna assessment, with the unchanged `editorial_v3` rubric, passed five of six
criteria: completeness, fluency, voice, style, and structure. It failed fidelity on a homophone
correction inside quoted speech. The local context supports the correction, but the primary
failed verdict remains retained and is not converted to a pass. Three source queries also
prevent a full completed-cleanup pass. The preceding recovery had passed fidelity but failed
fluency and style; that assessment remains retained as well. Neither recovery establishes
100% editorial reliability. A separately recorded blind Terra assessment passed fidelity,
completeness, and voice but failed fluency, style, and structure on the three retained
transcription annotations. It did not replace the primary Luna judgment. The disagreement
reinforces the distinction between a published review draft and fully resolved prose.

Five live controls for the added editorial-risk checks preserved genuine alternatives,
authored expressions, and literal speech while repairing an abandoned referent. They passed
once with source reading enabled and once with it explicitly disabled. These are the same
five development cases repeated under two settings, not ten independent holdout cases.
The frozen V5 corpus remains unevaluated. Private sources, candidates, and provider traces
remain outside Git. Cerebras qualification has not resumed.

### Follow-up failures and fixes

A separate fresh-database transcript run at `37a1479` completed publication in about
17 minutes 37 seconds, using 64 editing calls and 128 review/confirmation calls. It retained
one 4,767-character source window. This is a processing success and an editorial failure,
not another clean-output pass. Its independent assessment also returned invalid source
evidence; that failed assessment is retained rather than rerun until favorable.

The failed window exposed a 6,000-character minimum repair-merge size that defeated smaller
unit profiles. `8d1080e` bounds merges by twice the configured unit target (at most 12,000
characters), and splits only oversized unresolved legacy windows back into canonical source
units. A real recovery reused three cached chunks and completed the fourth with eight editing
calls and 21 review/confirmation calls, no source fallback, and three transcription queries.
The independent assessment still failed fluency on an abandoned speech repair.

That repair exposed a separate coverage bug: the editorial-risk checker inspected only its
first four cues but could approve the entire unit. `e139f97` checks later cues in batches of
four, with an explicit capacity failure above 32 proposals instead of silent approval. It
upgrades old audit receipts explicitly. The subsequent real recovery used seven editing
calls and 28 review/confirmation calls, with no source fallback and the same three queries.
The unchanged Luna assessment passed fidelity, completeness, voice, style, and structure,
but failed fluency on a disconnected utterance it regarded as transcript debris. It is not
a full editorial pass. No phrase-specific deletion rule or changed assessor rubric was added.

The acceptance audit during that upgrade found two previously accepted units reopened
without an explicit invalidation. Checkpoint restore had reapplied a word-count heuristic
to already reviewed filler removal. The failed audit is retained. `ae08fdd` restores those
accepted bodies through the frozen-text validation path, while keeping numeric and structural
checks and explicitly reporting any hard-guard invalidation. The regression now exercises an
actual restart in both advisory and strict modes, as well as a corrupt numeric checkpoint.

Final replay verification at `ae08fdd` used a private SQLite backup with every completed-chunk
cache entry removed. All four chunks were reconstructed from unit checkpoints with zero
editing calls, zero review calls, and byte-identical Markdown; only spelling preparation and
classification called the provider. A separate complete-cache replay also returned identical
Markdown with zero editing/review calls. These are recovery checks, not fresh editorial trials.

The final local gate is 880 passing tests, 14 skipped, clean Ruff/Pyright, and successful wheel
and source-distribution builds. Swift frontend parsing passed, but a full Mac build could not
run with the local macOS 13 SDK, and GitHub's Mac jobs were canceled before receiving a runner.
The broader 100% editorial-quality target remains unqualified; the draft PR makes that limit
explicit. Cerebras and the untouched V5 qualification remain pending.
