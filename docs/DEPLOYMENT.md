# Deployment

Librarian can run as a self-contained Mac app, a local CLI, a single API process, or a split
API/worker service.

## Mac App

Release builds of the native macOS app bundle the entire backend (a relocatable Python runtime plus
the `nampara-librarian` wheel) inside `Librarian.app`. The app launches `python -m librarian api`
on a loopback port and supervises it; data lives in `~/Library/Application Support/Librarian`, and
an optional `.env` file there accepts every `LIBRARIAN_*` setting documented below. Distribution
DMGs are produced by `.github/workflows/macapp.yml`; build, signing, and notarization details are
in [apps/macos/README.md](../apps/macos/README.md).

## Docker Compose

```bash
export LIBRARIAN_API_KEY=change-me
docker compose up --build
curl http://127.0.0.1:8080/health
```

Compose runs:

- `api`: FastAPI service on port `8080`.
- `worker`: durable SQLite queue worker.
- `librarian-data`: persistent volume mounted at `/data`.

The compose file defaults to `LIBRARIAN_JOB_BACKEND=sqlite`, so API requests enqueue work and the
worker processes runs independently. Compose waits for the API `/ready` check before starting the
worker, and the image exposes the same readiness check for container orchestrators.
Compose drops Linux capabilities, sets `no-new-privileges`, runs with a read-only root filesystem,
limits process counts, and provides `/tmp` as tmpfs while keeping the persistent `/data` volume
writable.
Compose restarts API and worker services unless stopped; the worker has a longer shutdown grace
period so in-flight runs can finish or be reclaimed by the durable queue lease.
Compose also rotates Docker JSON logs at 10 MiB per file with five retained files per service.
Compose requires `LIBRARIAN_API_KEY` or `LIBRARIAN_API_KEYS`; authenticated requests must send
one configured value as `x-api-key`.
Inspect durable queue state with `librarian admin queue --limit 100 --offset 0`.

## Direct Docker Run

The published image starts the API on `0.0.0.0:8080`. Public binds require both an API key and an
import root:

```bash
docker run --rm -p 8080:8080 \
  -e LIBRARIAN_API_KEY=change-me \
  -e LIBRARIAN_API_IMPORT_ROOT=/data/imports \
  ghcr.io/nampara-ai/librarian:v1.1.7
```

## Environment

Common production settings:

```bash
LIBRARIAN_API_KEY=change-me
LIBRARIAN_API_KEYS=
LIBRARIAN_API_KEY_SHA256=
LIBRARIAN_API_KEY_HASHES=
LIBRARIAN_API_IMPORT_ROOT=/data/imports
LIBRARIAN_API_MAX_REQUEST_BYTES=1073741824
LIBRARIAN_API_MAX_UPLOAD_BYTES=104857600
LIBRARIAN_API_MAX_BATCH_FILES=100
LIBRARIAN_API_MAX_BATCH_BYTES=1073741824
LIBRARIAN_API_MAX_IMPORT_FILES=1000
LIBRARIAN_API_MAX_IMPORT_BYTES=1073741824
LIBRARIAN_API_MAX_IMPORT_MANIFEST_BYTES=10485760
LIBRARIAN_API_MAX_CONTENT_CHARS=2097152
LIBRARIAN_API_RATE_LIMIT_PER_MINUTE=0
LIBRARIAN_LOG_FORMAT=json  # json or text
LIBRARIAN_LOG_LEVEL=INFO  # DEBUG, INFO, WARNING, ERROR, or CRITICAL
LIBRARIAN_OTEL_ENABLED=false
LIBRARIAN_OTEL_SERVICE_NAME=librarian
LIBRARIAN_OTEL_ENDPOINT=
LIBRARIAN_OTEL_HEADERS=
LIBRARIAN_DATA_DIR=/data
LIBRARIAN_DATABASE_PATH=/data/librarian.sqlite  # optional; defaults to <LIBRARIAN_DATA_DIR>/librarian.sqlite
LIBRARIAN_JOB_BACKEND=sqlite
LIBRARIAN_LLM_PROVIDER=openai-compatible  # mock or openai-compatible
LIBRARIAN_LLM_MODEL=gpt-4.1-mini
LIBRARIAN_LLM_PROMPT_COST_PER_1K_TOKENS_USD=0
LIBRARIAN_LLM_COMPLETION_COST_PER_1K_TOKENS_USD=0
LIBRARIAN_LLM_MAX_PROMPT_CHARS=2097152
LIBRARIAN_LLM_MAX_RESPONSE_CHARS=2097152
LIBRARIAN_CLEANING_PROMPT_VERSION=cmos_v2  # cmos_v1 or cmos_v2
LIBRARIAN_CLASSIFICATION_PROMPT_VERSION=dewey_v2  # dewey_v1 or dewey_v2
LIBRARIAN_OCR_TIMEOUT_SECONDS=120
LIBRARIAN_OCR_PRESERVE_PAGE_IMAGES=false
LIBRARIAN_OCR_ROTATION_RETRY=false
LIBRARIAN_OCR_PDF_DPI=200
LIBRARIAN_OCR_PDF_MAX_PAGES=1000
LIBRARIAN_OCR_LLM_CORRECTION=always
LIBRARIAN_OCR_LLM_MODEL=
LIBRARIAN_OCR_LOW_CONFIDENCE_THRESHOLD=85
LIBRARIAN_OCR_PAGE_CONCURRENCY=2
LIBRARIAN_OCR_FAIL_ON_PAGE_ERROR=true
LIBRARIAN_UNIVERSAL_MAX_INPUT_BYTES=52428800
LIBRARIAN_UNIVERSAL_TIMEOUT_SECONDS=120
OPENAI_API_KEY=...
```

