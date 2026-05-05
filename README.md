# Librarian

Librarian is a local-first corpus cleaner and organizer. It ingests messy transcripts and documents, cleans them with an LLM while preserving source fidelity, classifies them into a library taxonomy, and exposes the same engine through both a CLI and an HTTP API.

This repository is the production rewrite of an earlier prototype. The architecture is intentionally hexagonal: the domain and application services do not depend on CLI, API, storage, or LLM vendor details.

## Status

Early foundation. The current implementation includes the project skeleton, domain model, ports, deterministic chunking, validation, prompt assets, CLI/API entry points, and test scaffolding.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,pdf]"
```

## CLI

```bash
librarian version
librarian init
librarian migrate
librarian ingest path/to/transcript.txt
librarian process doc_...
librarian worker --once
librarian status run_...
librarian list
librarian show doc_...
librarian search "horse training"
librarian export doc_... --output cleaned.txt
librarian export doc_... --format md --output cleaned.md
librarian benchmark
librarian eval examples/eval_cases.json
librarian chunk path/to/transcript.txt
librarian api
```

## API

```bash
uvicorn librarian.api.app:create_app --factory --host 127.0.0.1 --port 8080
```

Initial endpoints:

- `GET /health`
- `GET /version`
- `POST /documents`
- `GET /documents`
- `GET /documents/{id}`
- `POST /runs`
- `GET /runs/{id}`
- `GET /runs/{id}/events`
- `GET /runs/{id}/events/stream`
- `GET /documents/{id}/content`
- `GET /documents/{id}/export?format=json|txt|md`
- `POST /search`

If `LIBRARIAN_API_KEY` is set, requests other than `/health` and `/version` must include `x-api-key`.

By default, API-created runs execute in-process with bounded concurrency. To run the API and
workers as separate processes, set `LIBRARIAN_JOB_BACKEND=sqlite`, start the API, and run one or
more workers:

```bash
LIBRARIAN_JOB_BACKEND=sqlite librarian api
librarian worker
```

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/PRD.md](docs/PRD.md),
[docs/MIGRATIONS.md](docs/MIGRATIONS.md), and [docs/RELEASE.md](docs/RELEASE.md).

## Privacy

Librarian stores data locally by default. Text is only sent to a configured model provider when a processing run requires LLM work. API keys belong in environment variables or `.env`, never in Git.
