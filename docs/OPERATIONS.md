# Operations

This runbook covers the stable single-node production profile: local CLI use, one API process, or an API plus SQLite-backed worker. Source documents, converted files, page manifests, cleaned outputs, and search indexes are private runtime data.

## Storage

SQLite is the supported storage backend. Librarian opens connections with WAL mode, foreign keys, `synchronous=NORMAL`, and a 5-second busy timeout. Use a persistent disk or volume and run migrations before starting API or worker processes when startup ordering matters.

```bash
librarian migrate
librarian admin db-check
librarian admin db-stats
librarian admin db-maintain
```

For long-lived databases, run maintenance during a quiet window. Use `librarian admin db-maintain --vacuum` only when compaction is worth the extra runtime.

## Backups And Restore

Create both database-only and full workspace backups when operating the API with uploads:

```bash
librarian admin workspace-backup /backups/librarian-workspace-$(date +%Y%m%d%H%M%S).zip
librarian admin db-backup /backups/librarian-$(date +%Y%m%d%H%M%S).sqlite
```

Backup and restore paths must not be symlinks or cross symlinked parents. Workspace backup skips symlinked files under the data directory so archives do not copy targets outside the workspace.

Stop API and worker processes before restoring:

```bash
librarian admin db-restore /backups/librarian-20260522120000.sqlite --yes
librarian admin workspace-restore /backups/librarian-workspace-20260522120000.zip --yes
librarian admin db-check
```

Workspace restore rejects oversized manifests, duplicate archive paths, unsafe member paths, symlink archive members, excessive file counts, and archives whose expanded size exceeds the configured limit.

## Conversion And OCR

Markdown is the canonical structured conversion format. Built-in support covers `.txt`, `.md`, `.csv`, `.json`, `.srt`, `.vtt`, `.docx`, `.pdf`, and common OCR image formats. Optional MarkItDown support adds broader formats such as `.pptx`, `.xlsx`, `.html`, `.rtf`, `.epub`, and `.xml`.

Local conversion and import enforce configurable input limits before expensive parsing:

- `LIBRARIAN_MAX_SOURCE_BYTES`
- `LIBRARIAN_TEXT_MAX_INPUT_BYTES`
- `LIBRARIAN_DOCX_MAX_INPUT_BYTES`
- `LIBRARIAN_PDF_MAX_INPUT_BYTES`
- `LIBRARIAN_PDF_MAX_PAGES`
- `LIBRARIAN_API_MAX_UPLOAD_BYTES`

PDF extraction is page-aware. The text layer is read with PDFium (`LIBRARIAN_PDF_ENGINE=auto`, the default); pages with a usable text layer are never OCR'd, while empty pages and image pages with only stray characters are OCR'd. A page whose OCR fails keeps its text layer (or an explicit failed-page marker), is flagged `ocr-page-failed`, and the document completes; set `LIBRARIAN_OCR_FAIL_ON_PAGE_ERROR=true` to abort instead. OCR output never replaces a text layer that holds more of the page, and a rejected LLM OCR correction keeps the raw OCR text (`ocr-correction-failed`). Long OCR jobs write `<output>.pages.json` manifests when conversion sidecars are enabled. These manifests record page status, source, OCR confidence, retry attempts, correction state, warning codes, and optional preserved page image paths.

With `LIBRARIAN_FIGURE_VISION_ENABLED=true` and a vision-capable model (`LIBRARIAN_FIGURE_VISION_MODEL`, else the cleaning model), pages that carry raster images, at least 30 vector path objects (charts, ruled tables), typeset mathematics, or no usable text layer are rendered with PDFium and sent to the model (`LIBRARIAN_FIGURE_VISION_MAX_FIGURES` pages at most, most valuable first). The reply's Markdown replaces the page when it retains at least 90% of the text layer's words; otherwise only its `**Figure:**` paragraphs, Markdown tables, and `$$` equations are appended. Pages without a text layer take the vision transcription in place of Tesseract. The extraction report's `page_vision` records pages sent, replaced, augmented, failed, and the first provider error (a text-only model rejects the image and the page keeps its text layer).

