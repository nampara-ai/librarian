# API Surface

The FastAPI service exposes the same core workflows as the CLI.
Requests above `LIBRARIAN_API_MAX_REQUEST_BYTES` are rejected before routing, either from
`Content-Length` or while streaming the request body.

## Documents

- `POST /documents`: upload one file. Uploads are capped by
  `LIBRARIAN_API_MAX_UPLOAD_BYTES`. Upload storage rejects symlinked `data_dir`/`uploads` paths and
  symlinked parents before persisting files. Known archive/container extensions are rejected before
  upload bytes are written, and common archive signatures are rejected on the first upload chunk
  before any upload bytes are persisted. Supported ZIP-container document types such as `.docx`,
  `.pptx`, `.xlsx`, and `.epub` remain accepted by extension.
- `POST /documents/batch`: upload multiple files in one request. Each file returns either a
  `document` object or an `error` object; one failed file does not roll back successful ingests.
  Batch size is capped by `LIBRARIAN_API_MAX_BATCH_FILES` and
  `LIBRARIAN_API_MAX_BATCH_BYTES`. Per-file result filenames are normalized the same way as stored
  upload filenames, including failed items.
- `GET /documents?limit=100&offset=0`: list documents.
- `GET /documents/{id}`: document metadata.
- `DELETE /documents/{id}`: delete a document and dependent records.
- `POST /documents/{id}/reprocess`: create a new processing run.
- `GET /documents/{id}/content?offset=0&limit=...`: latest cleaned output as a bounded JSON page.
  When `limit` is omitted, the response is capped by `LIBRARIAN_API_MAX_CONTENT_CHARS`; full
  downloads should use `/export`.
- `GET /documents/{id}/export?format=json|txt|md`: export latest output. JSON exports return
  `document_id`, `filename`, `classification`, and `text`; `txt` and `md` exports return
  `text/plain` and `text/markdown` bodies respectively. Export responses include sanitized
  `Content-Disposition` filenames with UTF-8 `filename*` values for download clients. Unsupported
  `format` values return `code: "bad_request"` before document lookup.

## Imports

- `POST /imports`: convert a server-local file or directory, ingest outputs, and optionally process or
  queue runs.
  Directory imports are capped by `LIBRARIAN_API_MAX_IMPORT_FILES` and
  `LIBRARIAN_API_MAX_IMPORT_BYTES`.

Example:

```json
{
  "source_dir": "/data/input",
  "format": "md",
  "output_mode": "subdirectory",
  "recursive": true,
  "overwrite": false,
  "processing_mode": "queue"
}
```

`processing_mode` may be `none`, `process`, or `queue`.

The `source_dir` field accepts either a file path or a directory path under
`LIBRARIAN_API_IMPORT_ROOT`. The name is kept for API compatibility.

Set `manifest_path` to a JSON file under `LIBRARIAN_API_IMPORT_ROOT` to persist incremental import
progress. Existing manifest files must be Librarian import reports so unrelated JSON files are not
overwritten. `resume: true` reuses successful records from that manifest.
`GET /imports/status?limit=500&offset=0` reads the same manifest and returns summary counts plus a
bounded page of per-file records. Import manifest validation, resume, and status reads are capped by
`LIBRARIAN_API_MAX_IMPORT_MANIFEST_BYTES`. Manifest paths must not be symlinks or cross symlinked
parents, and manifest writes are atomic replacements. `new-directory` output paths are also
validated before conversion starts and must not be symlinks or cross symlinked parents.
- `GET /imports/page-manifest?manifest_path=...`: read a PDF page extraction manifest under
  `LIBRARIAN_API_IMPORT_ROOT` and return page-level OCR/conversion progress, including status/source
  counts, warning counts, attempts, average OCR confidence, corrected page count, optional
  `image_path`, and a bounded page list. The manifest path must not be a symlink or cross symlinked
  parents. Use `failures_only=true` to inspect failed pages.

## Runs

- `POST /runs`: create a processing run for one document.
- `GET /runs?limit=100&offset=0`: list runs. Responses include `total`, `limit`, and `offset`
  so operator clients can page through large run histories.
- `GET /runs/{id}`: run status.
- `POST /runs/{id}/cancel`: mark a run canceled.
- `POST /runs/{id}/retry`: replay a failed run as a new run.
- `GET /runs/{id}/events?limit=500&offset=0`: run events. Responses include `events`,
  `limit`, and `offset`; `limit` is capped at 1,000.
- `GET /runs/{id}/events/records`: run events as structured JSON records with `stage`,
  `message`, `created_at`, and `sequence`. Supports the same `limit` and `offset` query
  parameters and response metadata as `/events`.
- `GET /runs/{id}/events/stream`: server-sent event stream with `text/event-stream` responses.
- `GET /runs/{id}/events/records/stream`: server-sent event stream of structured JSON event
  records. Each `run-event` payload includes `stage`, `message`, `created_at`, and `sequence`;
  the stream closes with a `done` event.

