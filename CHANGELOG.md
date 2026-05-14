# Changelog

## Unreleased

## 0.1.0a61 - 2026-05-14

- Added repeated-tail output validation so degenerate LLM/OCR corrections that
  end by looping the same phrase are persisted as `repeated-tail` cleaned chunk
  warnings and fail evals unless explicitly allowed.

## 0.1.0a60 - 2026-05-14

- Added an external repository capability review covering Dolphin, olmOCR, and
  LongCut, including licensing constraints, reusable capabilities, and
  implementation strategies for optional visual parsing, VLM page OCR, richer
  corpus evals, transcript citations, notes, and grounded Q&A.
- Updated the roadmap with the review-derived v0.2/v0.3 priorities.

## 0.1.0a59 - 2026-05-14

- Extended corpus-eval release evidence semantic checks so DOCX/table/header
  tags must come from DOCX sources, embedded-text tags must prove embedded PDF
  extraction, and transcript-caption/SRT/VTT tags must come from caption files
  with search and classification evidence.

## 0.1.0a58 - 2026-05-14

- Hardened corpus-eval release evidence against tag spoofing by requiring PDF
  tags to include page counts/status/source diagnostics, OCR/scanned tags to
  include OCR page/source metrics, and mixed embedded/scanned PDF tags to prove
  both embedded and OCR page sources.

## 0.1.0a57 - 2026-05-14

- Tightened release corpus-eval evidence verification so PDF/page extraction
  evidence is rejected if any case still reports failed or pending page
  statuses, closing a gap where aggregate counts could be internally
  consistent while incomplete page extraction slipped through.
- Updated README benchmark examples to use the deterministic multi-chunk
  synthetic workload used by CI and release gates.

## 0.1.0a56 - 2026-05-14

- Strengthened benchmark release evidence by moving CI/release benchmarks to a
  deterministic multi-chunk synthetic workload and adding verifier floors for
  minimum benchmark input characters and chunk count.

## 0.1.0a55 - 2026-05-14

- Added required corpus-eval tag coverage to release evidence verification and
  wired CI, release, and manual release checks to require the shipped DOCX,
  table/header/footer, embedded PDF, scanned OCR PDF, noisy OCR, mixed
  embedded/scanned PDF, and SRT/VTT caption transcript coverage tags in per-case
  evidence.

## 0.1.0a54 - 2026-05-14

- Added required prompt-eval tag coverage to release evidence verification and
  wired CI, release, and manual release checks to require the shipped
  classification, transcript, legal, technical, no-summarization, Markdown, and
  OCR-correction risk tags in per-case evidence.

## 0.1.0a53 - 2026-05-14

- Added a release evidence floor for prompt eval coverage with
  `--min-eval-cases`, wired CI/release/manual verification to require the six
  shipped sanitized prompt-risk cases, and expanded the shipped eval-suite
  coverage test for transcript, legal, technical, Markdown, OCR correction, and
  no-summarization risks.

## 0.1.0a52 - 2026-05-14

- Surfaced PDF page-manifest schema and lifecycle summaries through CLI/API
  inspection, and made corpus-eval reject mismatched page summary diagnostics
  instead of trusting top-level manifest summaries.

## 0.1.0a51 - 2026-05-14

- Added top-level lifecycle summaries and schema versioning to PDF page
  extraction manifests so large OCR runs expose status, retry, source,
  warning, confidence, and duration diagnostics without scanning raw page text.
- Tightened CI and release evidence verification to require all 13 shipped
  synthetic corpus-eval cases, including the SRT/VTT caption fixtures.

## 0.1.0a50 - 2026-05-14

- Added sanitized SRT/VTT caption fixtures to the synthetic corpus generator
  and shipped corpus-eval suite, covering transcript-caption conversion,
  search, classification, and text-order assertions in release evidence.

## 0.1.0a49 - 2026-05-14

- Added clean WebVTT transcript export through `TranscriptFormat.VTT` and
  `librarian transcript-normalize --format vtt`, including VTT timestamp
  formatting and CLI/docs coverage.

## 0.1.0a48 - 2026-05-14

