# Librarian

### Drop in messy documents. Get back a clean, classified, searchable library.

Librarian is a local-first **parser + copy-editor + librarian** in one. Hand it transcripts, PDFs,
DOCX, images, or scans; it extracts the text at near-commercial fidelity, **cleans it with an LLM to
Chicago-Manual style without inventing or dropping a single fact**, files every document under a
Dewey-style classification, and makes the whole collection full-text searchable. Runs as a native
**Mac app**, a scriptable **CLI**, and a **FastAPI service** — all on the same engine, all on your
own machine.

⬇️ [**Download the Mac app**](https://github.com/nampara-ai/librarian/releases/latest) ·
🚀 [Quick start](#-quick-start-60-seconds) ·
⌨️ [CLI reference](#️-cli-reference-every-command) ·
🔌 [API](#-api) ·
🏛️ [Architecture](docs/ARCHITECTURE.md)

> This is not just a PDF-to-text converter. Plenty of tools turn a PDF into a wall of text.
> Librarian's job starts *after* extraction: it copy-edits the result, gives it a clean title and a
> Dewey number, writes an 80–100 word synopsis and metadata tags, and drops it into a searchable,
> exportable library. The extractor is best-in-class; the **clean-up and organization are what make
> it Librarian**.

Version `1.7.0` is the stable production release. Everything runs locally by default — source files
and generated outputs live in a SQLite-backed workspace on your disk, and text leaves your machine
only when *you* point cleaning, classification, or OCR-correction at an external model provider.

---

## What it is

| Surface | What you get | Best for |
| --- | --- | --- |
| 🖥️ **Mac app** | Drag files into a window. Live progress, full-text search, one-click Markdown export. The entire engine, Python runtime, and OCR tools are inside the download. | Just using it. Zero terminal. |
| ⌨️ **CLI** | The whole pipeline as composable commands, every query command speaks `--json`. | Scripting, automation, bulk corpora. |
| 🔌 **API** | A local FastAPI service with the same engine behind an HTTP surface. | Wiring Librarian into other tools/agents. |

All three run the **same engine** and the **same local SQLite library**.

---

## ⬇️ Install in 60 seconds

### The Mac app (no terminal, nothing to set up)

1. Download [**Librarian-AppleSilicon.dmg**](https://github.com/nampara-ai/librarian/releases/latest/download/Librarian-AppleSilicon.dmg)
   (M-series) or [**Librarian-Intel.dmg**](https://github.com/nampara-ai/librarian/releases/latest/download/Librarian-Intel.dmg)
   (Intel). If a direct link doesn't resolve, grab the DMG from the [latest release](https://github.com/nampara-ai/librarian/releases/latest) assets.
2. Open the DMG and drag **Librarian** to **Applications**. First launch: right-click → **Open** once to clear Gatekeeper.
3. Drop files anywhere in the window.

The app bundles the high-fidelity extraction engine **and** its OCR — scanned PDFs are read fully
offline, no Homebrew, no `PATH` setup, no first-run downloads. See [apps/macos](apps/macos/README.md)
for data locations, model-provider setup, and how it's built.

### The CLI / API (Python 3.12+)

```bash
python -m venv .venv && source .venv/bin/activate
pip install "nampara-librarian[all]"      # [all] pulls every optional capability
librarian doctor                          # confirm what's available
```

> From a release wheel: `pip install "nampara_librarian-1.7.0-py3-none-any.whl[all]"` ·
> From a checkout: `pip install -e ".[dev,all]"`

---

## 🚀 Quick start (60 seconds)

```bash
librarian init                                  # create a local workspace (./.librarian)
librarian import ./my-documents --recursive --process   # convert → clean → classify everything
librarian list                                  # see what landed, with Dewey codes + titles
librarian search "canter transitions" --details # full-text search across the library
librarian show doc_1a2b3c4d                      # one document's metadata + synopsis
librarian export doc_1a2b3c4d --format md --output clean.md
```

That's the whole loop: **import → search → export.** By default everything runs with a built-in
*mock* model (no network, deterministic), so you can try the mechanics instantly. Point it at a real
model when you want real cleaning and classification:

```bash
export LIBRARIAN_LLM_PROVIDER=openai-compatible
export LIBRARIAN_LLM_MODEL=gpt-4.1-mini
export OPENAI_API_KEY=sk-...
librarian import ./input --recursive --format md --process
```

---

## 🧠 What actually happens to a document

Each file flows through five stages — and you can stop at any of them:

1. **Extract** — PDFs, DOCX, images, transcripts, and 20+ formats → clean Markdown. Tables,
   headings, lists, and figures are reconstructed; only the pages that *need* OCR get it.
2. **Clean** — an LLM copy-edits the Markdown to Chicago-Manual-of-Style prose, fixing OCR noise,
   line-break artifacts, and spacing **without summarizing, reordering, or inventing**. Source
   fidelity is validated, not assumed.
3. **Classify** — a Dewey-style code, a human title, an 80–100 word synopsis, and metadata tags.
   Recurring publications are linked into series across editions.
4. **Search** — everything is indexed for fast full-text search with facets and citation lookup.
5. **Export** — single documents as Markdown/JSON/text, or the whole library as an
   [Open Knowledge Format](docs/OKF.md) bundle for handing to another agent or knowledge tool.

---

## 📄 The extraction engine

With the `liteparse` extra (included in `[all]`), Librarian extracts PDFs and images with
[liteparse](https://github.com/run-llama/liteparse) (Apache-2.0) — reconstructed **Markdown tables,
headings, lists, and figure placeholders**, with selective OCR and its own bundled PDFium +
Tesseract (no `poppler` needed). The built-in `pdfplumber` + Tesseract path stays as a per-document
fallback.

| Capability | How to turn it on | What it does |
| --- | --- | --- |
| **Engine select** | `LIBRARIAN_PDF_ENGINE=auto\|liteparse\|legacy` | `auto` (default) uses liteparse when installed, else built-in. |
| **Offline OCR data** | `LIBRARIAN_LITEPARSE_TESSDATA_PATH=/path/to/tessdata` | Point liteparse's OCR at local language data (the Mac app does this for you). |
| **Higher-accuracy OCR** | `LIBRARIAN_LITEPARSE_OCR_SERVER_URL=...` | Offload OCR to a Surya/EasyOCR/PaddleOCR server. |
| **Figure → data (vision)** | `LIBRARIAN_FIGURE_VISION_ENABLED=true` | A vision model describes each figure and **reconstructs chart data as a Markdown table**, injected next to the figure so the numbers become searchable text. |
| **Extraction cache** | on by default | Re-ingesting unchanged files skips re-extraction (keyed by content hash + engine/OCR config). |
| **Parallel imports** | `LIBRARIAN_IMPORT_CONCURRENCY=N` (default 2) | Convert/ingest several files at once; order, resume, and per-file failure isolation preserved. |
| **Extraction timeout** | `LIBRARIAN_EXTRACTION_TIMEOUT_SECONDS=N` | Bound a single document's extraction so one pathological file can't hang a batch. |

### OCR system tools (CLI/API only — the Mac app bundles these)

The built-in OCR fallback needs two system binaries on your `PATH`:

```bash
brew install tesseract poppler                       # macOS
sudo apt-get install -y tesseract-ocr poppler-utils  # Debian/Ubuntu
```

Without them, text-layer PDFs still work; scanned pages can't be read by the fallback path. Run
`librarian doctor` to see exactly what's available.

**Rotated scans** are handled automatically: sideways or upside-down images and pages are detected
with Tesseract's orientation detection and rotated upright before OCR, so they yield real text
instead of garbage (it only rotates when detection is confident, never flipping a correct page).
On by default; set `LIBRARIAN_OCR_AUTO_ORIENT=false` to disable.

---

## ⌨️ CLI reference (every command)

All query/control commands accept `--json` for machine-readable output, so an agent can drive the
whole pipeline without scraping tables. Run any command with `--help` for its full flag set.

### Setup & health
| Command | What it does |
| --- | --- |
| `librarian init` | Create a local workspace (`.librarian/` with the SQLite database). |
| `librarian doctor [--strict] [--json]` | Report optional dependencies and OCR tools, with install hints. |
| `librarian migrate` | Apply pending database migrations. |
| `librarian version` | Print the Librarian version. |

### Convert (no database, file → file)
| Command | What it does |
| --- | --- |
| `librarian convert report.docx --format md --output out/report.md` | Convert one file to Markdown/text. |
| `librarian convert-dir ./folder --format md --output-mode subdirectory` | Convert every supported file in a folder. |
| `librarian transcript-normalize captions.srt --format md --output clean.md` | Normalize an SRT/VTT transcript to clean Markdown/text. |
| `librarian transcript-find captions.srt "a quoted phrase" --json` | Locate a quote in a transcript with timestamps. |

### Ingest, clean & classify
| Command | What it does |
| --- | --- |
| `librarian import ./folder --recursive --process` | The big one: convert → ingest → (optionally) clean+classify a whole tree. `--manifest <path> --resume` makes huge imports idempotent; `--report report.json` writes a full run report; exits non-zero if anything failed. |
| `librarian ingest transcript.txt` | Ingest a single file and persist its extracted text. |
| `librarian process doc_...` | Run cleaning + classification on an ingested document. |
| `librarian worker --once` | Drain the durable SQLite job queue (for `--process`-deferred imports). |

### Browse, search & export
| Command | What it does |
| --- | --- |
| `librarian list [--details] [--json]` | List ingested documents. |
| `librarian show doc_... [--json]` | Show a document's metadata and latest output summary. |
| `librarian search "query" [--details] [--json]` | Full-text search across the library. |
| `librarian status run_... [--json]` | Poll a processing run's status, stage, and chunk progress. |
| `librarian delete doc_... --yes` | Delete a document and its dependent local records. |
| `librarian export doc_... --format json\|txt\|md [--citation-quote "..."]` | Export one document's cleaned text + metadata. |
| `librarian export-okf ./bundle [--classification-prefix 6] [--series <key>] [--json]` | Export the whole library as an [Open Knowledge Format](docs/OKF.md) bundle. |

### Run the service
| Command | What it does |
| --- | --- |
| `librarian api` | Start the local FastAPI service (see [API](#-api)). |

### `librarian admin …` — operator & storage
| Command | What it does |
| --- | --- |
| `admin db-stats [--json]` | File size, page usage, row counts, stored-text totals (incl. the extraction cache). |
| `admin db-maintain [--vacuum]` | SQLite `optimize`, WAL checkpoint, optional `VACUUM`. |
| `admin db-check` | Verify integrity, foreign keys, and migration state. |
| `admin db-backup <dest>` / `admin db-restore <src>` | Consistent online SQLite backup / verified restore. |
| `admin workspace-backup <dest>` / `admin workspace-restore <src>` | Archive/restore data files **plus** a consistent DB snapshot. |
| `admin runs` / `admin run-cancel <id>` / `admin run-retry <id>` | List runs; cancel a queued/running run; replay a failed one. |
| `admin queue` | Inspect the durable job queue. |
| `admin api-audit [--json]` | Inspect API audit-log events. |
| `admin page-manifest <doc> [--json]` | Inspect a PDF's per-page OCR manifest (which pages were OCR'd, confidence, warnings). |

### `librarian maintainer …` — quality & release harness *(source checkouts only)*
| Command | What it does |
| --- | --- |
| `maintainer chunk <file>` | Extract + chunk a document without calling an LLM. |
| `maintainer benchmark` | Benchmark chunking and the configured cleaning provider's throughput. |
| `maintainer eval` / `maintainer corpus-eval` | Run a prompt/model evaluation suite, or evaluate over a corpus. |
| `maintainer generate-corpus` | Generate a synthetic evaluation corpus. |

---

## ⚙️ Configuration

Everything is configured by `LIBRARIAN_*` environment variables (or a `.env` file in the workspace).
The essentials:

| Variable | Default | What it controls |
| --- | --- | --- |
| `LIBRARIAN_DATA_DIR` | `.librarian` | Where the workspace + SQLite database live. |
| `LIBRARIAN_LLM_PROVIDER` | `mock` | `mock` (offline, deterministic) or `openai-compatible`. |
| `LIBRARIAN_LLM_MODEL` | `mock-cleaner` | Model name for cleaning + classification. |
| `LIBRARIAN_LLM_BASE_URL` | – | Base URL for an OpenAI-compatible endpoint. |
| `OPENAI_API_KEY` | – | API key (env-var name configurable via `LIBRARIAN_LLM_API_KEY_ENV`). |
| `LIBRARIAN_LLM_MAX_CONCURRENCY` | `8` | Parallel chunk-cleaning requests. |
| `LIBRARIAN_PDF_ENGINE` | `auto` | Extraction engine (see [the engine](#-the-extraction-engine)). |
| `LIBRARIAN_FIGURE_VISION_ENABLED` | `false` | Vision pass that turns charts into data tables. |
| `LIBRARIAN_IMPORT_CONCURRENCY` | `2` | Files converted/ingested in parallel. |
| `LIBRARIAN_API_KEY` / `LIBRARIAN_API_KEYS` | – | Require an API key for protected endpoints. |

### Optional dependency extras

The base install is lean; opt into capabilities (or take `[all]`):

| Extra | Enables |
| --- | --- |
| `pdf` | Built-in PDF text extraction (`pdfplumber`). |
| `ocr` | Scanned/image-PDF OCR for the built-in engine (`pdf2image`, `pillow`, `pytesseract`). |
| `liteparse` | High-fidelity engine — tables, headings, figures, selective OCR (bundles PDFium + Tesseract). |
| `universal` | Broad conversion via `markitdown` (PPTX, XLSX, Outlook, …). |
| `otel` | OpenTelemetry tracing/metrics export. |
| `all` | Everything above. |

---

## 🔌 API

```bash
uvicorn librarian.api.app:create_app --factory --host 127.0.0.1 --port 8080
# or simply: librarian api
```

Primary endpoints:

- `GET /health`, `GET /ready`, `GET /version`
- `POST /documents`, `GET /documents`, `GET /documents/{id}`, `DELETE /documents/{id}`
- `POST /imports`, `GET /imports/status`, `GET /imports/page-manifest`
- `POST /runs`, `GET /runs`, `GET /runs/{id}`, `POST /runs/{id}/cancel`, `POST /runs/{id}/retry`
- `GET /runs/{id}/events`, `GET /runs/{id}/events/stream`
- `GET /documents/{id}/content`, `GET /documents/{id}/export?format=json|txt|md`
- `GET /export/okf`, `GET /documents/{id}/okf`
- `POST /search`, `POST /search/results`, `POST /search/facets`
- `GET /metrics`, `GET /metrics/prometheus`

Set `LIBRARIAN_API_KEY` (or `LIBRARIAN_API_KEYS`) to require a key via `x-api-key` or
`Authorization: Bearer …`. Read-scoped keys reach document/search endpoints; operational endpoints
need write scope. Full details in [docs/API.md](docs/API.md).

---

## 📂 Where your writing lives

A workspace is just a folder (`.librarian/` by default):

```
.librarian/librarian.sqlite   ← the library: documents, cleaned text, classifications, search index
.librarian/converted/         ← Markdown/text produced by `import` (originals are never touched)
```

`librarian import` converts sources into the workspace by default; use `--output-mode` to place
converted files `new-directory`, `original`, or `subdirectory` instead. Back the whole thing up with
`librarian admin workspace-backup`.

---

## 🔒 Private by default

Librarian stores everything locally. Text is sent to a model provider **only** when cleaning,
classification, or OCR-correction actually needs LLM work — and only to the provider you configure.
With the default `mock` provider, nothing leaves your machine. Keep API keys in environment variables
or `.env`, never in Git. CI runs secret scanning, dependency audit, type checking, the full test
suite, a wheel smoke-install, and Docker build checks on every change.

---

## 🐳 Docker

Containerized deployment is optional and aimed at server installs — the Mac app and CLI need none of
it. Images publish to `ghcr.io/nampara-ai/librarian`; compose/run examples live in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

---

## 🗂️ What's in here

```
src/librarian/            The engine: ingest, pipeline, application, storage, api, cli, taxonomy
src/librarian/ingest/     Extraction adapters (liteparse, pdfplumber/Tesseract, DOCX, markitdown, …)
src/librarian/pipeline/   Chunking, cleaning, validation
src/librarian/taxonomy/   Dewey classification
apps/macos/               The native Mac app (Swift) + its build/bundle/sign scripts
docs/                     ARCHITECTURE · API · DEPLOYMENT · OPERATIONS · OKF
tests/                    The full test suite (unit + integration)
examples/corpus/          Sample documents to try the pipeline on
```

---

## 📚 Documentation

Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Then: [API](docs/API.md) ·
[deployment](docs/DEPLOYMENT.md) · [operations runbooks](docs/OPERATIONS.md) ·
[Open Knowledge Format](docs/OKF.md) · [Mac app](apps/macos/README.md). Release history is in
[CHANGELOG.md](CHANGELOG.md).

---

## 🤝 Contributing & 📜 License

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for setup and the quality gate, and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community expectations. To report a vulnerability, follow
[SECURITY.md](SECURITY.md).

Licensed under the **MIT License** — see [LICENSE](LICENSE). Librarian bundles or builds on
third-party components under their own licenses (notably the Apache-2.0 `liteparse` engine); see
[NOTICE](NOTICE).
