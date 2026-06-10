# Contributing

Librarian is a stable production release. Keep changes small, tested, and aligned with the
hexagonal boundaries in `docs/ARCHITECTURE.md`.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,pdf]"
```

## Checks

Run these before opening a PR:

```bash
ruff check .
pyright
pytest
python -m build
```

## Development Rules

- Keep domain and application code independent of FastAPI, Typer, SQLite, and model SDKs.
- Put infrastructure behind ports.
- Add migrations instead of editing applied migration files.
- Do not commit private documents, transcripts, API keys, provider logs, or eval outputs containing
  sensitive text.
- Add or update eval cases when prompt behavior changes.
- Preserve prompt wording unless an eval-backed change justifies it.

## Useful Commands

```bash
librarian migrate
librarian ingest examples/benchmark_text.txt
librarian maintainer eval examples/eval_cases.json
librarian maintainer benchmark --input-path examples/benchmark_text.txt --repeats 3
docker compose up --build
```
