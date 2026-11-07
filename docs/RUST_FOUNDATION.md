# Librarian terminal guide

The CLI and TUI share one Rust processing engine. Native targets are Apple Silicon macOS, Intel macOS, Windows x64, and Linux x64. The portable document core also compiles to WASM. Graphical application work is deferred.

## Install

Unpack the platform archive and keep the executable beside its `engines` directory. Run `./librarian` on macOS/Linux or `.\librarian.exe` in PowerShell. No Python, Node, Homebrew, or system OCR installation is required for a bundled distribution. Linux targets glibc 2.35 or newer, including current Arch/Omarchy; musl distributions are not a supported binary target.

For a source build:

```sh
cargo build --release --locked -p librarian-cli
./target/release/librarian setup
./target/release/librarian engines install
./target/release/librarian
```

`engines install` verifies pinned archive SHA-256 hashes and installs PDFium, CPU ONNX Runtime, and PaddleOCR models under the selected library. Bundled distributions already contain them. `doctor --load-engines` diagnoses actual library loading and OCR model-session construction without importing documents or calling a provider.

Default data lives in the platform application-data location for **librarian-terminal**, separate from the legacy app. Global `--library PATH` and `--config PATH` select another library/configuration. `config path` prints the active TOML path. Import, cancellation, and retry do not delete originals or Keychain entries.

## Dashboard

Press `,` for setup, then `n` for an import. Enter source and destination; `Ctrl+B` opens the filesystem browser. Enter opens folders/selects a file, Space selects the current folder, Backspace moves up. Tab moves through options. Explicitly choose whether to create a missing destination. Writability is checked before queueing.

Files run one at a time. Within-file model requests have bounded concurrency.

| Key | Action |
| --- | --- |
| `s` | Start queued work |
| `p` / `r` / `c` | Pause / resume / cancel selected job |
| `v` / Enter | Preview output or diagnostics |
| Arrows / PgUp / PgDn | Navigate or scroll a preview |
| `/` | Search accepted document text |
| `,` | Configure new jobs |
| `?` | Help |
| `q` | Close dashboard; background worker continues |
| Ctrl+C | Pause active/queued work and close |

The dashboard shows stages, progress, calls, local cache hits, tokens, configured-price cost estimates, outputs, and review/failure status. Preview is capped at 64 KiB; `print` streams the complete file. Background diagnostics also go to `worker.log` in the library directory.

## Commands

```sh
librarian clean document.pdf -o results --create-output
librarian clean source-folder -o results --recursive --queue-only
librarian run
librarian status --watch
librarian show 12
librarian pause 12
librarian resume 12 --start
librarian retry 12 --start
librarian cancel 12
librarian search 'climate adaptation'
librarian print 12
librarian export 12 -o another-folder --create-output
```

`clean` without paths prompts interactively. Scripts must supply a destination. `--extract-only` disables editing, classification, and vision calls. `--no-vision` keeps text editing/classification but prohibits image requests. `--body-only` omits frontmatter, generated title/synopsis, page comments, and review notes; review status remains in the library. Global `--json` emits machine-readable results without animation.

`resume` retains saved configuration and accepted work. `retry` creates a new job using current configuration and the retained original. `export` applies current presentation settings to accepted content without model calls. Existing files are never overwritten; collisions receive a filename suffix.

Processing exit codes: `0` complete, `1` failure/budget exhausted, `2` review needed, `3` paused/cancelled. A failed file does not discard the remaining queue.

## Configuration

```sh
librarian config show
librarian config schema
librarian config set provider.model MODEL_NAME
librarian config set provider.vision_model VISION_MODEL_NAME
librarian config set provider.concurrency 3
librarian config set processing.chunk_bytes 6000
librarian config set output.page_markers true
librarian config set output.filename '{class} - {title} - {number}.md'
librarian config edit
```

`schema` prints JSON Schema. `set` accepts dotted keys and JSON values or ordinary strings. `edit` uses `$VISUAL`/`$EDITOR`; `reset` backs up the prior file. Unknown fields and invalid settings are rejected.

| Provider kind | Base endpoint convention | Prompt caching |
| --- | --- | --- |
| `responses` | `/v1`; appends `/responses` | `implicit`, `explicit`, `off` |
| `chat` | OpenAI-compatible `/v1`; appends `/chat/completions` | `implicit`, `off` |
| `anthropic` | Anthropic-compatible `/v1`; appends `/messages` | `ephemeral`, `off` |

Choose models explicitly. `provider.key_env` names the environment variable holding credentials; the configuration stores its name, never the secret value. Localhost endpoints may omit credentials. Remote endpoints require HTTPS. Hosted providers receive material needed by enabled operations. Extraction-only mode stays local after engine installation.

Stable instruction prefixes enable provider caching. The separate local exact-response cache includes endpoint, namespace, model, prompt, schema, input/images, and output limit. Identical concurrent requests are coalesced. Change `provider.namespace` when changing accounts or trust contexts. `cache_ttl_hours` sets reuse duration.