- Improved SRT/VTT transcript parsing by stripping common caption markup,
  unescaping HTML entities, preserving WebVTT voice labels as speakers, and
  removing spacing artifacts left by inline caption tags.

## 0.1.0a47 - 2026-05-14

- Added direct `.srt` and `.vtt` ingest/conversion support through the composite
  extractor, rendering timestamped captions as sentence-merged Markdown before
  import or conversion.
- Fixed VTT cue-setting handling so settings such as `align:start` and
  `position:10%` are not treated as transcript text.

## 0.1.0a46 - 2026-05-14

- Added transcript quote matching and the `librarian transcript-find` CLI command
  to map exact or fuzzy quote matches back to timestamp and segment evidence in
  SRT/VTT-style or timestamp-prefixed transcript files.

## 0.1.0a45 - 2026-05-14

- Added `expected_text_order` to corpus-eval cases so sanitized evaluation
  suites can catch reading-order regressions in converted transcripts,
  multi-column documents, and OCR output after whitespace normalization.
- Enabled order assertions in the shipped corpus-eval example.

## 0.1.0a44 - 2026-05-14

- Added timestamped transcript normalization utilities and a
  `librarian transcript-normalize` CLI command that parses SRT/VTT-style
  timestamp ranges and timestamp-prefixed transcript lines, reconstructs
  sentence-like spans, preserves speaker labels, and exports Markdown, text,
  SRT, or CSV without calling an LLM.
- Documented transcript normalization in the README, quickstart, and conversion
  guide.

## 0.1.0a43 - 2026-05-14

- Strengthened release evidence verification to cross-check per-record derived
  metrics, rejecting eval/corpus output ratios, corpus search recall, and
  benchmark run throughput when they do not match the raw size, timing, and
  search diagnostics.

## 0.1.0a42 - 2026-05-14

- Expanded benchmark release evidence verification to recompute aggregate
  input-size, chunk-count, duration, and throughput summary metrics from
  per-run records.

## 0.1.0a41 - 2026-05-14

- Expanded prompt-eval release evidence verification to recompute aggregate
  size, throughput, warning, and failure summary metrics from per-case records.

## 0.1.0a40 - 2026-05-14

- Expanded corpus-eval release evidence verification to recompute aggregate
  size, OCR, correction, peak-memory, search, failure, and page-diagnostic
  summary metrics from per-case records.

## 0.1.0a39 - 2026-05-14

- Strengthened release evidence verification to recompute corpus-eval page
  attempt totals, failed-page totals, and max page duration from per-case
  records, rejecting mismatched OCR page diagnostic summaries.

## 0.1.0a38 - 2026-05-14

- Added page status counts, OCR warning counts, retry attempts, and max page
  duration metrics to corpus-eval evidence, and made the release verifier
  require those page-level diagnostics in every corpus-eval case.

## 0.1.0a37 - 2026-05-14

- Made SQLite FTS result ordering deterministic by adding newest-document and
  document-ID tie-breakers after BM25 score for both cleaned and raw searches,
  keeping paginated search windows stable when relevance scores tie.

## 0.1.0a36 - 2026-05-14

- Tightened release evidence verification so eval, corpus-eval, and benchmark
  detail records must include complete case names, tags, warnings, classification
  and search/page diagnostics, benchmark model/chunk/timing metrics, and positive
  size/throughput measurements before release evidence can pass.

## 0.1.0a35 - 2026-05-14

- Added explicit corpus-eval assertions for PDF page-source counts, minimum OCR
  pages, and minimum corrected OCR pages, and enabled them in the shipped
  synthetic PDF/OCR suite so scanned-page coverage regressions fail evaluation
  instead of only changing diagnostics.

## 0.1.0a34 - 2026-05-14

- Strengthened release evidence verification to cross-check eval and corpus-eval
  summaries against per-case details, and benchmark summaries against per-run
  measurements, so hidden case failures or malformed run records cannot pass
  release gates.

## 0.1.0a33 - 2026-05-14

- Added explicit eval evidence artifact metadata and verifier checks so prompt eval,
  corpus-eval, and benchmark JSON distinguish deterministic mock smoke evidence from
  real-provider release evidence, and reject mismatched provider/tier claims.