## Search And Metadata

- `POST /search`: full-text search over cleaned outputs. Queries are normalized so punctuation
  and hyphenated phrases such as `follow-up care` remain searchable without FTS syntax errors.
  Quote phrases such as `"follow-up care"` to require adjacent terms in that order.
  Set `phrase: true`, or use CLI `--phrase`, to treat the whole query as one adjacent phrase
  without manually adding quotes.
  Queries longer than 4,096 characters are rejected before FTS execution.
  Optional paging and filters: `limit`, `offset`, `classification_code`,
  `classification_prefix`, `document_status`, `filename_contains`, `created_after`,
  `created_before`, `phrase`, and `scope` (`cleaned` or `raw`; default `cleaned`). Exact
  classification filters can be combined with prefix filters; prefix filters match Dewey-style
  families such as `636` against `636.1`. Responses include `total`, the full matching document
  count before `limit`/`offset` are applied. Date filters are ISO-8601 document creation
  timestamps; inverted `created_after`/`created_before` windows return
  `code: "invalid_search_window"` before storage access.
- `POST /search/results`: ranked full-text search results with `document_id`, `run_id`,
  source metadata, classification metadata, `source`, `snippet`, and `score`. Raw-source search
  results use `source: "raw"` and `run_id: null`. Snippets escape source markup and preserve
  Librarian-owned `<mark>...</mark>` match highlights.
  Responses include the same `total`, `limit`, and `offset` pagination metadata as `/search`.
- `POST /search/facets`: facet counts for a query grouped by classification, document status,
  search source, and filename. Facets honor the same query filters as `/search`, so counts reflect
  the active result set. `facet_limit` caps classification and filename bucket counts, defaults to
  `50`, and is capped at `500`; source totals still report the full matching document count.
- `GET /classifications`: built-in Dewey labels.
- `GET /config`: selected runtime settings.
- `GET /health`: health check.
- `GET /ready`: readiness check that verifies the runtime data directory is writable, plus SQLite
  integrity and migration metadata.
- `GET /metrics`: process-local request, run-stage, queue, conversion failure, OCR throughput,
  and LLM usage metrics.
- `GET /metrics/prometheus`: the same process-local metrics in Prometheus text format.
  API requests, in-process run stages, and queue processing can also be exported through
  OpenTelemetry when the `otel` extra and `LIBRARIAN_OTEL_ENABLED=true` are configured.

## Errors

All HTTP responses, including text exports, Prometheus metrics, and server-sent event streams, carry
the API security headers configured by the service.

JSON errors include the human-readable `detail` field and a stable machine-readable `code` field.
The OpenAPI schema documents this shared error shape across API operations.
Validation errors use `code: "validation_error"` and keep FastAPI's structured validation detail.
Unhandled server exceptions return `code: "server_error"` with a generic message so internal
exception text and provider credentials are not exposed to API clients.
When `LIBRARIAN_API_RATE_LIMIT_PER_MINUTE` is enabled, exhausted clients receive HTTP 429 with
`code: "rate_limited"` and a `Retry-After` header. Unauthenticated rate limits use the socket peer
IP unless `LIBRARIAN_API_TRUSTED_PROXY_CIDRS` explicitly trusts the connecting proxy; untrusted
`X-Forwarded-For` headers are ignored.
When scoped keys are configured with `LIBRARIAN_API_KEYS=read:<key>,write:<key>`, read-only keys can
call `GET`, `HEAD`, `OPTIONS`, and search endpoints except operational `/config`, `/metrics`, and
`/metrics/prometheus`; blocked writes or operational reads return HTTP 403 with
`code: "insufficient_scope"`.
Use `LIBRARIAN_API_KEY_SHA256` or scoped `LIBRARIAN_API_KEY_HASHES` to configure SHA-256 hashes
instead of plaintext API keys. Clients still send the original key in `x-api-key` or
`Authorization: Bearer <key>`.
When API credentials are configured, `/openapi.json` itself requires authentication, and the
generated schema advertises both authentication schemes on protected endpoints while leaving
`/health`, `/ready`, and `/version` unauthenticated.

Example:

```json
{
  "detail": "Document not found",
  "code": "not_found"
}
```

## CLI Parity

- `librarian import` maps to `POST /imports`.
- `librarian process` maps to `POST /runs`.
- `librarian run-cancel` maps to `POST /runs/{id}/cancel`.
- `librarian run-retry` maps to `POST /runs/{id}/retry`.
- `librarian search` maps to `POST /search`; `--details` maps to `POST /search/results`
  and prints the same total/limit/offset pagination metadata. `--phrase` maps to `phrase: true`.
- `librarian export` maps to `GET /documents/{id}/export`.
