# Deployment

Librarian can run as a local CLI, a single API process, or a split API/worker service.

## Docker Compose

```bash
docker compose up --build
curl http://127.0.0.1:8080/health
```

Compose runs:

- `api`: FastAPI service on port `8080`.
- `worker`: durable SQLite queue worker.
- `librarian-data`: persistent volume mounted at `/data`.

The compose file defaults to `LIBRARIAN_JOB_BACKEND=sqlite`, so API requests enqueue work and the
worker processes runs independently.

## Environment

Common production settings:

```bash
LIBRARIAN_API_KEY=change-me
LIBRARIAN_LOG_FORMAT=json
LIBRARIAN_LOG_LEVEL=INFO
LIBRARIAN_DATA_DIR=/data
LIBRARIAN_DATABASE_PATH=/data/librarian.sqlite
LIBRARIAN_JOB_BACKEND=sqlite
LIBRARIAN_LLM_PROVIDER=openai-compatible
LIBRARIAN_LLM_MODEL=gpt-4.1-mini
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
