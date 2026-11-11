# Contributing

This repository holds two implementations. The branch `codex/rust-foundation` carries the Rust
terminal program under `crates/` and `tools/`. The Python/Swift application, version 1.10.3,
remains under `src/`, `apps/` and `tests/` for reference.

## Rust terminal program

The toolchain is pinned in `rust-toolchain.toml`. Run these before pushing:

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --locked -p librarian-core -p librarian-native --lib --test persistence
```

The collaboration rules, including the testing policy and the rules for measuring changes to the
PDF parser, are in `AGENTS.md`. The measurement scripts are in `tools/bench/`. Benchmark fixtures
and third-party outputs are kept outside the repository.

## Legacy Python/Swift application

The legacy application is a stable production release. Keep changes small, tested, and aligned
with the hexagonal boundaries in `docs/ARCHITECTURE.md`.

### Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,all]"
```

The full extras match CI and exercise optional extraction integrations. Install Tesseract and
Poppler separately if you want to test scanned PDFs with an unbundled backend. On macOS, the
native app also requires Xcode; see [its development guide](apps/macos/README.md#develop-for-contributors).

### Checks

Run these before opening a PR:

```bash
ruff check .
pyright
pytest
python -m build
```

### Development Rules

- Keep domain and application code independent of FastAPI, Typer, SQLite, and model SDKs.
- Put infrastructure behind ports.
- Add migrations instead of editing applied migration files.
- Do not commit private documents, transcripts, API keys, provider logs, or eval outputs containing
  sensitive text.
- Add or update eval cases when prompt behavior changes.
- Preserve prompt wording unless an eval-backed change justifies it.

### Useful Commands

```bash
librarian migrate
librarian ingest examples/benchmark_text.txt
librarian maintainer eval examples/eval_cases.json
librarian maintainer benchmark --input-path examples/benchmark_text.txt --repeats 3
docker compose up --build
```
