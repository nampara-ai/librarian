# Changelog

## Unreleased

- Added deterministic scanned and mixed embedded/scanned PDF fixtures to
  `generate-corpus` and the shipped synthetic corpus eval suite, covering OCR
  extraction, OCR page metrics, search recall, and classification without
  private documents.

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
