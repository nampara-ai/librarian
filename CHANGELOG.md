# Changelog

## 1.1.0 - 2026-06-11

Librarian 1.1.0 introduces the native macOS app: a self-contained download with the entire engine inside. Release builds bundle a relocatable Python runtime plus the Librarian wheel in `Librarian.app`, launch the backend automatically on a loopback port secured by a random per-launch API key, and store data in `~/Library/Application Support/Librarian`. The app offers drag-and-drop ingest, live per-run progress with expandable run events, cleaned-output viewing with classification, full-text search, Markdown export, and a backend readiness checklist — all over the same public HTTP API the CLI uses. DMG installers for Apple Silicon and Intel are built by the new `macapp.yml` workflow and attached to releases, with optional Developer ID signing and notarization via repository secrets, plus a download landing page under `site/`.

Engine and tooling changes:

- The Mac app's embedded backend now requires a random per-launch API key, passed through
  `LIBRARIAN_API_KEY` to the spawned process and attached by the app's API client, so other
  local processes cannot read or modify the corpus over localhost.
- Enabled ruff's blind-except rule (`BLE001`) for `src/`: every `except Exception` must either
  re-raise or carry an inline justification stating where the error is recorded. The 11
  deliberate boundary handlers are annotated; new silent swallows fail lint.
- Audited the settings surface: all 71 settings fields are read by runtime code (none dead).
  Trimmed the README Docker section to a pointer into docs/DEPLOYMENT.md; containers are an
  optional server deployment, not part of the default experience.

- Fixed `database_path` so it defaults to `librarian.sqlite` inside `data_dir`. Setting only
  `LIBRARIAN_DATA_DIR` no longer leaves the SQLite database at a working-directory-relative
  `.librarian/librarian.sqlite` while uploads follow the configured data directory. Explicit
  `LIBRARIAN_DATABASE_PATH` values are unchanged.
- Added a `workspace` conversion output mode and made it the default for `librarian import` and
  `POST /imports`: converted files now land under `<data_dir>/converted` instead of a
  `librarian-converted/` directory created next to the source documents. `convert-dir` keeps its
  explicit `subdirectory` default but also accepts `workspace`.
- Added a `python -m librarian` module entry point.
- Moved the maintainer eval, corpus-eval, benchmark, and synthetic-corpus harnesses to
  `librarian.maintainer`, which ships with source checkouts and is excluded from release wheels.
  `librarian maintainer` commands print an actionable message when the harness is absent.
- Made optional-dependency tests skip cleanly on minimal installs, and made packaging/changelog
  hygiene tests skip outside a git checkout instead of failing.
- Added `httpx2` to the dev dependencies: starlette 1.2 deprecates its httpx 1.x test-client shim
  and leaves it untyped, which broke pyright on fresh installs. The lockfile now pins
  starlette 1.2.1/fastapi 0.136.3 so local runs match fresh CI resolution.
- Enabled ruff's blind-except rule (`BLE001`) for `src/`: every `except Exception` must either
  re-raise or carry an inline justification stating where the error is recorded.
- Made the dependency-review workflow guard test version-agnostic so Dependabot action bumps do
  not break CI.
- Corrected `CONTRIBUTING.md` release status and `librarian maintainer` command examples, and
  trimmed the README Docker section to a pointer into docs/DEPLOYMENT.md.

## 1.0.0 - 2026-05-22

Librarian 1.0.0 is the stable release of the local-first document ingestion, cleaning, classification, and search engine. It ships a focused user CLI and FastAPI service for converting documents, importing corpora, running provenance-rich LLM cleaning, classifying outputs with Dewey-style labels, searching SQLite FTS indexes, and exporting cleaned content with optional transcript citation evidence. The release supports Markdown, text-like files, DOCX, PDFs, OCR images, and SRT/VTT transcript normalization, including page-aware PDF extraction with durable OCR page manifests for long-running jobs. Operational commands are grouped under `librarian admin`, while evaluation and benchmark tools are grouped under `librarian maintainer` so the production surface stays clear. The release workflow keeps secret scanning, dependency audit, SBOM generation, checksums, artifact attestations, wheel smoke installation, Docker build, and image scanning, while removing alpha-era mock evidence artifacts from published releases.