Provider controls include requests/minute, conservative tokens/minute, concurrency, timeout, and bounded retries. Rate limits span sequential files in one worker. Per-job budgets cover request count, generated tokens, and optional estimated cost. A cost ceiling requires input/output prices; a different vision model requires its own prices. These are estimates from configuration, not invoices. In-flight allowances are reserved transactionally. Unknown billing after timeout/crash remains reserved; a request already running remotely may still be billed after cancellation.

`processing.style` and `glossary` control copyediting. Defaults follow Chicago conventions while preserving facts, quotations, numbers, names, citations, meaning, and voice. `preserve_original_on_review` retains source content when protected-material checks fail. Those checks do not prove semantic correctness.

`taxonomy` maps codes to descriptions. Defaults are ten broad Dewey-style classes, not a licensed full Dewey implementation. `output.fields` maps semantic metadata to frontmatter keys; omit a mapping to omit its field. Switches control title, synopsis, frontmatter, review notes, and page comments. Filename variables: `{title}`, `{class}`, `{id}`, `{number}`; names are sanitized for Windows and Unix.

## Extraction and output

- UTF-8 text uses streaming windows with continuation across transport boundaries. Long documents are journaled incrementally, not placed in one prompt.
- Markdown retains source structure, code, links, math, and Mermaid. An oversized Markdown structure is preserved with a review finding rather than split through its syntax.
- SRT/VTT retain timing and identifiers separately from editable speech.
- CSV/TSV retain quoted/multiline cells in bounded table batches; the first row is not silently declared a header.
- HTML preserves body text, lists, code, tables, local images, and TeX math annotations. Remote image URLs are not fetched automatically; missing assets are reported.
- DOCX preserves paragraphs, common emphasis, lists, merged cells, links, footnote placement/definitions, images, and common equation structures. Complex layouts/uncommon equations require review against the retained original.
- PDFium supplies PDF text, geometry, images, and page renders. Usable ordinary text takes the native route. Scans use OCR; complex pages/figures can use vision. Repeated native-text margin furniture can be excluded, with records retained.
- PNG, JPEG, BMP, WebP, GIF frames, and supported multi-page TIFF encodings are handled as image observations. Unsupported encodings fail explicitly.

The default OCR engine is PaddleOCRv5 mobile detection/recognition on CPU ONNX Runtime, with English/Chinese/Japanese recognition. `ocr=tesseract` uses an installed executable and `ocr_language` data. `ocr=vision` uses the selected vision model; `ocr=off` disables OCR. Uncertain local OCR can escalate to configured vision when enabled. OCR confidence is an engine estimate, not an accuracy guarantee.

Logical paragraphs can continue across physical pages. Neighboring context is read-only; edits commit only owned blocks. Fidelity findings retain questionable content for review. Native-text/vision discrepancies preserve native text by default. Page comments sit at logical boundaries to avoid breaking sentences.

Simple tables use GFM; merged/irregular tables use escaped HTML in Markdown. Equations use TeX. Supported flowcharts serialize to Mermaid fences with generated identifiers and quoted labels. Figures remain linked local assets, with generated descriptions labeled. Pele is preferred; common flowchart syntax is usable by other Mermaid renderers.

Encrypted PDFs require an unencrypted copy. Unsupported formats fail with diagnostics and retained originals. HTML, DOCX XML parts, and Markdown have explicit whole-parser size ceilings; plain-text streaming has separate limits. Handwriting, difficult scans, reading order, chart semantics, and copyediting quality require document-level evaluation.

## Durability and builds

SQLite WAL/full synchronization and an OS worker lock protect queue state. Content-addressed originals/assets remain immutable. Observations, plans, accepted blocks, inference cache, request attempts, usage reservations, and publication records are persisted. Model output never rewrites a source file.

Export stages/syncs Markdown in the destination and publishes without replacing a file. Its digest supports recovery after rename but before database completion/indexing. Pause/cancel blocks new publication and accepted-block commits. Local search uses SQLite FTS5. Back up the library and exports; originals/caches can contain sensitive source material even when output omits it.

```sh
wasm-pack build crates/librarian-wasm --target web --out-dir pkg-web --release -- --locked
wasm-pack build crates/librarian-wasm --target nodejs --out-dir pkg --release -- --locked
python3 tools/package.py aarch64-apple-darwin --binary target/release/librarian
```

WASM exposes `execute_json`: `schema_version: 1` with actions `convert`, `assemble`, `render_corpus`, `plan_text`, `inspect_edit`, and `filename`. Native/WASM share document rules. Browser hosts supply storage, PDF/OCR adapters, and provider transport; the native terminal and FFI runtimes are not browser modules.

The build matrix produces four native archives with engines/models/licenses and SHA-256 sidecars, plus web/Node WASM bindings. These remain development artifacts until release signing/publication is arranged. Local builds do not establish other-platform or real-document success.

Verification stays narrow: formatting, Clippy, compilation, and critical checks for byte preservation/publication recovery and concurrent durable budget reservations. Jake owns hands-on evaluation with real documents/API keys. A passing check covers only its stated invariant.
