# Librarian

Librarian is a local-first document ingestion, cleaning, classification, and search system. It converts transcripts, Markdown, text files, DOCX, PDFs, and OCR images into clean Markdown or plain text; processes them with an OpenAI-compatible model while preserving source fidelity; classifies the result with Dewey-style labels; and exposes the same engine through a Mac app, a CLI, and a FastAPI service.

Version `1.6.1` is the stable production release. The default deployment is local or single-node: source documents and generated outputs stay in SQLite-backed local storage unless you configure an external model provider for cleaning, classification, or OCR correction.

## Mac App

The easiest way to use Librarian is the native Mac app — a self-contained download with the entire engine inside:

1. Download [Librarian-AppleSilicon.dmg](https://github.com/nampara-ai/librarian/releases/latest/download/Librarian-AppleSilicon.dmg) (M-series Macs) or [Librarian-Intel.dmg](https://github.com/nampara-ai/librarian/releases/latest/download/Librarian-Intel.dmg) (Intel Macs). If a direct link does not resolve, download the DMG from the assets of the [latest release](https://github.com/nampara-ai/librarian/releases/latest).
2. Open the DMG and drag **Librarian** to **Applications**.
3. Launch it and drop files anywhere in the window.

Drag-and-drop ingest, live processing progress, full-text search, and Markdown export — no terminal required. See [apps/macos](apps/macos/README.md) for first-launch notes, data locations, LLM provider configuration, and how the app is built and released.

Everything below covers the engine itself — the CLI and API the app is built on.

## Install

Requires **Python 3.12+**. (The Mac app needs none of this — it bundles the engine, the Python
runtime, and the OCR tools. This section is for the CLI and API.)

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
pip install "nampara_librarian-1.6.1-py3-none-any.whl[all]"
```

From a source checkout:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,all]"
```

### Optional dependency extras

The base install is intentionally lean; opt into capabilities with extras (or `[all]` for
everything):

| Extra | Enables |
| --- | --- |
| `pdf` | Built-in PDF text extraction (`pdfplumber`) |
| `ocr` | Scanned/image-PDF OCR for the built-in engine (`pdf2image`, `pillow`, `pytesseract`) |
| `liteparse` | High-fidelity PDF/image extraction — tables, headings, figures, selective OCR — via the `liteparse` engine (bundles PDFium + Tesseract; see below) |
| `universal` | Broad document conversion via `markitdown` (PPTX, XLSX, Outlook, …) |
| `otel` | OpenTelemetry tracing/metrics export |
| `all` | All of the above |

### PDF/image extraction engine

With the `liteparse` extra installed (included in `[all]`), Librarian extracts PDFs and images
with [liteparse](https://github.com/run-llama/liteparse) (Apache-2.0) instead of the built-in
pdfplumber + Tesseract path. It reconstructs **Markdown tables, headings, lists, and figure
placeholders** and OCRs only the pages that need it, and it bundles its own PDFium + Tesseract
(no `poppler` system binary required for PDFs). The richer Markdown feeds the same cleaning,
classification, and OKF pipeline. Set `LIBRARIAN_PDF_ENGINE` to `auto` (default — use liteparse
when installed, else built-in), `liteparse` (force), or `legacy` (always built-in). Point
`LIBRARIAN_LITEPARSE_OCR_SERVER_URL` at a Surya/EasyOCR/PaddleOCR server for higher-accuracy OCR.
liteparse's bundled Tesseract fetches its language data on first use; to run fully offline, set
`LIBRARIAN_LITEPARSE_TESSDATA_PATH` to a directory of Tesseract `*.traineddata` (the Mac app does
this automatically, pointing it at its bundled `eng`/`osd` data). The Mac app ships the `[all]`
extras, so the liteparse engine is the default there with no setup.

### Figure & chart enrichment (vision)

Charts and figures carry data that plain extraction loses — liteparse leaves them as image
placeholders. With a vision-capable model you can recover that data as text. Set
`LIBRARIAN_FIGURE_VISION_ENABLED=true` (and `LIBRARIAN_FIGURE_VISION_MODEL` if your cleaning model
isn't vision-capable): when the liteparse engine is active, each embedded figure image is sent to the
model, which returns a description and — for charts — a reconstructed Markdown data table. The result
is injected next to the figure's placeholder, so the chart's numbers flow into the same cleaning,
classification, search, and OKF pipeline as everything else.

It's off by default because it needs a vision model and adds per-figure cost/latency. Tunables:
`LIBRARIAN_FIGURE_VISION_MAX_FIGURES` (cap per document, default 20),
`LIBRARIAN_FIGURE_VISION_MIN_BYTES` / `_MAX_BYTES` (skip icons / oversized images), and
`LIBRARIAN_FIGURE_VISION_MAX_CONCURRENCY`. A figure the model can't read is left as its plain
placeholder rather than failing the document. (Vector-drawn charts that liteparse doesn't emit as
embedded raster images aren't covered yet.)

### Extraction throughput

Two controls keep bulk ingestion fast and bounded:

- **Content-hash extraction cache** (on by default): extracted Markdown is cached by the source
  file's SHA-256 plus a signature of the extraction configuration, so re-ingesting unchanged files —
  or the same bytes across documents — skips the parser/OCR work. The cache is config-aware (changing
  `LIBRARIAN_PDF_ENGINE` or OCR settings re-extracts instead of serving stale text) and never caches
  failures, so a transient error (e.g. an unreachable OCR server) is retried on the next run. Disable
  with `LIBRARIAN_EXTRACTION_CACHE_ENABLED=false`. `librarian admin db-stats` reports the
  `extraction_cache` row count.
- **Extraction timeout** (off by default): set `LIBRARIAN_EXTRACTION_TIMEOUT_SECONDS` to a positive
  number to cap how long a single document may spend in extraction, so one pathological file cannot
  hang a batch.
- **Parallel directory import** (`LIBRARIAN_IMPORT_CONCURRENCY`, default `2`): a directory import
  converts and ingests several files at once, overlapping their extraction/OCR work, while keeping
  result order, manifest resume, and per-file failure isolation intact. Set it to `1` for sequential
  imports; higher values speed bulk imports but, for `--process`/`--queue` runs, multiply with
  `LIBRARIAN_LLM_MAX_CONCURRENCY`, so keep it modest on rate-limited model providers.

### OCR system dependencies (CLI/API only)

OCR for scanned or image-based PDFs needs two **system** binaries that cannot be installed via
pip — they must be on your `PATH`:

```bash
# macOS
brew install tesseract poppler

# Debian/Ubuntu
sudo apt-get install -y tesseract-ocr poppler-utils
```

Without them, text-layer PDFs still work, but scanned pages cannot be read. (The Mac app bundles
both, so app users never need this.) Run `librarian doctor` to verify what's available.

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
librarian export-okf ./bundle --classification-prefix 6 --json
librarian api
```

`librarian import` converts sources into the workspace by default: converted Markdown/text lands under `<data_dir>/converted` instead of next to your original files. Use `--output-mode` to opt into `new-directory`, `original`, or `subdirectory` placement.

### Automation and scripting

The query and control commands emit machine-readable JSON with `--json`, so an agent can drive the whole pipeline without scraping tables: `ingest --json` and `process --json` return the new `document_id`/`run_id`; `status --json` reports `status`, `stage`, `total_chunks`, and `completed_chunks` for polling; and `list`, `show`, and `search [--details] --json` return structured records. For bulk runs, `librarian import --recursive --process --report report.json` writes a full JSON report, `--manifest <path> --resume` makes large imports idempotent across restarts, and the command exits non-zero if any item failed. `doctor --json`, `admin db-stats --json`, `admin api-audit --json`, and `admin page-manifest --json` round out the machine-readable surface.

To hand a processed corpus to another agent or knowledge tool, `librarian export-okf ./bundle` renders all processed documents as an [Open Knowledge Format](docs/OKF.md) v0.1 bundle — a directory of markdown concept files with YAML frontmatter, organized by Dewey classification, cross-linked, and indexed. See [docs/OKF.md](docs/OKF.md) for the field mapping and layout.

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
- `GET /export/okf`, `GET /documents/{id}/okf` (Open Knowledge Format bundle / single concept)
- `POST /search`, `POST /search/results`, `POST /search/facets`
- `GET /metrics`, `GET /metrics/prometheus`

If `LIBRARIAN_API_KEY` or `LIBRARIAN_API_KEYS` is set, protected requests must include one configured value as `x-api-key` or `Authorization: Bearer ...`. Read-scoped keys can use document and search endpoints, while operational endpoints such as config, metrics, and page-manifest inspection require write scope.

## Docker

Containerized deployment is optional and aimed at server installs; the Mac app and CLI need none of it. Images are published as `ghcr.io/nampara-ai/librarian`, and compose/run examples live in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Architecture And Operations

Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). API details are in [docs/API.md](docs/API.md), deployment guidance is in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md), production runbooks are in [docs/OPERATIONS.md](docs/OPERATIONS.md), and the Open Knowledge Format export is documented in [docs/OKF.md](docs/OKF.md).

## Privacy

Librarian stores data locally by default. Text is sent to a configured model provider only when processing or OCR correction requires LLM work. API keys belong in environment variables or `.env`, never in Git. CI runs secret scanning, dependency audit, type checking, tests, package build, wheel smoke install, and Docker build checks for every pull request.

## Contributing And Security

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and the
quality gate, and [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations. To report a
vulnerability, follow [SECURITY.md](SECURITY.md). Release history is in [CHANGELOG.md](CHANGELOG.md).

## License

Licensed under the MIT License — see [LICENSE](LICENSE).
