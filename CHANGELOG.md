# Changelog

## Unreleased

- Fixed `database_path` so it defaults to `librarian.sqlite` inside `data_dir`. Setting only
  `LIBRARIAN_DATA_DIR` no longer leaves the SQLite database at a working-directory-relative
  `.librarian/librarian.sqlite` while uploads follow the configured data directory. Explicit
  `LIBRARIAN_DATABASE_PATH` values are unchanged.
- Added a `workspace` conversion output mode and made it the default for `librarian import` and
  `POST /imports`: converted files now land under `<data_dir>/converted` instead of a
  `librarian-converted/` directory created next to the source documents. `convert-dir` keeps its
  explicit `subdirectory` default but also accepts `workspace`.
- Moved the maintainer eval, corpus-eval, benchmark, and synthetic-corpus harnesses to
  `librarian.maintainer`, which ships with source checkouts and is excluded from release wheels.
  `librarian maintainer` commands print an actionable message when the harness is absent.
- Made optional-dependency tests skip cleanly on minimal installs, and made packaging/changelog
  hygiene tests skip outside a git checkout instead of failing.
- Corrected `CONTRIBUTING.md` release status and `librarian maintainer` command examples.
- Added a native SwiftUI macOS companion app under `apps/macos` with drag-and-drop ingest,
  live run progress, output viewing/search/export, and a backend readiness checklist. The app
  consumes only the public HTTP API.

## 1.0.0 - 2026-05-22

Librarian 1.0.0 is the stable release of the local-first document ingestion, cleaning, classification, and search engine. It ships a focused user CLI and FastAPI service for converting documents, importing corpora, running provenance-rich LLM cleaning, classifying outputs with Dewey-style labels, searching SQLite FTS indexes, and exporting cleaned content with optional transcript citation evidence. The release supports Markdown, text-like files, DOCX, PDFs, OCR images, and SRT/VTT transcript normalization, including page-aware PDF extraction with durable OCR page manifests for long-running jobs. Operational commands are grouped under `librarian admin`, while evaluation and benchmark tools are grouped under `librarian maintainer` so the production surface stays clear. The release workflow keeps secret scanning, dependency audit, SBOM generation, checksums, artifact attestations, wheel smoke installation, Docker build, and image scanning, while removing alpha-era mock evidence artifacts from published releases.
