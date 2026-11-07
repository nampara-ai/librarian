# Librarian terminal guide

The CLI and TUI share one Rust processing engine. Native targets are Apple Silicon macOS, Intel macOS, Windows x64, and Linux x64. The portable document core also compiles to WASM. Graphical application work is deferred.

## Install

Unpack the platform archive and keep the executable beside its `engines` directory. Run `./librarian` on macOS/Linux or `.\librarian.exe` in PowerShell. No Python, Node, Homebrew, or system OCR installation is required for a bundled distribution. Linux targets glibc 2.35 or newer, including current Arch/Omarchy; musl distributions are not a supported binary target.

For a source build:

```sh
cargo build --release --locked -p librarian-cli
./target/release/librarian
```

On first launch, Librarian opens setup **before Home**. Its Rust runtime needs no Python virtual environment, shell activation, or manual export commands. The same wizard runs on all four native platforms.

## First launch: establish your runtime profile

Each step asks one question or performs one visible operation. Accept the defaults with Enter/Space. Automatic operations wait for completion; press `x`, Enter, or Space to continue. Esc goes back without losing accepted answers.

1. Start setup.
2. Choose the workspace for originals, jobs, caches and the search index. This is separate from each import's Markdown destination. You can use the default, browse, or type a path.
3. Librarian creates and checks local storage.
4. Choose a provider. Its API base URL is filled automatically. Custom compatible endpoints are also supported.
5. Paste your API key into a masked field, reuse a saved credential, or import a key already present in your shell. The provider's key-page link is available if you need an account/key. Local servers may use no key.
6. Choose secure storage or session-only storage. macOS uses Keychain, Windows uses Credential Manager, and Linux uses Secret Service. If Linux has no unlocked Secret Service, session-only mode works without installing a desktop environment. Existing Keychain entries are never deleted.
7. Librarian retrieves the provider's **live model catalog** with your credential. Discovery errors remain visible; a stale model list is never substituted.
8. Select a model for text and vision. Search by typing; use arrows or Tab to navigate. The list labels reported image support and unknown capabilities. No model slug needs to be typed.
9. Set per-document spending or generated-token limits. Missing catalog prices are shown as unknown, never free.
10. Review processing defaults: copyediting, classification, selective vision, local OCR, language and editorial style.
11. Review Markdown defaults: metadata, title, synopsis, page markers, review notes and filenames.
12. Initialize PDF/OCR engines. Bundled engines are reused; source builds download pinned, checksum-verified assets automatically. The optional external Tesseract engine requires its own installation.
13. Explicitly run a small billed API check. A fresh worker loads the draft profile and credential, reads a built-in image, and returns validated structured data. One request, at most 1,024 generated tokens; no user documents. This checks authentication, worker handoff, image input and protocol compatibility, not document quality.
14. Review and activate the profile, then enter Home.

Accepted answers are saved in a non-secret setup draft. Ctrl+S saves that draft and exits; unaccepted text and unsaved API keys are discarded. On the next launch, resume the draft or start from your current configuration. Setup does not rewrite existing queued jobs. Choosing a workspace that already has a configuration offers to load those settings.

The active profile supplies workspace, credentials, provider, models, engines and defaults automatically to future CLI/TUI launches. Session-only credentials remain available to workers started by that process; a new independent launch needs the key again. Choose “Reconnect the previous session key” to restore access for jobs already queued under that profile. Creating a new credential keeps older queued jobs bound to their original reference. A job with an unavailable key reports a credential error without preventing other files from processing. Keys never appear in ordinary config files, job snapshots, logs, or command-line arguments. Credential references are bound to their provider endpoint.

Provider presets include Anthropic, Alibaba Qwen, Google, OpenAI, xAI, Z.ai, Moonshot, DeepSeek, MiniMax, Mistral, OpenRouter, Together, Fireworks, Groq, NVIDIA, Hugging Face, Nebius, Novita, SiliconFlow, SambaNova, Ollama, LM Studio and custom compatible endpoints. Account access and API compatibility are verified for the selected model during setup; inclusion is not a claim that every provider has been exercised with a paid account.

Direct providers are sorted by their best listed model's [Vision Arena](https://arena.ai/leaderboard/chat/vision) point estimate as of **2026-11-06**. Scores and the snapshot date are displayed. This is a general vision preference benchmark with overlapping confidence intervals, not a document-OCR ranking. Gateways/local endpoints are listed separately without invented scores. Current model availability always comes from the live provider catalog.