Use `LIBRARIAN_API_KEYS=old-key,new-key` during key rotation. `LIBRARIAN_API_KEY` remains supported
for single-key deployments, and both settings can be active at the same time. `LIBRARIAN_API_KEYS`
also supports scoped entries: `read:<key>` can call read endpoints plus search, while `write:<key>`
or an unprefixed key can call every endpoint. Operational endpoints such as `/config`, `/metrics`,
and `/metrics/prometheus` require write scope because they expose deployment settings and telemetry.
For environments where plaintext API keys should not be present in process configuration, use
`LIBRARIAN_API_KEY_SHA256=<sha256>` or scoped `LIBRARIAN_API_KEY_HASHES=read:<sha256>,write:<sha256>`.
Clients still send the original key value in `x-api-key` or `Authorization: Bearer <key>`;
Librarian hashes the supplied value before comparison.
Authentication failures, scope denials, and rate-limit denials are emitted as `librarian.api`
warning log events without API key material and persisted to the local SQLite `api_audit_events`
table with method, path, client host, credential-presence/scope metadata, retry-after seconds, and
timestamp. Audit rows older than `LIBRARIAN_API_AUDIT_RETENTION_DAYS=90` are pruned when new audit
events are written; set it to `0` to keep rows indefinitely. Use `librarian admin api-audit --json` for a
paginated operator view. Retain those logs and database backups when exposing the API beyond
localhost.
Set `LIBRARIAN_API_MAX_REQUEST_BYTES` to reject oversized HTTP requests by `Content-Length` or
streamed body bytes before routing or multipart parsing completes.
Set `LIBRARIAN_API_MAX_BATCH_FILES` and `LIBRARIAN_API_MAX_BATCH_BYTES` to cap multipart batch
uploads before files are persisted.
Set `LIBRARIAN_API_MAX_IMPORT_FILES` and `LIBRARIAN_API_MAX_IMPORT_BYTES` to cap server-side file
imports before conversion starts.
Set `LIBRARIAN_API_MAX_IMPORT_MANIFEST_BYTES` to cap import manifest validation, resume, and
`/imports/status` reads. Import manifest paths must not be symlinks, and manifest writes use atomic
same-directory replacement.
Set `LIBRARIAN_API_MAX_CONTENT_CHARS` to cap each JSON `/documents/{id}/content` page. Use
`/documents/{id}/export` for intentional full-output downloads.
Set `LIBRARIAN_API_RATE_LIMIT_PER_MINUTE` to a positive value to enable a per-process fixed-window
request limit. Authenticated requests are keyed by the supplied API key or bearer token;
unauthenticated requests are keyed by client IP. `0` disables rate limiting, which is the default
for local CLI-adjacent deployments. Expired per-identity windows are pruned during request handling
so high-churn client traffic does not retain old limiter buckets indefinitely. `X-Forwarded-For` is
ignored unless `LIBRARIAN_API_TRUSTED_PROXY_CIDRS` is set to a comma-separated list of proxy IPs or
CIDRs. Only requests from those proxy networks can supply forwarded client IPs for rate-limit and
audit identity.

## Health And Metrics

- `GET /health`: process health.
- `GET /ready`: process readiness plus data-directory writability, SQLite integrity, and migration
  metadata verification; returns `503` if storage is not writable or the database is missing,
  corrupt, or fails verification.
- `GET /metrics`: in-memory request counters, latency summary, run-stage timings, terminal run
  counts, durable queue claims, queue failures, average queue wait, conversion failures by type,
  OCR page throughput/failures/corrections, and provider-reported LLM token usage when available.
- `GET /metrics/prometheus`: Prometheus text exposition for the same in-memory counters.

Metrics are process-local. For multi-replica deployments, scrape every API process separately or
replace the metrics adapter with a dedicated telemetry backend.

