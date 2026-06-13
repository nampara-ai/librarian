# Changelog

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