## 0.1.0a32 - 2026-05-14

- Added total run counts to `GET /runs` responses through a repository-level
  `count_runs` capability so API clients can page through large run histories
  without inferring totals from the current page.

## 0.1.0a31 - 2026-05-14

- Added bounded search facet buckets with `facet_limit` so broad facet queries
  stay predictable on large libraries while source totals continue to report
  the full matching document count.

## 0.1.0a30 - 2026-05-14

- Added classification prefix filters to CLI and API search so users can browse
  Dewey-style result families such as `636` while keeping exact
  `classification_code` filtering available.

## 0.1.0a29 - 2026-05-14

- Expanded broad SQLite FTS query normalization for hyphenated and slash-separated
  compounds so searches such as `follow-up care` can match both separated tokens and
  concatenated document forms like `followup`, while quoted phrase searches remain exact.

## 0.1.0a28 - 2026-05-14

- Improved broad SQLite FTS query normalization so possessive apostrophes in user searches
  such as `children's hospital` and `horse’s gait` do not require a standalone `s` token,
  while preserving explicit quoted/phrase query behavior.

## 0.1.0a27 - 2026-05-14

- Sanitized remaining CLI exception-detail surfaces for database/workspace maintenance,
  import/report validation, search validation, synthetic corpus generation, page manifests,
  and directory-output validation so CLI errors preserve useful context without leaking
  credentials or provider/parser payloads.

## 0.1.0a26 - 2026-05-14

- Sanitized API exception details for readiness, imports, page-manifest reads, upload ingestion,
  and search adapter validation paths so API responses preserve stable validation text without
  leaking credentials or oversized provider/parser error payloads.

## 0.1.0a25 - 2026-05-14

- Extended shared secret redaction to quoted JSON-style secret fields such as
  `"api_key": "..."`, `"token": "..."`, and single-quoted provider payloads, while
  preserving non-secret fields.

## 0.1.0a24 - 2026-05-14

- Extended shared secret redaction to colon-separated API key, token, secret, and password
  formats, including header-like `x-api-key: ...` messages, so logs and sanitized error
  payloads cover more provider and gateway failure shapes.

## 0.1.0a23 - 2026-05-13

- Redacted OpenAI-compatible provider failures before surfacing them from the LLM adapter,
  including non-retriable provider errors and retry-exhaustion errors, without chaining raw
  provider exceptions that may include API keys or tokens.

## 0.1.0a22 - 2026-05-13

- Redacted MarkItDown broad-format child-process failures before returning them across the worker
  queue, preventing optional conversion adapter exceptions from leaking API keys or provider tokens.

## 0.1.0a21 - 2026-05-13

- Redacted PDF OCR page failures before writing page extraction manifests or raising conversion
  errors, preventing Tesseract/adapter exception text from leaking API keys or provider tokens.

## 0.1.0a20 - 2026-05-13

- Redacted CLI `run-retry --queue` enqueue failures before printing command errors or persisting
  retry-run failure text, matching the API/import queue-submission hardening.

## 0.1.0a19 - 2026-05-13

- Redacted conversion, import, and corpus-eval report failure details before writing or returning
  report item errors so API keys and provider tokens cannot leak through shared workflow artifacts.

## 0.1.0a18 - 2026-05-13

- Redacted API run-submission failures before returning 503 responses or persisting failed-run
  errors, preventing queue/backend exception text from exposing API keys or provider tokens.

## 0.1.0a17 - 2026-05-13

- Redacted per-file API batch-upload failure details before returning item-level errors so
  unexpected exceptions cannot echo API keys or provider tokens in batch responses.

## 0.1.0a16 - 2026-05-13

- Rejected malformed and oversized API search queries before storage access so `/search`,
  `/search/results`, and `/search/facets` return stable `invalid_search_query` or
  `search_query_too_large` responses without opening the SQLite-backed search container.

## 0.1.0a15 - 2026-05-13

- Aligned README, release checklist, and threat model secret-scanning guidance with the pinned
  Gitleaks container used by CI/release workflows, with regression coverage to prevent stale local
  binary commands from returning.