OpenTelemetry tracing is opt-in. Install the extra with `pip install "nampara-librarian[otel]"`,
then set `LIBRARIAN_OTEL_ENABLED=true` and, for OTLP/HTTP collectors, `LIBRARIAN_OTEL_ENDPOINT`.
Use `LIBRARIAN_OTEL_HEADERS` for comma-separated `key=value` exporter headers. When enabled,
Librarian emits API request spans, processing stage spans with run/document IDs, and queue
processing spans for in-process jobs or workers constructed with the configured tracer.

LLM token counters are populated when the provider response includes usage metadata. Estimated cost
remains zero unless `LIBRARIAN_LLM_PROMPT_COST_PER_1K_TOKENS_USD` and
`LIBRARIAN_LLM_COMPLETION_COST_PER_1K_TOKENS_USD` are set for the configured model.
Set `LIBRARIAN_LLM_MAX_PROMPT_CHARS` to reject oversized system plus user prompts before provider
network calls.
Set `LIBRARIAN_LLM_MAX_RESPONSE_CHARS` to cap raw model responses before cleaned text or
classification summaries are validated and persisted.

Processing runs also emit structured `run_stage_finished` logs with `run_id`, `document_id`,
`stage`, `status`, and `duration_ms`. These logs intentionally avoid source text and generated
document content. The JSON logger redacts common credential patterns such as API-key assignments,
bearer tokens, and `sk-...` provider keys before writing log records.

API responses include conservative default HTTP security headers: `X-Content-Type-Options:
nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, a restrictive
`Permissions-Policy`, and `Cache-Control: no-store`.

## Storage

SQLite is the first durable backend. Use a persistent disk or volume and run `librarian migrate`
before starting services in environments where startup ordering matters. SQLite is suitable for
single-node production deployments; a networked queue/database adapter should be added before horizontal
multi-node production.

For long-lived databases, run maintenance during a quiet window:

```bash
librarian admin workspace-backup /backups/librarian-workspace-$(date +%Y%m%d%H%M%S).zip
librarian admin db-backup /backups/librarian-$(date +%Y%m%d%H%M%S).sqlite
librarian admin db-check
librarian admin db-stats
librarian admin db-maintain
librarian admin db-maintain --vacuum
```

Librarian opens SQLite connections with WAL mode, foreign keys, `synchronous=NORMAL`, and a 5-second
busy timeout. `workspace-backup` creates a zip archive containing a consistent database snapshot plus
data-dir files such as API uploads. Workspace archive destinations must not be symlinks or cross
symlinked parents. Symlinked files under the data directory are skipped so backups do not copy
targets outside the workspace. The database backup command uses SQLite's online backup API,
rejects symlink destinations or symlinked destination parents, and verifies the copied database with
`PRAGMA integrity_check` before replacing the destination path. The maintenance command runs SQLite
optimize and a WAL checkpoint.
`--vacuum` additionally compacts the database file and can take longer on large corpora.
Use `librarian admin db-stats` to inspect current database file bytes, WAL/SHM sidecar bytes, SQLite page
usage, row counts, source-file bytes, and stored raw/chunk/cleaned text bytes. For large corpora,
record this output before and after representative imports to estimate local disk growth; the
stored-text totals show how much of the database is raw extraction, chunk, cache, and cleaned-output
payload rather than schema/index overhead. `librarian admin db-stats --json` is suitable for deployment
runbooks and periodic capacity snapshots.
To restore, stop API and worker processes first, then run:

```bash
librarian admin db-restore /backups/librarian-20260512120000.sqlite --yes
```

The restore command verifies the backup, rejects symlink destinations or symlinked destination
parents, and removes stale SQLite WAL sidecars around the replacement. Run `librarian admin db-check` after
restore to verify integrity, foreign keys, and migration metadata.
To restore a full workspace archive, stop API and worker processes first, then run:

```bash
librarian admin workspace-restore /backups/librarian-workspace-20260512120000.zip --yes
```

Workspace restore rejects archives whose total uncompressed size exceeds 10 GiB by default. Use
`--max-expanded-bytes` to raise or lower that limit for your environment. Restore also rejects
oversized manifests, duplicate archive paths, unsafe member paths, symlink archive members, and
archives with excessive file counts before applying data files. Restore data directories must not
be symlinks or cross symlinked parents.

## Upload And Archive Policy

Librarian accepts individual supported document files. Archive/container formats such as `.zip`,
`.tar`, `.7z`, and `.rar` are rejected by default rather than recursively unpacked. Scan and unpack
archives outside Librarian using your approved malware tooling, then import the extracted files from
`LIBRARIAN_API_IMPORT_ROOT`. Common archive signatures are also rejected when they appear under
non-container document extensions, while supported ZIP-container document types such as `.docx`,
`.pptx`, `.xlsx`, and `.epub` remain accepted by extension.

See `docs/OPERATIONS.md` for API import trust boundaries, storage runbooks, and residual hosted-mode risks.