`doctor --load-engines` diagnoses actual library loading and OCR model-session construction without provider requests. `engines install` installs the pinned assets manually if needed. Default data lives in the platform application-data location for **librarian-terminal**, separate from the legacy app. The active-profile pointer lives there too. Global `--library PATH` selects another workspace; `--config PATH` overrides its configuration. `config path` prints the resolved TOML path.

## Controls: one set of expectations

| Context | Keys | Action |
| --- | --- | --- |
| Every list/form | Arrows, Tab, Shift+Tab | Move selection |
| Buttons, toggles, browsable fields | Enter or Space | Activate, toggle, or browse |
| Every screen | Esc | Back one level; accepted answers remain |
| Completed automatic setup step | `x`, Enter or Space | Continue; never bypass running work or errors |
| Text field | Enter / Esc | Accept / discard current edit |
| Text field | Left/Right, Home/End, Backspace/Delete | Edit normally; spaces remain text |
| Long setup details | PageUp / PageDown | Scroll explanatory text |
| TUI | Ctrl+S | Save accepted drafts, pause active/queued work in this workspace, and exit |

Home shortcuts do not fire while entering text or filtering a list. Ctrl+C has **no Librarian TUI binding**. Copy/paste belongs to your terminal: commonly Cmd+C/V on macOS and Ctrl+Shift+C/V in Windows/Linux terminals; some terminals also support Ctrl+C with selected text. Librarian enables bracketed paste for text fields. Ctrl+S is handled in raw terminal mode so it does not invoke Unix terminal flow control.

### Home

| Key | Action |
| --- | --- |
| `n` | New Import |
| `q` | Start queue |
| `p` | Pause selected job |
| `r` | Resume selected job and start queue |
| `c` | Cancel selected job; retain originals and committed work |
| `s` | Search |
| `f` | Setup |
| `h` | Help |
| Enter / Space | Preview selected output or diagnostics |
| Esc | Close the dashboard; its background worker continues |
| Ctrl+S | Save, request a pause for all active/queued jobs, and exit |

**Closing Home and pausing work are different actions.** Esc leaves the worker running. Ctrl+S requests a pause before exiting. Accepted chunks and queue changes are already persisted as work progresses. A remote request already sent may finish and be billed after a pause; new accepted-block commits and publication remain gated by job state. Resume later with `r` or `librarian resume --start`.

### New Import and the browser

Press `n`. Source and Destination are browsable fields: Enter/Space opens either one; Ctrl+B is an additional shortcut. Press `e` on either field to type a path, including a not-yet-existing output directory. Choose “Create destination if missing” when appropriate. In the destination browser, `n` creates a folder.

Inside the browser, arrows or Tab move selection. **Enter opens the highlighted folder or selects the highlighted file. Space selects the folder you are currently inside.** Esc returns through your folder history, then back to the form. Backspace opens the parent; `/` opens a typed-path field.

Back in the form, Enter/Space toggles each option. **Queue import is a separate button** below the “Start processing after queueing” toggle. Nothing starts until that button is activated. The destination is checked before queueing. Esc returns Home and retains the unfinished import draft.

Files run one at a time; requests within a file have bounded concurrency. The dashboard shows stages, progress, calls, local cache hits, tokens, estimated cost, output paths and review/failure status. Preview is capped at 64 KiB; `print` streams the complete file. Background diagnostics go to `worker.log` in the workspace.

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

`clean` without paths opens the shared interactive browser. Scripts must supply a destination. `--extract-only` disables editing, classification, and vision calls. `--no-vision` keeps text editing/classification but prohibits image requests. `--body-only` omits frontmatter, generated title/synopsis, page comments, and review notes; review status remains in the library. Global `--json` emits machine-readable results without animation.

`resume` retains saved configuration and accepted work. `retry` creates a new job using current configuration and the retained original. `export` applies current presentation settings to accepted content without model calls. Existing files are never overwritten; collisions receive a filename suffix.

Standalone CLI workers retain conventional Ctrl+C/SIGINT handling to request a graceful pause; Ctrl+S is the interactive TUI action.

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

Use Setup to select live models and save credentials. For scripts and older profiles, `provider.key_env` can still name an existing environment variable; a saved credential takes precedence. `provider.structured_output` selects strict schema, JSON mode, or prompt-described JSON for compatible endpoints; every response is still validated locally. Localhost endpoints may omit credentials. Remote endpoints require HTTPS. Hosted providers receive material needed by enabled operations. Extraction-only mode stays local after engine installation.

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
