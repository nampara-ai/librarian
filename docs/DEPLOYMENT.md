# Deployment

Librarian can run as a local CLI, a single API process, or a split API/worker service.

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
worker processes runs independently.
Compose requires `LIBRARIAN_API_KEY`; authenticated requests must send
`x-api-key: $LIBRARIAN_API_KEY`.

## Direct Docker Run

The published image starts the API on `0.0.0.0:8080`. Public binds require both an API key and an
import root:

```bash
docker run --rm -p 8080:8080 \
  -e LIBRARIAN_API_KEY=change-me \
  -e LIBRARIAN_API_IMPORT_ROOT=/data/imports \
  ghcr.io/nampara-ai/librarian:v0.1.0a2
```

## Environment

Common production settings:

```bash
LIBRARIAN_API_KEY=change-me
LIBRARIAN_API_IMPORT_ROOT=/data/imports
LIBRARIAN_API_MAX_UPLOAD_BYTES=104857600
LIBRARIAN_LOG_FORMAT=json
LIBRARIAN_LOG_LEVEL=INFO
LIBRARIAN_DATA_DIR=/data
LIBRARIAN_DATABASE_PATH=/data/librarian.sqlite
LIBRARIAN_JOB_BACKEND=sqlite
LIBRARIAN_LLM_PROVIDER=openai-compatible
LIBRARIAN_LLM_MODEL=gpt-4.1-mini
LIBRARIAN_OCR_TIMEOUT_SECONDS=120
LIBRARIAN_OCR_PDF_DPI=200
LIBRARIAN_OCR_PDF_MAX_PAGES=1000
LIBRARIAN_OCR_LLM_CORRECTION=always
LIBRARIAN_OCR_LLM_MODEL=
LIBRARIAN_OCR_PAGE_CONCURRENCY=2
LIBRARIAN_OCR_FAIL_ON_PAGE_ERROR=true
LIBRARIAN_UNIVERSAL_MAX_INPUT_BYTES=52428800
LIBRARIAN_UNIVERSAL_TIMEOUT_SECONDS=120
OPENAI_API_KEY=...
```

## Health And Metrics

- `GET /health`: process health.
- `GET /metrics`: in-memory request counters and latency summary.

Metrics are process-local. For multi-replica deployments, scrape every API process separately or
replace the metrics adapter with a dedicated telemetry backend.

## Storage

SQLite is the first durable backend. Use a persistent disk or volume and run `librarian migrate`
before starting services in environments where startup ordering matters. SQLite is suitable for
single-node alpha deployments; a networked queue/database adapter should be added before horizontal
multi-node production.