`LIBRARIAN_PDF_ENGINE=liteparse` (or `LIBRARIAN_PDF_LAYOUT_ENHANCER=liteparse` with `auto`) runs the liteparse layout engine behind a retention guard: each page of its Markdown is measured against the PDFium text layer (word recall), pages below the threshold (80% as the engine, 95% as the enhancer) are replaced by the text layer, and a document with more than a third of its pages failing is re-extracted with the built-in extractor. The extraction report records `extraction_retention` (document and per-page scores, replaced pages, fallback) and `fallback_reason`; the Mac quality view shows them. `legacy` keeps the historical pdfplumber text layer and per-page headings.

Inspect page manifests without dumping raw page text:

```bash
librarian admin page-manifest ./out/report.md.pages.json --failures-only
librarian admin page-manifest ./out/report.md.pages.json --json --failures-only
```

API manifest inspection is available at `GET /imports/page-manifest`, constrained to `LIBRARIAN_API_IMPORT_ROOT`, and requires operational/write-scope credentials.

## Security

CLI users are trusted local operators. API callers are untrusted unless they present a configured API key. Public API binds require an API key and import root. Read-scoped keys can read documents and search results; write-scoped keys are required for operational endpoints such as config, metrics, audit, and page-manifest inspection.

Generated sidecars, reports, and page manifests are internal metadata and must not be treated as corpus input. Recursive conversion and import skip Librarian-generated metadata to avoid self-ingestion.

Archive formats are rejected by default, and common archive signatures are rejected even when renamed. Unpack archives outside Librarian after organization-approved malware scanning, then import extracted files from a controlled directory.

Librarian does not log source text or generated document content. Persisted error strings are redacted and length-capped before status APIs expose them. JSON and text logging redact common credential patterns, bearer tokens, and `sk-...` provider keys.

## Run Quality And Provider Failures

Chunk fidelity failures trigger up to `LIBRARIAN_CLEANING_FIDELITY_MAX_RETRIES` (default 3)
additional cleaning attempts. Each retry uses the original source and explicit validation
diagnostics, including numeric-token counts. This budget is separate from transport retries.
If the model still loses content or changes protected data, the run fails and keeps the original
source available. Raw source is never substituted for a successful cleaned chunk. Prior successful
exports remain available, and passing chunks are cached for a later retry. Legacy cached source
fallbacks are re-cleaned rather than accepted as completed work.

The opt-in `cmos_v6` cleaning prompt uses source-aligned JSON edits and a document spelling
sheet. It independently checks each changed span for meaning changes and retries only rejected
spans. Code remains verbatim; table dimensions, cell quantities, and numeric occurrences are
checked deterministically. Semantic checks are enabled by default and can be controlled with
`LIBRARIAN_CLEANING_SEMANTIC_VERIFICATION`. They use the configured provider and incur additional
calls. These checks reduce errors; inspect quotations and genuinely ambiguous transcription in
the exported Markdown. `[transcription unclear: ...]` annotations make uncertainty visible and
set the quality status to `needs-review`.

Truncated structured-edit responses retain their partial candidate for diagnosis and trigger
the same bounded smaller-span retries as rejected edits. Partial text is never accepted as a
complete response. Chunk totals are recorded before document preparation begins, including
when preparation fails.

`LIBRARIAN_LLM_REASONING_EFFORT` optionally requests a provider-supported reasoning level.
Leaving it unset preserves the provider default. Unsupported values are reported as request
errors rather than silently downgraded. Larger reasoning budgets can also exhaust the output
limit; reasoning and answer tokens may share that limit.
`LIBRARIAN_CLEANING_REASONING_EFFORT` overrides the editing stage separately, so a deeper
copyedit can retain ordinary reasoning budgets for classification and other model tasks.

