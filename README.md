# Librarian

Librarian is a local-first corpus cleaner and organizer. It ingests messy transcripts and documents, cleans them with an LLM while preserving source fidelity, classifies them into a library taxonomy, and exposes the same engine through both a CLI and an HTTP API.

This repository is the production rewrite of an earlier prototype. The architecture is intentionally hexagonal: the domain and application services do not depend on CLI, API, storage, or LLM vendor details.

## Status

`v0.1.0a40` is the latest public alpha. It includes local CLI workflows, a FastAPI service,
file and directory conversion/import, durable SQLite-backed processing runs, page-aware OCR and
broad-format extraction, the upgraded v2 prompt stack, search/export, release automation, and OSS
governance files.

## Install

From a downloaded release wheel:

```bash
python -m venv .venv
source .venv/bin/activate
pip install "nampara_librarian-0.1.0a40-py3-none-any.whl[all]"
```

From a source checkout:

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
librarian db-stats
librarian db-maintain
librarian convert path/to/report.docx --format md --output converted/report.md
librarian convert-dir path/to/folder --format md --output-mode subdirectory
librarian import path/to/large.md --process
librarian import path/to/folder --recursive --format md --process
librarian import path/to/folder --format txt --queue
librarian ingest path/to/transcript.txt
librarian process doc_...
librarian worker --once
librarian status run_... --event-limit 500 --event-offset 0
librarian list
librarian show doc_...
librarian search "horse training"
librarian search "horse training" --details
librarian search "rough OCR phrase" --scope raw --details
librarian search "horse training" --classification-code 636.1 --filename-contains notes
librarian search "horse training" --classification-prefix 636 --details
librarian search "horse training" --limit 20 --offset 20 --created-after 2026-01-01T00:00:00Z
librarian export doc_... --output cleaned.txt
librarian export doc_... --format md --output cleaned.md
librarian benchmark --repeats 3 --output benchmark.json
librarian benchmark --input-path examples/benchmark_text.txt
librarian eval examples/eval_cases.json --output eval.json
librarian generate-corpus --output-dir .librarian/synthetic-corpus --include-docx --include-pdf --include-scanned-pdf
librarian corpus-eval examples/corpus_eval_cases.json --output corpus-eval.json --overwrite
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
- `GET /runs/{id}/events?limit=500&offset=0`
- `GET /runs/{id}/events/records?limit=500&offset=0`
- `GET /runs/{id}/events/stream`
- `GET /runs/{id}/events/records/stream`
- `GET /documents/{id}/content`
- `GET /documents/{id}/export?format=json|txt|md`
- `POST /search`

If `LIBRARIAN_API_KEY` or `LIBRARIAN_API_KEYS` is set, requests other than `/health`, `/ready`, and
`/version` must include one configured value as `x-api-key`.

By default, API-created runs execute in-process with bounded concurrency. To run the API and
workers as separate processes, set `LIBRARIAN_JOB_BACKEND=sqlite`, start the API, and run one or
more workers:

```bash
LIBRARIAN_JOB_BACKEND=sqlite librarian api
librarian worker
librarian queue --limit 100 --offset 0
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
`LIBRARIAN_CHUNK_OVERLAP_CHARS`, `LIBRARIAN_LLM_MAX_CONCURRENCY`,
`LIBRARIAN_COHERENCE_MODE`, `LIBRARIAN_CLEANING_PROMPT_VERSION`, and
`LIBRARIAN_CLASSIFICATION_PROMPT_VERSION`. Prompt-version settings accept the prompt files bundled
with the package: `cmos_v1` or `cmos_v2` for cleaning, and `dewey_v1` or `dewey_v2` for
classification.

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
other than `/health`, `/ready`, and `/version` require a configured `x-api-key`. Use
`LIBRARIAN_API_KEYS=old-key,new-key` for key rotation.

For direct image runs, also set an import root because the image binds publicly by default:

```bash
docker run --rm -p 8080:8080 \
  -e LIBRARIAN_API_KEY=change-me \
  -e LIBRARIAN_API_IMPORT_ROOT=/data/imports \
  ghcr.io/nampara-ai/librarian:v0.1.0a40
```

## Privacy

Librarian stores data locally by default. Text is only sent to a configured model provider when a processing run requires LLM work. API keys belong in environment variables or `.env`, never in Git. CI runs secret scanning, and maintainers should run the pinned Gitleaks container command from `docs/SUPPLY_CHAIN.md` before release candidates.
Prometheus metrics are built in; OpenTelemetry request, run-stage, and queue tracing is available
with the optional `otel` extra.
