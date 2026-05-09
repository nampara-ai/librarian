# Five-Minute Quickstart

## Local CLI

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,all]"
librarian init
```

Convert and process a folder:

```bash
librarian import ./examples --recursive --format md --process --overwrite
librarian list
librarian search "library processing"
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
librarian import ./input --recursive --format md --process
```

## Large PDF Smoke Test

```bash
export LIBRARIAN_OCR_PDF_MAX_PAGES=1000
export LIBRARIAN_PDF_MAX_PAGES=1000
export LIBRARIAN_OCR_LLM_CORRECTION=never
librarian convert ./large.pdf --format md --output ./large.md
librarian ingest ./large.md
librarian process doc_...
librarian search "known phrase"
```