`LIBRARIAN_CLEANING_VERIFICATION_MODEL` selects an independent model on the same endpoint for
semantic review. `LIBRARIAN_CLEANING_VERIFICATION_REASONING_EFFORT`
controls that profile separately. Unset review options reuse the editing profile. Cache keys
include both profiles. Independent review reduces reliance on one model approving its own edits;
it does not establish that a model or profile is accurate without external evaluation.
Spelling preparation uses the base LLM profile independently of the editing-only override.

Rejected large spans are subdivided on later attempts, retaining neighboring context and
checking the reconstructed parent span again. Passing spans are retained. Untouched long,
unpunctuated prose is rejected rather than counted as an edited result. Sentence seams receive
light capitalization and paragraph repair; prose uses ordinary editable spaces and hyphens.

Long cleaning calls emit waiting heartbeats. Quality reports retain each attempt's checks and
candidate output locally, including rejected candidates, so a failed run can be investigated.

Cleaning cache keys include the full document context, prompt contents, provider endpoint,
and verification policy. Resumed runs retain the original chunk order and neighboring context;
only newly cleaned chunks advance progress. Verified native PDF extraction is still copyedited:
extraction confidence is not an editorial-quality score.

OCR correction retries failed fidelity checks against the original page up to three attempts.
Unknown confidence is eligible for correction in `low-confidence` mode. Unsafe corrections fail
the page instead of replacing its OCR with damaged text. Changing the OCR fidelity policy also
invalidates old page manifests. DOCX extraction preserves headings, empty table cells, and
literal cell pipes in Markdown tables.

For a completed Mac app run, open **Quality** on its row. The view shows page extraction repairs,
whole-document warnings, and final refinement actions. Use **Review page** for a flagged PDF page;
compare the exported Markdown with the stored source. An `applied` action passed the edit checks;
`rejected` or `deferred` means the issue still needs review. The final pass has an action budget
(`LIBRARIAN_FINAL_REFINEMENT_MAX_ACTIONS`, default 32), so large or noisy documents may retain
warnings. The API exposes the same reports at `GET /runs/{id}/quality` and run progress at
`GET /runs/{id}/events`.

If a provider returns rate limits or timeouts, inspect the run status and the Mac backend log or
API/worker logs, then lower
`LIBRARIAN_LLM_MAX_CONCURRENCY` (default 8) or `LIBRARIAN_IMPORT_CONCURRENCY` (default 2) before
retrying. `LIBRARIAN_LLM_MAX_RETRIES` (default 5) controls retries for transient model failures;
`LIBRARIAN_LLM_TIMEOUT_SECONDS` (default 120) bounds each request. A content refusal or persistent
provider error may require a different provider or model. `POST /runs/{id}/retry` starts a new run
for a failed one. A failed reprocess leaves any previous successful export available.

