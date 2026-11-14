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

The Python scripts under `tools/` package and measure the program. Lint them with
`ruff check tools`. Their lint settings are in `tools/ruff.toml`.

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

## Pull request checks

The workflows `Librarian terminal builds` and `CI` start on every pull request. Each compares
the pull request with its merge base and runs only the jobs for the files that changed. A job
that does not apply is reported as skipped.

| Workflow | File | Runs its jobs when these change | Final check |
| --- | --- | --- | --- |
| Librarian terminal builds | `.github/workflows/rust-foundation.yml` | `crates/`, the Cargo files, the packaging tools, the packaged documents. The lint job runs for any file under `tools/` and for `pyproject.toml` | `Rust required` |
| CI | `.github/workflows/ci.yml` | `src/`, `tests/`, `apps/`, `examples/`, the Python packaging and container files, the workflows, and the documents the tests read | `Legacy required` |

`Rust required` and `Legacy required` always run. Each fails if a job that had to run did
not succeed. The exact path lists are in the job `changes` of each workflow.
