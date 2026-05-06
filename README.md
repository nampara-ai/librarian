# Librarian

Librarian is a local-first corpus cleaner and organizer. It ingests messy transcripts and documents, cleans them with an LLM while preserving source fidelity, classifies them into a library taxonomy, and exposes the same engine through both a CLI and an HTTP API.

This repository is the production rewrite of an earlier prototype. The architecture is intentionally hexagonal: the domain and application services do not depend on CLI, API, storage, or LLM vendor details.

## Status

`v0.1.0a1` is the first public alpha. It includes local CLI workflows, a FastAPI service,
directory conversion/import, durable SQLite-backed processing runs, OCR/broad-format extraction,
search/export, release automation, and OSS governance files.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,pdf]"
# broad conversion and OCR:
pip install -e ".[dev,all]"
```

## CLI

```bash
librarian version
librarian init
librarian migrate
librarian convert path/to/report.docx --format md --output converted/report.md
librarian convert-dir path/to/folder --format md --output-mode subdirectory
librarian import path/to/folder --recursive --format md --process
librarian import path/to/folder --format txt --queue
librarian ingest path/to/transcript.txt
librarian process doc_...
librarian worker --once
librarian status run_...
librarian list
librarian show doc_...
librarian search "horse training"
librarian export doc_... --output cleaned.txt
librarian export doc_... --format md --output cleaned.md
librarian benchmark --repeats 3 --output benchmark.json
librarian benchmark --input-path examples/benchmark_text.txt
librarian eval examples/eval_cases.json --output eval.json
librarian chunk path/to/transcript.txt
librarian api
```

## API

```bash
uvicorn librarian.api.app:create_app --factory --host 127.0.0.1 --port 8080
```

Initial endpoints:

- `GET /health`
- `GET /metrics`
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

## Provider Evals And Benchmarks

The mock provider is deterministic and used by CI. To evaluate a real OpenAI-compatible provider:

```bash
export LIBRARIAN_LLM_PROVIDER=openai-compatible
export LIBRARIAN_LLM_MODEL=gpt-4.1-mini
export OPENAI_API_KEY=...
librarian eval examples/eval_cases.json --output eval-openai.json
librarian benchmark --input-path examples/benchmark_text.txt --repeats 3 --output bench-openai.json
```

Use the resulting JSON to compare `LIBRARIAN_CHUNK_TARGET_CHARS`,
`LIBRARIAN_CHUNK_OVERLAP_CHARS`, `LIBRARIAN_LLM_MAX_CONCURRENCY`, and
`LIBRARIAN_COHERENCE_MODE`.

## Architecture

Start with [docs/QUICKSTART.md](docs/QUICKSTART.md). See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/PRD.md](docs/PRD.md),
[docs/MIGRATIONS.md](docs/MIGRATIONS.md), [docs/CONVERSION.md](docs/CONVERSION.md),
[docs/EVALUATION.md](docs/EVALUATION.md), [docs/PERFORMANCE.md](docs/PERFORMANCE.md),
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md), [docs/ROADMAP.md](docs/ROADMAP.md), and
[docs/API.md](docs/API.md), [docs/RELEASE.md](docs/RELEASE.md), and
[docs/SUPPLY_CHAIN.md](docs/SUPPLY_CHAIN.md). Package naming stability is tracked in
[docs/NAMING.md](docs/NAMING.md).

## Docker

```bash
export LIBRARIAN_API_KEY=change-me
docker compose up --build
```

The compose stack runs the API plus a separate SQLite-backed worker. API requests
other than `/health` and `/version` require `x-api-key: $LIBRARIAN_API_KEY`.

For direct image runs, also set an import root because the image binds publicly by default:

```bash
docker run --rm -p 8080:8080 \
  -e LIBRARIAN_API_KEY=change-me \
  -e LIBRARIAN_API_IMPORT_ROOT=/data/imports \
  ghcr.io/nampara-ai/librarian:v0.1.0a1
```

## Privacy

Librarian stores data locally by default. Text is only sent to a configured model provider when a processing run requires LLM work. API keys belong in environment variables or `.env`, never in Git.