The extraction cache is enabled by default. Reprocessing a document may reuse extracted pages or
cleaned chunks when their cache keys still match; inspect the quality report's cache counts when
comparing a repeated run with a fresh import. For a true first-run test, use a fresh data directory
or reset the Mac app as described in [its guide](../apps/macos/README.md#resetting-the-app-for-a-fresh-test).

## Performance

Performance depends on provider, model, document type, OCR path, and concurrency settings. Record these values when comparing runs:

- model/provider/base URL
- `LIBRARIAN_LLM_MAX_CONCURRENCY`
- `LIBRARIAN_LLM_MAX_OUTPUT_TOKENS`
- `LIBRARIAN_FINAL_REFINEMENT_MAX_ACTIONS`
- `LIBRARIAN_IMPORT_CONCURRENCY`
- `LIBRARIAN_OCR_PAGE_CONCURRENCY`
- `LIBRARIAN_OCR_LLM_CORRECTION`
- `LIBRARIAN_OCR_ROTATION_RETRY`
- chunk target and overlap
- coherence mode
- document page count and scanned-page count
- `/metrics` OCR throughput, correction counts, queue wait, run-stage timing, and provider token usage

For large PDFs, measure conversion separately from processing:

```bash
time librarian convert ./large.pdf --format md --output ./large.md
time librarian ingest ./large.md
time librarian process doc_...
```

Run once with `LIBRARIAN_OCR_LLM_CORRECTION=never` to isolate extraction/OCR throughput, then run with the intended correction provider to measure final quality and cost.

Maintainers can run prompt, corpus, and throughput checks without exposing them as top-level user commands:

```bash
librarian maintainer eval examples/eval_cases.json --output eval-provider.json
librarian maintainer benchmark --paragraphs 40 --paragraph-chars 1000 --repeats 3 --output bench-provider.json
librarian maintainer corpus-eval examples/corpus_eval_cases.json --output-dir .librarian/corpus-eval --output corpus-eval-provider.json --overwrite
```

Do not commit provider outputs that contain private text.

## Release Hygiene

Before tagging a stable release, run:

```bash
ruff check .
pyright
pytest
pip-audit --progress-spinner off --skip-editable
librarian doctor --strict
rm -rf dist
python -m build
docker build -t librarian-release-check .
```

The tag release workflow verifies tag/version alignment, changelog readiness, secret scanning, dependency audit, tests, type checking, wheel build, smoke installation, SBOM generation, checksums, distribution attestations, Docker build, image scan, image attestation, and GitHub release creation.

## Migrations

SQLite migrations live in `src/librarian/storage/migrations` and apply in lexical order. Each applied filename is recorded in `schema_migrations`.

- Name migrations with a zero-padded sequence and short description, such as `0006_add_field.sql`.
- Treat applied migrations as immutable; add a new migration for follow-up changes.
- Keep migrations small enough to review.
- Add tests for new migrations.

## Bounded editorial recovery

Content repair and provider recovery have separate outcomes. With
`LIBRARIAN_CLEANING_PUBLISH_PARTIAL=true` (the application default), exhausted
content repairs publish a draft with explicit `librarian:needs-review` HTML
comments around unresolved source spans. The run succeeds and the document is
ready to open, but its quality report says `cleaning_status: needs-review`.
`needs_review` records unit ID, zero-based chunk index, reasons, attempt count,
and source text. `kind: source` means the exact source was retained;
`kind: advisory` retains the edit and records a fallible reviewer concern.
Neither outcome is a claim that the passage has been fully cleaned.
The comments remain in Markdown exports; consult the quality report or the Mac
quality view before treating a draft as final.

Partial work is stored separately from the clean cache. Reprocessing uses
source- and policy-matched unit checkpoints, preserving accepted bytes and
retrying unresolved source units. Accepted units also checkpoint before the
chunk finishes, so a later provider failure does not discard them. Changed
source, prompts, model settings, or review policy invalidate reuse. Authentication,
credit, cancellation, storage, and provider failures still stop a run; they do
not become successful source fallbacks.

`LIBRARIAN_CLEANING_REVIEW_MODE` accepts `off`, `advisory` (default), or `strict`.
Advisory findings never instruct the editor or cause another draft. Unusable
reviewer JSON gets one format retry and is then reported as `review-unavailable`.
Strict review can request bounded, source-grounded repairs and blocks acceptance
of unresolved semantic issues. To retain the earlier all-or-nothing behavior,
combine strict review with `LIBRARIAN_CLEANING_PUBLISH_PARTIAL=false`.
The deprecated `LIBRARIAN_CLEANING_SEMANTIC_VERIFICATION` alias maps explicit
`true` to strict and `false` to off; remove it when using the new setting.
Deterministic checks detect defined failures; passing them does not prove
semantic correctness. Two of them guard against a model that abbreviates:
`substantial-content-loss` fires when fewer than 90% of a unit's content words
(four or more letters, conversational filler such as "um" or "you know"
excluded; spelling repairs and the odd word substitution are credited) survive
in the candidate, and `model-elided-text` fires when the candidate contains an
ellipsis the source did not. No review mode or reviewer verdict clears either;
a unit that fails them after its retry budget is published as its complete
source inside a review marker. The run quality report records `words_retained`
per chunk and for the document, and warns `low-content-retention` below 90%.
A candidate whose `<!-- page-break -->` markers differ from the source has them
rebuilt from the words around each source break (`page-breaks-restored`)
before the structural checks run; `changed-page-breaks` remains for the case
where a break cannot be placed.

The app's data directory (`~/Library/Application Support/Librarian` for the
Mac app; `LIBRARIAN_DATA_DIR` otherwise) holds the SQLite library, the
extraction and cleaned-chunk caches, the `.env` settings, and `backend.log`.
Removing the app or the export folder leaves it in place. An extraction whose
pages could not all be read is never cached or reused, so a retry re-extracts.

