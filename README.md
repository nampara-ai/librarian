# Librarian

Librarian is a local-first document ingestion, cleaning, classification, and search system. It converts transcripts, Markdown, text files, DOCX, PDFs, and OCR images into clean Markdown or plain text; processes them with an OpenAI-compatible model while preserving source fidelity; classifies the result with Dewey-style labels; and exposes the same engine through a Mac app, a CLI, and a FastAPI service.

Version `1.1.7` is the stable production release. The default deployment is local or single-node: source documents and generated outputs stay in SQLite-backed local storage unless you configure an external model provider for cleaning, classification, or OCR correction.

## Mac App

The easiest way to use Librarian is the native Mac app — a self-contained download with the entire engine inside:

1. Download [Librarian-AppleSilicon.dmg](https://github.com/nampara-ai/librarian/releases/latest/download/Librarian-AppleSilicon.dmg) (M-series Macs) or [Librarian-Intel.dmg](https://github.com/nampara-ai/librarian/releases/latest/download/Librarian-Intel.dmg) (Intel Macs). If a direct link does not resolve, download the DMG from the assets of the [latest release](https://github.com/nampara-ai/librarian/releases/latest).
2. Open the DMG and drag **Librarian** to **Applications**.
3. Launch it and drop files anywhere in the window.

Drag-and-drop ingest, live processing progress, full-text search, and Markdown export — no terminal required. See [apps/macos](apps/macos/README.md) for first-launch notes, data locations, LLM provider configuration, and how the app is built and released.

Everything below covers the engine itself — the CLI and API the app is built on.

## Install

From PyPI:

```bash
python -m venv .venv
source .venv/bin/activate
pip install "nampara-librarian[all]"
```

From a downloaded release wheel:

```bash
python -m venv .venv
source .venv/bin/activate
pip install "nampara_librarian-1.1.7-py3-none-any.whl[all]"
```

From a source checkout:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,all]"
```

## Quick Start

```bash
librarian init
librarian doctor --strict
librarian import examples/corpus/markdown-transcript.md --format md --process
librarian list
librarian search "library processing" --details
librarian export doc_... --format md --output cleaned.md
```

For a real model provider:

```bash
export LIBRARIAN_LLM_PROVIDER=openai-compatible
export LIBRARIAN_LLM_MODEL=gpt-4.1-mini
export OPENAI_API_KEY=...
librarian import ./input --recursive --format md --process
```

## CLI

User-facing commands:

```bash
librarian version
librarian doctor
librarian init
librarian migrate
librarian convert path/to/report.docx --format md --output converted/report.md
librarian convert-dir path/to/folder --format md --output-mode subdirectory
librarian transcript-normalize path/to/captions.srt --format md --output normalized.md
librarian transcript-find path/to/captions.srt "quoted source phrase" --json
librarian import path/to/folder --recursive --format md --process
librarian ingest path/to/transcript.txt
librarian process doc_...
librarian worker --once
librarian list
librarian show doc_...
librarian delete doc_... --yes
librarian status run_... --event-limit 500 --event-offset 0
librarian search "horse training" --details
librarian export doc_... --format json --citation-quote "quoted source phrase"
librarian api
```

`librarian import` converts sources into the workspace by default: converted Markdown/text lands under `<data_dir>/converted` instead of next to your original files. Use `--output-mode` to opt into `new-directory`, `original`, or `subdirectory` placement.

Operator commands live under `librarian admin`, including database maintenance, backups, run controls, queue inspection, API audit logs, and PDF page-manifest inspection. Release and quality harnesses live under `librarian maintainer`; they ship with source checkouts only and are excluded from release wheels.

## API

```bash
uvicorn librarian.api.app:create_app --factory --host 127.0.0.1 --port 8080
```

Primary endpoints:

- `GET /health`, `GET /ready`, `GET /version`
- `POST /documents`, `GET /documents`, `GET /documents/{id}`, `DELETE /documents/{id}`
- `POST /imports`, `GET /imports/status`, `GET /imports/page-manifest`
- `POST /runs`, `GET /runs`, `GET /runs/{id}`, `POST /runs/{id}/cancel`, `POST /runs/{id}/retry`
- `GET /runs/{id}/events`, `GET /runs/{id}/events/stream`
- `GET /documents/{id}/content`, `GET /documents/{id}/export?format=json|txt|md`
- `POST /search`, `POST /search/results`, `POST /search/facets`
- `GET /metrics`, `GET /metrics/prometheus`

If `LIBRARIAN_API_KEY` or `LIBRARIAN_API_KEYS` is set, protected requests must include one configured value as `x-api-key` or `Authorization: Bearer ...`. Read-scoped keys can use document and search endpoints, while operational endpoints such as config, metrics, and page-manifest inspection require write scope.

## Docker

Containerized deployment is optional and aimed at server installs; the Mac app and CLI need none of it. Images are published as `ghcr.io/nampara-ai/librarian`, and compose/run examples live in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Architecture And Operations

Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). API details are in [docs/API.md](docs/API.md), deployment guidance is in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md), and production runbooks are in [docs/OPERATIONS.md](docs/OPERATIONS.md).

## Privacy

Librarian stores data locally by default. Text is sent to a configured model provider only when processing or OCR correction requires LLM work. API keys belong in environment variables or `.env`, never in Git. CI runs secret scanning, dependency audit, type checking, tests, package build, wheel smoke install, and Docker build checks for every pull request.