## 0.1.0a14 - 2026-05-13

- Replaced the Node-backed Gitleaks GitHub Action in CI/release secret scans with a pinned
  `zricethezav/gitleaks:v8.30.1` container invocation to keep secret scanning independent of
  GitHub Actions Node runtime deprecations.

## 0.1.0a13 - 2026-05-13

- Rejected inverted API search date windows before storage access so `/search`, `/search/results`,
  and `/search/facets` return a stable `invalid_search_window` response for impossible ranges.

## 0.1.0a12 - 2026-05-13

- Validated API export formats before document lookup so unsupported formats return a stable
  `bad_request` response instead of depending on document existence.

## 0.1.0a11 - 2026-05-13

- Bounded API rate-limiter memory growth by pruning expired per-identity buckets even when traffic
  comes from a high churn of unique clients.

## 0.1.0a10 - 2026-05-13

- Added sanitized `Content-Disposition` filenames to API document exports so download clients get
  stable safe names for JSON, text, and Markdown outputs.

## 0.1.0a9 - 2026-05-13

- Added an OCR-correction prompt eval fixture that requires common OCR artifacts to be corrected
  while preserving source facts, with deterministic mock-provider coverage in CI.

## 0.1.0a8 - 2026-05-13

- Hardened API client-IP identity so `X-Forwarded-For` is ignored for rate limiting and audit logs
  unless the connecting proxy is explicitly trusted by CIDR.

## 0.1.0a7 - 2026-05-13

- Hardened CLI/import text and broad-format conversion to reject renamed archives by signature while
  preserving supported ZIP-container document uploads such as `.docx`.

## 0.1.0a6 - 2026-05-13

- Hardened API import status and PDF page-manifest reads to reject symlinked manifest paths before
  opening JSON files.
- Hardened API uploads to reject renamed archives with common archive signatures before persisting
  upload bytes.

## 0.1.0a5 - 2026-05-13

- Added deterministic scanned and mixed embedded/scanned PDF fixtures to
  `generate-corpus` and the shipped synthetic corpus eval suite, covering OCR
  extraction, OCR page metrics, search recall, and classification without
  private documents.
- Added a SQLite durable queue contention regression that verifies concurrent
  workers claim each run exactly once while repository reads and writes continue
  through the same database.
- Added `librarian db-stats` for operator-visible SQLite file/page, row-count,
  and stored-text sizing so large-corpus database growth can be measured from a
  real workspace.
- Added final-assembly render-quality regression coverage for headings, page
  markers, lists, tables, citations, paragraph boundaries, duplicate boundary
  sentences, and assistant artifact removal.
- Added an application-layer `SearchIndex` port and `SearchLibrary` service so
  future semantic or hybrid search adapters can replace SQLite FTS without
  changing API or CLI routes.
- Added release reproducibility notes covering tag/version checks,
  constraints, checksums, attestations, and local rebuild/audit commands.
- Added opt-in OCR preprocessing controls for grayscale, thresholded, and
  deskewed image preparation before Tesseract extraction.
- Added a deterministic noisy scanned-OCR PDF fixture option to synthetic corpus
  generation and the shipped synthetic corpus eval suite.
- Added redacted API security audit log events for authentication failures,
  scope denials, and rate-limit denials.
- Added an opt-in OCR page-image preservation setting for sidecar-backed PDF
  page manifests.
- Added an API endpoint for PDF page manifest status so operators can inspect
  page-level OCR progress and failures without shell access.
- Changed the release workflow to smoke-install built wheels against the
  exported `constraints.txt` dependency pins before publishing artifacts.
- Added durable SQLite API audit events for authentication failures, scope
  denials, and rate-limit denials without storing credential material.
- Added `librarian api-audit` for paginated operator inspection of durable API
  security audit events.
- Added configurable API audit-event retention so long-lived deployments can
  prune old denial records automatically.
- Added a structured API run-event SSE stream for clients that need live JSON
  progress records.
- Escaped source markup in search snippets while preserving trusted match
  highlight tags.