A reply cut off at the output token limit is retried with a doubled budget up
to 65,536 tokens before it counts as a failed attempt. Reasoning models
(gpt-oss, o-series) spend their hidden reasoning from that same budget; set
`LIBRARIAN_CLEANING_REASONING_EFFORT=low` for them to keep replies short and
cleaning fast.

For constrained models, this explicit profile reduces each editing job and
avoids source-reading and unchanged-prose review calls:

```dotenv
LIBRARIAN_CLEANING_PROMPT_VERSION=cmos_v8
LIBRARIAN_CLEANING_REVIEW_MODE=advisory
LIBRARIAN_CLEANING_PUBLISH_PARTIAL=true
LIBRARIAN_CLEANING_UNIT_TARGET_CHARS=1200
LIBRARIAN_CLEANING_UNIT_MAX_RETRIES=3
LIBRARIAN_CLEANING_SOURCE_READING=false
LIBRARIAN_LLM_MAX_CONCURRENCY=2
```

The source-aligned `cmos_v8` editor remains experimental; the release prompt
selection is unchanged. This profile is a bounded recovery strategy, not a
quality guarantee for an arbitrary model. Unit edits use the existing worker
queue; dependent strict semantic patches remain in source order. Accepted units
are excluded from later editing and review, and their neighbors remain read-only
context. `units-accepted` attempt records identify accepted IDs and content hashes.
Merged failed windows share their original units’ unspent retries; merging cannot reset
or enlarge that allocation, and the whole-chunk safety ceiling still applies.
The unit budget falls back to `CLEANING_FIDELITY_MAX_RETRIES` when
`CLEANING_UNIT_MAX_RETRIES` is unset; its effective default is three retries.

Quoted continuations whose opening boundary depends on an unresolved preceding unit
remain provisional. Their draft is reused, but review may repeat when that context
changes; independent accepted units stay frozen. Older checkpoints that prematurely
froze such a quotation emit an explicit `context-approval-deferred` transition.
Confirmation explanations stay in the audit log: only the original proposal that
was actually approved becomes a repair instruction.

Malformed or truncated spelling suggestions and optional source readings are
reported as unavailable and skipped. They cannot abort prose cleaning. Truncated
classification uses the existing deterministic metadata fallback. Access, credit,
network, cancellation, and storage failures still stop the run and preserve
completed approvals for recovery. Individual approval events have unique persisted
keys so simultaneous approvals cannot overwrite each other’s audit records.

Strict prose review also requires explicit decisions on a bounded set of candidate
editorial risks, including apparent abandoned corrections and broken aside joins.
These are proposals, not deterministic proof of errors. The reviewer must reject
repairs that would remove genuine alternatives, meaningful uncertainty, humor,
authored quotations, or linguistic examples. Audit receipts let later runs reuse
those decisions; legacy cue-bearing units receive the missing audit without
regenerating unrelated accepted text. No evaluation reference enters this path.

Unit review and final assembly use the same source-aware comma join: a leading
comma introduced by an editor attaches to the preceding word. Source punctuation
and paragraph gaps are preserved, as are the bytes of the accepted units.
