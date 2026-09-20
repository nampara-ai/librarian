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

PDF extraction is page-aware. Embedded-text pages use embedded extraction; empty or scanned pages are OCRed. Long OCR jobs write `<output>.pages.json` manifests when conversion sidecars are enabled. These manifests record page status, source, OCR confidence, retry attempts, correction state, warning codes, and optional preserved page image paths.

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