- Hardened workspace restore to reject zip members marked as symlinks.
- Hardened CLI PDF page-manifest inspection to reject symlinked paths before reading manifests.
- Hardened CLI import and directory conversion path handling so manifest, report, and
  `new-directory` output paths keep symlink validation intact.
- Hardened API import path handling so `manifest_path` and `new-directory` `output_dir` symlink
  validation is preserved before conversion starts.

## 0.1.0a4 - 2026-05-13

- Added exact phrase search, search totals, result/facet pagination metadata, raw/cleaned search
  filtering, and more forgiving punctuation/hyphen handling.
- Added durable OCR page manifests with pending/failed/retry state, attempts, duration,
  confidence diagnostics, and `low-confidence` OCR correction mode.
- Added `librarian corpus-eval` and `generate-corpus` coverage for sanitized Markdown, DOCX, and
  embedded-text PDF fixtures, including search recall, output ratio, memory, and timing budgets.
- Added release evidence verification for eval, corpus-eval, and benchmark JSON artifacts,
  including real-provider, version, provenance, timestamp, and consistency checks.
- Hardened API upload/import/search paths, archive rejection, symlink handling, request limits,
  deletion coverage, stable error codes, auth scopes, rate limits, security headers, metrics,
  OpenTelemetry hooks, and readiness checks.
- Added CI/release gates for prompt eval, synthetic corpus eval, secret scanning, Docker image
  scanning, SBOMs, checksums, provenance attestations, and container readiness.
- Strengthened logging and stored-error redaction for API keys, bearer tokens, provider keys, and
  oversized failure messages.

## 0.1.0a3 - 2026-05-09

- Added configurable 1,000-page PDF/OCR defaults, page-aware Markdown output documentation, and
  large-PDF smoke-test guidance.
- Fixed direct PDF extractor defaults to match runtime settings.
- Fixed mock-provider OCR correction so local dry runs do not include correction instructions in
  converted OCR text.
- Added `cmos_v2` and `dewey_v2` prompts as defaults, with stronger OCR cleanup, structure
  preservation, context-handling, and Dewey reference guidance.
- Added final assembly cleanup for echoed context markers, assistant artifacts, duplicate sentences,
  duplicate headers, and boundary whitespace.

## 0.1.0a2 - 2026-05-06

- Implemented real `fast`, `balanced`, and `max-coherence` cleaning behavior.
- Added constant-time API key comparison.
- Stopped retrying non-transient OpenAI-compatible errors such as bad requests and auth failures.
- Added per-page OCR for mixed text/scanned PDFs.
- Expanded DOCX extraction to include tables, headers, and footers.
- Added SQLite busy timeout configuration for API/worker contention.
- Wired processing runs through the advertised extraction, normalization, validation,
  classification, and indexing stages.
- Cached bundled prompt reads.
- Added lightweight binary sniffing for text-family inputs.
- Corrected architecture docs to describe the SQLite-backed alpha content store.

## 0.1.0a1 - 2026-05-06

- Added OSS governance files, templates, Dependabot, and CodeQL.
- Added combined import manifests, resume mode, JSON reports, and run/queue controls.
- Added collision-safe conversion outputs, sidecar metadata, OCR language configuration, and
  conversion failure classification.
- Added API endpoints for imports, document deletion/reprocess, run cancel/retry, and run listing.
- Added release workflow support for GHCR images and SBOM artifacts.
- Initial production rewrite foundation.
- CLI and FastAPI surfaces over shared application services.
- SQLite persistence with append-only migrations.
- Durable optional SQLite run queue and external worker command.
- Prompt/versioned cleaning and Dewey classification stack.
- Export, search, benchmark, and eval commands.
- Fixed Docker runtime data directory permissions.
- Hardened public API binds to require authentication and an import root.
- Made canceled runs terminal and durable workers resilient to failed jobs.
- Scoped chunk IDs to documents while preserving content-hash cache reuse.
- Avoided conversion work for resumed import manifest entries.
- Prevented failed extraction attempts from persisting valid-looking documents.
- Returned controlled errors for malformed SQLite FTS search queries.
- Marked prerelease tags as prereleases in release automation.
- Documented the alpha release dependency policy.
