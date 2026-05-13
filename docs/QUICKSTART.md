# Five-Minute Quickstart

## Local CLI

From a downloaded release wheel:

```bash
python -m venv .venv
source .venv/bin/activate
pip install "nampara_librarian-0.1.0a11-py3-none-any.whl[all]"
librarian init
librarian doctor
```

From a source checkout:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,all]"
librarian init
librarian doctor
```

`librarian init` writes workspace config with an atomic same-directory replacement and rejects
symlinked config paths.

Convert and process a file or folder:

```bash
librarian import ./large.md --format md --process
librarian import ./examples --recursive --format md --process --overwrite
librarian list
librarian search "library processing"
librarian status run_... --event-limit 500 --event-offset 0
librarian queue --limit 100 --offset 0
```

Export a cleaned document:

```bash
librarian export doc_... --format md --output cleaned.md
```

## Docker

```bash
export LIBRARIAN_API_KEY=change-me
docker compose up --build
curl http://127.0.0.1:8080/health
```

In another shell:

```bash
curl http://127.0.0.1:8080/version
curl -H "x-api-key: $LIBRARIAN_API_KEY" http://127.0.0.1:8080/documents
```

## Real Provider

```bash
export LIBRARIAN_LLM_PROVIDER=openai-compatible
export LIBRARIAN_LLM_MODEL=gpt-4.1-mini
export OPENAI_API_KEY=...
librarian import ./large.md --format md --process
librarian import ./input --recursive --format md --process
```

## Large PDF Smoke Test

```bash
export LIBRARIAN_OCR_PDF_MAX_PAGES=1000
export LIBRARIAN_PDF_MAX_PAGES=1000
export LIBRARIAN_OCR_LLM_CORRECTION=never
librarian doctor --strict
librarian convert ./large.pdf --format md --output ./large.md
librarian ingest ./large.md
librarian process doc_...
librarian search "known phrase"
```
