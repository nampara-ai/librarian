# Changelog

## Unreleased

- Vision-LLM figure/chart enrichment (opt-in, `LIBRARIAN_FIGURE_VISION_ENABLED`). With the liteparse
  engine active, each embedded figure image is sent to a vision-capable model that returns a
  description and, for charts, a reconstructed Markdown data table; the result is injected next to the
  figure's placeholder so otherwise-lost chart data becomes searchable, classifiable text. Bounded by
  `LIBRARIAN_FIGURE_VISION_MAX_FIGURES`/`_MIN_BYTES`/`_MAX_BYTES`/`_MAX_CONCURRENCY`; uses
  `LIBRARIAN_FIGURE_VISION_MODEL` (defaults to the cleaning model). Per-figure failures are swallowed
  so one bad image never fails the document, and the output-affecting vision settings (model, figure
  cap, and the size/length gates) fold into the extraction-cache signature so toggling them
  re-extracts instead of serving stale text. Providers gained a `describe_image` capability (OpenAI-compatible vision content parts;
  deterministic mock for tests/dry runs).
- High-fidelity PDF/image extraction via the optional `liteparse` engine
  ([liteparse](https://github.com/run-llama/liteparse), Apache-2.0). When the `liteparse` extra is
  installed (included in `[all]`), PDFs and images are extracted to Markdown with reconstructed
  **tables, headings, lists, and figure placeholders**, OCR-ing only the pages that need it, and
  bundling its own PDFium + Tesseract (no `poppler` system binary needed for PDFs). The richer
  Markdown feeds the existing cleaning/classification/OKF pipeline unchanged. `LIBRARIAN_PDF_ENGINE`
  selects `auto` (default; liteparse when installed, otherwise the built-in pdfplumber + Tesseract
  path), `liteparse`, or `legacy`; the built-in path remains a per-document fallback. See `NOTICE`
  for attribution.
- Content-hash extraction cache: extracted Markdown is cached by source SHA-256 plus a signature of
  the extraction configuration (engine + OCR options), so re-ingesting unchanged files — or the same
  bytes across documents — skips the parser/OCR work. The cache is config-aware (changing the engine
  or OCR settings re-extracts rather than serving stale text) and never caches failures (transient
  errors retry). New migration `0009_extraction_cache.sql`; toggle with
  `LIBRARIAN_EXTRACTION_CACHE_ENABLED` (default on). `admin db-stats` reports the `extraction_cache`
  row count.
- Extraction timeout ceiling: `LIBRARIAN_EXTRACTION_TIMEOUT_SECONDS` (default `0`, disabled) bounds a
  single document's extraction so one pathological file cannot hang a batch, raising
  `ExtractionTimeoutError` when exceeded.
- Directory imports now convert/ingest files with bounded concurrency
  (`LIBRARIAN_IMPORT_CONCURRENCY`, default `2`), so the per-file extraction work overlaps instead of
  running strictly one at a time. Output paths are reserved up front to stay collision-free, and
  result order, manifest resume, and per-file failure isolation are preserved. Set it to `1` for
  fully sequential imports; raising it speeds bulk imports but, for `--process`/`--queue` runs, also
  multiplies with `llm_max_concurrency`, so keep it modest on rate-limited providers.

- Classification now captures a recurring-publication identity so editions of the same report are
  connected over time: `issuer`, `series_title`, a normalized `series_key`, and an orderable
  `period`. The new `dewey_v5` prompt extracts issuer/series/period; the `series_key` is derived
  deterministically by stripping date/period tokens so monthly editions converge, and falls back to
  a distinctive source filename when the model gives no series (generic names like `report.pdf` are
  ignored). Documents classified before v5 keep parsing with these fields unset.
- OKF export surfaces the series: each concept lists its other editions under a `## Series Editions`
  heading ordered by reporting period, and carries `issuer` / `series` / `series_key` / `period` as
  frontmatter extension fields. `librarian export-okf --series <key-or-name-fragment>` (and the
  `series` query parameter on `GET /export/okf`) filters a bundle to one series.
- New migration `0008_classification_series.sql` adds the four nullable columns and an index on
  `series_key`.

## 1.6.1 - 2026-06-14

Relicensed to MIT, plus an OKF output mode in the Mac app and a small PDF cleanup. (The v1.6.0
tag was accidentally created on the v1.5.0 commit before this work merged; protected tags cannot
be moved, so it is retained as inert history and superseded by 1.6.1.)

- **License changed from Apache-2.0 to the MIT License.** Librarian is now fully permissively
  open source; see [LICENSE](LICENSE).
- The Mac app gains a **"Markdown (OKF bundle)"** output format. Selecting it makes the
  destination folder an Open Knowledge Format bundle: each finished document is folded into a
  Dewey-organized tree of concept files (with a debounced whole-bundle rebuild so indexes and
  cross-links stay consistent), instead of writing one file per document. The other formats
  (Markdown / Plain Text / JSON) are unchanged. Built on the OKF producer shipped in 1.5.0.
- PDF page section markers are now level-four headings (`#### Page N`) instead of `## Page N`, so
  page boundaries no longer dominate a document's heading outline.

## 1.5.0 - 2026-06-14

Librarian can now emit a processed corpus as an Open Knowledge Format (OKF) v0.1 bundle — a
vendor-neutral, agent- and human-readable knowledge format
([spec](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf)). Turn a pile of
scanned PDFs, transcripts, and documents into a portable knowledge wiki an agent can reason over.

- New `librarian export-okf ./bundle` (and `GET /export/okf`, `GET /documents/{id}/okf`) render
  processed documents as conformant OKF concept files: markdown with YAML frontmatter, organized
  into a Dewey-derived directory hierarchy, cross-linked to same-classification siblings, and
  accompanied by generated `index.md` files for progressive disclosure. The bundle root declares
  `okf_version: "0.1"`. Filters: `--classification-prefix`, `--tag`, `--limit`; `--json` summary;
  non-zero exit when nothing matches.
- The classification stage now also produces a one-sentence `description` (the new `dewey_v4`
  prompt), used as the OKF concept abstract; documents classified before v4 fall back to the first
  sentence of their synopsis. Frontmatter maps title → `title`, the one-line abstract →
  `description`, tags → `tags`, the document kind → `type`, with the Dewey code/label and
  confidence as extension fields. There is no runtime OKF dependency — Librarian emits the format
  directly. See [docs/OKF.md](docs/OKF.md).
- A Mac-app "Markdown (OKF bundle)" output format is a planned fast follow.

## 1.4.0 - 2026-06-13

Makes the CLI fully scriptable, so an agent can drive bulk document processing end to end
without scraping human-readable tables.

- `--json` now covers the core query and control commands: `ingest` and `process` return the
  new `document_id`/`run_id` (plus run status and chunk counts), `status` returns
  `status`/`stage`/`total_chunks`/`completed_chunks`/`failed_chunks` and the event list for
  polling, and `list`, `show`, and `search` (with or without `--details`) return structured
  records — `show` and detailed `search` include the Dewey code, title, tags, and summary.
  Output is clean, unstyled JSON suitable for piping into `jq` or a parser.
- Combined with the existing `import --recursive --process --report report.json` (full JSON
  report), `import --manifest <path> --resume` (idempotent bulk imports across restarts),
  non-zero exit on any failed item, and SHA-based ingest de-duplication, the CLI is now a
  complete machine-driveable surface. The README documents the automation flow.

## 1.3.0 - 2026-06-13

Scanned and image-based PDFs now work in the Mac app, out of the box.

- The Mac app bundles the OCR toolchain (Tesseract and Poppler, with English
  language data) inside `Librarian.app`, fully self-contained and relocated so it
  depends on nothing outside the bundle. Previously a PDF with image pages failed
  at upload: macOS GUI apps inherit only a bare system `PATH`, so the engine could
  not find an OCR binary even when one was installed via Homebrew. The app now puts
  its bundled OCR tools (then common Homebrew locations) on the engine's `PATH` and
  sets `TESSDATA_PREFIX`.
- A single unreadable page no longer sinks a large mixed PDF: failed pages are
  skipped and recorded in the page manifest, and ingest fails only when no text can
  be extracted at all. Genuinely unreadable PDFs now get a clear "this may be a scan
  with no readable text" message instead of a generic failure.
- Fixed the misleading "The engine was restarting — press Retry" error on large
  PDFs. OCR ingest can take minutes, but the app's network client gave up after 30
  seconds and mislabeled the timeout as an engine restart. The upload/ingest timeout
  is now generous enough to let OCR finish, so the real result (success or a specific
  error) reaches you.
- The macOS build now compiles each DMG on a native-architecture runner (Apple
  Silicon and Intel) and verifies, on every pull request, that the bundled OCR stack
  reads a generated test PDF end to end — so an OCR packaging mistake fails the build,
  never an immutable release tag.

## 1.2.0 - 2026-06-12

Cleaned documents now come out of the pipeline shelf-ready: named, summarized, and tagged.

- Output files get a clean library filename: the Dewey code followed by an AI-generated
  document title, e.g. `636.1 Saddle Fit and Groundwork Notes.md`. The engine suggests the
  name on every export (sanitized for the filesystem and HTTP headers), the Mac app uses it
  when auto-saving to the destination folder, and identical or near-identical documents that
  produce the same name fall into the existing " (2)", " (3)" collision numbering — never
  overwritten. When classification produced no title, the original filename is used as before.
- Markdown exports open with an 80-100 word synopsis of the document, followed by the
  classification and topical tags, then a separator and the untouched cleaned text. JSON
  exports carry the new `title`, `summary`, `tags`, and `suggested_stem` fields; plain-text
  exports remain the cleaned text verbatim.
- All of this comes from the classification stage, not the cleaning stage: the new dewey_v3
  classification prompt asks for a title and 3-7 tags alongside the existing summary, code,
  and category. The cleaning prompts are untouched, the classifier's response stays
  schema-validated with the existing heuristic fallback (worst case: original filename, no
  synopsis — never a stalled run or altered content), and dewey_v1/v2 remain selectable via
  `LIBRARIAN_CLASSIFICATION_PROMPT_VERSION`.

## 1.1.8 - 2026-06-11

Closes the last silent black hole. A stale preference combination — built-in engine disabled
plus an empty external server address — could survive reinstalls indefinitely; the app started
no engine, sent every file to an empty address, and showed nothing wrong.

- Self-healing: external mode with no usable server address is unreachable by construction, so
  the app now falls back to the built-in engine automatically when that state is detected.
- The Settings toggle can no longer create the state: turning the built-in engine off with an
  empty server address pre-fills the default address.
- No more silence: whenever the engine target is unreachable for any reason, the footer shows a
  red "Engine not connected" pill with a direct Settings link. Healthy stays silent; dead never
  is again.
- The cleaning progress bar now tracks real progress. The engine previously recorded chunk
  completion only after the entire cleaning stage finished, so the bar sat near zero for the
  whole run; it now persists progress as each chunk is cleaned (cache hits credited
  immediately), and the bar fills smoothly across all coherence modes.

## 1.1.7 - 2026-06-11

Fixes "Couldn't reach the AI provider" failures on Macs where the app could connect but the
embedded engine could not. The app's own networking follows macOS system settings; the bundled
Python runtime does not. The app now bridges both into the engine's environment at launch:

- macOS system proxy settings (from `scutil --proxy`) are passed as proxy environment
  variables, so VPN and proxy setups work for cleaning calls, with loopback excluded.
- The macOS system trust store (system roots plus admin-added certificates) is exported to a
  PEM bundle and passed via `SSL_CERT_FILE`, so corporate or security-tool TLS interception
  roots that the Mac trusts are also trusted by the engine.
- TLS-specific failures now say so ("Secure connection to the AI provider failed — a VPN,
  proxy, or security tool may be interfering") instead of the generic connection message.
- The app build workflow now verifies, on every pull request, that the bundled runtime can
  complete a TLS connection to a provider endpoint from the packaged app.
- Files dropped (or retried) while the engine restarts after a settings change no longer fail:
  the app now waits up to 15 seconds for the engine to come back before sending, and
  engine-restart hiccups are labeled "The engine was restarting — press Retry" instead of being
  misattributed to the AI provider.
- Fixed the routing bug behind those failures: in embedded mode, the app could silently fall
  back to the external server address while the engine was starting or restarting, pointing
  uploads at a dead port where retries could never succeed. Embedded mode now always targets
  the embedded engine.

## 1.1.6 - 2026-06-11

Settings becomes connect-first and idiot-proof:

- Pick a provider (Anthropic, OpenAI, DeepSeek, Ollama, LM Studio, or Custom), paste an API
  key — or a server address for local models — and press Connect. The app calls the provider's
  live model listing and answers within seconds: a green Connected line plus a model dropdown
  populated with that provider's actual models (an Anthropic key lists Haiku/Sonnet/Opus;
  Ollama lists the models you have pulled). Picking a model applies immediately and pins that
  model to every cleaning call: "Cleaning with {model}". A bad key gets a clear red failure.
- Demo mode is no longer a choice. Until a provider is connected, Settings shows "Add an API
  key or a local model to start AI cleaning," and the main window's empty state offers a
  "Set up AI cleaning…" link that opens Settings directly.
- Fixed the broken scrolling in Settings: the pane is now a flat fixed-width layout with a
  single non-nested Advanced section, so there is no internal scroll region to glitch.
- Keys stay in the macOS Keychain; DeepSeek keys are now bridged to the engine alongside
  Anthropic and OpenAI ones.

## 1.1.5 - 2026-06-11

The Mac app is redesigned around its real job — a pipeline, not a database browser. One
window, one column, one verb: drop files, pick a destination, let it cook.

- The main window is a queue. Each dropped file moves through Waiting → Sending → Converting
  → Cleaning → Classifying → Saved, and the cleaned file is exported automatically to the
  destination folder the moment processing finishes — zero clicks between drop and
  files-in-folder. Done rows get Show in Finder; failures get a plain-words reason and Retry.
- A destination strip (Save to + Format) sits above the queue, so "where will these land" is
  answered before the first drop. Name collisions append " (2)" — never overwrite, never ask.
- The settings drawer is one pane: provider (Anthropic / OpenAI / OpenAI-compatible / Ollama /
  None), model, and API key, with inline key validation against the provider. API keys now
  live in the macOS Keychain (legacy keys in `.env` are migrated automatically) and are handed
  to the engine through its environment, never written to disk.
- File and transcript tools moved to a menu-bar Tools menu; Diagnostics moved to the Help
  menu (backed by the new `librarian doctor --json`). Engine health appears in the footer
  only when something is wrong.
- Items that stop making progress fail with "Took too long" instead of spinning forever; the
  queue is session-scoped, so relaunches start clean while the engine's own storage persists.
- Lifecycle hardening: backend log handle closed on stop/restart, crashed engines detected
  via terminationHandler, view layer reduced by ~40%.
- Fixed Swift actor-isolation errors that broke the first compile of the app's CLI-tool and
  configuration helpers (`BackendController`'s static path helpers are now `nonisolated`). The
  Mac App workflow now builds the app on every pull request that touches `apps/macos`, so a
  compile failure can never first surface on an immutable release tag again. The v1.1.4 tag was
  burned by exactly that failure and joins v1.1.0–v1.1.2 as inert history.

## 1.1.3 - 2026-06-11

First fully published release of the 1.1 line, containing all 1.1.0 and 1.1.1 changes below.
This repository publishes immutable releases, so assets cannot be attached after publication;
the release workflow now waits for the Mac app DMG builds, collects them as workflow artifacts,
and includes them — checksummed alongside the engine artifacts — in a single atomic release
creation. The v1.1.1 release published with engine artifacts only (its DMG attach step was
rejected by release immutability) and is superseded by this version. The v1.1.0–v1.1.2 tags
remain as inert history: v1.1.2 was accidentally created on the 1.1.1 commit before this
release's pipeline fix merged, and protected tags cannot be moved.

## 1.1.1 - 2026-06-11

Patch release on top of the unpublished 1.1.0. The Docker image build now upgrades base-layer
packages, picking up Debian's fix for CVE-2026-45447 (OpenSSL), which was published mid-release
and blocked the image scan gate. The v1.1.0 GitHub release was never published: its tag hit a
release-assembly race (fixed in this version's workflows) and is retained as an inert tag.
v1.1.1 is the first release with attached Mac app DMGs; all 1.1.0 changes below are included.

## 1.1.0 - 2026-06-11

Librarian 1.1.0 introduces the native macOS app: a self-contained download with the entire engine inside. Release builds bundle a relocatable Python runtime plus the Librarian wheel in `Librarian.app`, launch the backend automatically on a loopback port secured by a random per-launch API key, and store data in `~/Library/Application Support/Librarian`. The app offers drag-and-drop ingest, live per-run progress with expandable run events, cleaned-output viewing with classification, full-text search, Markdown export, and a backend readiness checklist — all over the same public HTTP API the CLI uses. DMG installers for Apple Silicon and Intel are built by the new `macapp.yml` workflow and attached to releases, with optional Developer ID signing and notarization via repository secrets, plus a download landing page under `site/`.

Engine and tooling changes:

- The Mac app's embedded backend requires a random per-launch API key, passed through
  `LIBRARIAN_API_KEY` to the spawned process and attached by the app's API client, so other
  local processes cannot read or modify the corpus over localhost.
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
  re-raise or carry an inline justification stating where the error is recorded. The 11
  deliberate boundary handlers are annotated; new silent swallows fail lint. A settings audit
  confirmed all 71 configuration fields are read by runtime code.
- Made the dependency-review workflow guard test version-agnostic so Dependabot action bumps do
  not break CI.
- Corrected `CONTRIBUTING.md` release status and `librarian maintainer` command examples, and
  trimmed the README Docker section to a pointer into docs/DEPLOYMENT.md.

## 1.0.0 - 2026-05-22

Librarian 1.0.0 is the stable release of the local-first document ingestion, cleaning, classification, and search engine. It ships a focused user CLI and FastAPI service for converting documents, importing corpora, running provenance-rich LLM cleaning, classifying outputs with Dewey-style labels, searching SQLite FTS indexes, and exporting cleaned content with optional transcript citation evidence. The release supports Markdown, text-like files, DOCX, PDFs, OCR images, and SRT/VTT transcript normalization, including page-aware PDF extraction with durable OCR page manifests for long-running jobs. Operational commands are grouped under `librarian admin`, while evaluation and benchmark tools are grouped under `librarian maintainer` so the production surface stays clear. The release workflow keeps secret scanning, dependency audit, SBOM generation, checksums, artifact attestations, wheel smoke installation, Docker build, and image scanning, while removing alpha-era mock evidence artifacts from published releases.
