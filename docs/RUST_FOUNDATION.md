# Librarian terminal guide

Librarian produces Markdown through an editorial pipeline designed for continuous reading and listening. The saved runtime profile supplies the workspace, credentials, models, engines and defaults to the CLI, TUI and background workers. No shell activation or export commands are needed.

## Launch and import

On Jake's Mac:

```sh
~/librarian/dist/librarian-aarch64-apple-darwin/librarian
```

On other platforms, run `librarian` (`librarian.exe` on Windows) from the extracted native package. Keep its `engines` directory alongside it. First launch guides you through setup. Provider selection fills the endpoint, retrieves live models, connects credentials and verifies access. Keys stay out of ordinary configuration and job snapshots.

1. Press **n** for New Import.
2. Select the source file or folder; choose recursion if needed.
3. Select or create the destination before processing.
4. Review processing/output options and add the jobs.
5. Press **q** to start the queue. Files run one at a time; independent work within a document runs concurrently.
6. Select a job and press **Enter** to see its output or diagnostics.

## Controls

| Control | Action |
| --- | --- |
| n | New Import |
| q | Start queue |
| p | Pause selected work |
| r | Resume paused/cancelled/budget-limited work; retry a finished or failed job as a new run using current settings |
| c | Cancel selected work; retain originals and checkpoints |
| s | Search |
| f | Setup |
| h | Help |
| Arrows, Tab, Shift+Tab | Navigate lists/forms |
| Enter, Space | Activate a button, toggle or browsable field |
| Esc | Back; at Home, close the dashboard while the worker continues |
| Ctrl+S | Save accepted drafts, pause workspace work and exit |
| x | Continue a completed automatic setup step |

Inside the file browser, Enter opens a folder/selects a file; Space selects the current folder; Esc goes back. Ctrl+B remains a browse shortcut. Text entry treats spaces and cursor keys normally. Librarian has no Ctrl+C binding in the TUI; use the terminal's clipboard commands. CLI workers still support conventional SIGINT.

## Editorial workflow

1. Snapshot the source. Quick-look metadata establishes provisional document context.
2. Prepare an ordered image bundle. PDFs render through PDFium; Office documents/slides convert through bundled LibreOffice in a private profile; text/Markdown gets a deterministic print layout. Raster image pages remain images.
3. Two branches run together: native text receives CMOS copyediting; each page receives fresh local OCR followed by multimodal reading of the image/OCR pair. Empty or failed OCR is explicitly represented so vision can read the image directly.
4. The Typesetter consumes ready pages in order, reconciles the cleaned skeleton with visual/OCR evidence, places narrative explanations and structured content, and carries unfinished prose across boundaries.
5. The Senior Document Reviewer receives the assembled passage, document brief, source-linked evidence and continuity ledger. It repairs the document, retrieves relevant source/image evidence when needed, and can revise earlier passages using hash-guarded exact edits. Protected literals remain intact unless explicitly repaired with independent evidence. Ordinary number formatting, prose compression and contextual transcription changes do not trigger whole-chunk rollback.
6. Final metadata, classification, category accession, atomic Markdown publication and full-text indexing complete the run.

There is no human approval gate for ordinary editorial decisions. Genuine engine, provider, budget and I/O failures remain visible and resumable. A completed document still requires human evaluation of the model's editorial/visual accuracy.

The default image reader requests narrative explanations that make figures/tables/diagrams understandable aloud, alongside CSV, TeX and portable Mermaid where appropriate. CSV and diagram source are useful structured supplements; an external TTS reader may still speak code fences unless configured to skip them. No TTS player or graphical renderer is included.

## Commands

```sh
librarian parse /path/source.pdf
librarian parse /path/source.pdf --output /path/parsed --create-output
librarian clean /path/source.pdf --output /path/finished --create-output
librarian clean /path/folder --output /path/finished --recursive
librarian run
librarian status --json
librarian show 3
librarian pages 3
librarian pages 3 --page 2
librarian prompts
librarian prompts copyeditor
librarian prompts reviewer
librarian editorial 3
librarian feedback 3 --segment review:2 --replacement corrected-section.md --reason "Explain the correction"
librarian prepare /path/source.docx
librarian pause 3
librarian resume 3 --start
librarian retry 3 --start
librarian cancel 3
librarian print 3
librarian export 3 --output /path/other-folder --create-output
librarian search 'horse groundwork'
librarian catalog
librarian stats
librarian doctor --load-engines
librarian config show
librarian config schema
librarian config edit
```

`parse` cracks a PDF locally with the bundled PDFium engine and writes reader Markdown plus a typed JSON layout model (titles, headings, paragraphs, lists, tables, code, captions, pictures, headers, footers, page numbers) with a per-page routing report. It makes no model calls and refuses to publish if any word is lost or duplicated. `--words` adds positioned words and fonts; `--furniture` keeps running headers and footers in the Markdown. See the [parsing foundation](PARSING_FOUNDATION.md).

`prepare` renders page images and runs local OCR without making model calls or publishing a cleaned document. Its status is `prepared`. `pages` exposes the ordered matrix: A=image, B=OCR, C=visual reading, plus native/copyedited text. With `--page`, it prints that page's actual contents. The hidden work area is `<workspace>/.librarian-work/job-ID/pages`, with numbered PNG/JSON/Markdown sidecars.

`retry` creates a new job from the retained original and **current** settings. It preserves the prior result. `resume` continues checkpoints with the job's original settings. Older archival jobs retain their pipeline, and existing editorial-v1 runs resume with their original prompts. Retry as a new job to adopt editorial-v2 and the new role prompts. Changing Setup does not rewrite queued jobs. Re-export makes no model calls.

## Role prompts, evidence and feedback

The exact immutable role texts ship in `prompts/copyeditor-v2.txt` and `prompts/reviewer-v2.txt`. In the repository they live under `crates/librarian-core/prompts/`. `prompts copyeditor` and `prompts reviewer` print the installed texts; `prompts` lists their versions and SHA-256 hashes. `prompts protocol` prints the separate response contract instructions.

Each request contains three layers: the permanent role plus stable harness protocol, user house style, and the document-specific payload. The payload identifies editable source, read-only neighboring passages, provisional metadata, source spans, protected artifacts and continuity decisions. Role hashes are frozen per run. `editorial JOB` exports the actual bounded request packets, responses, revisions and interventions; source originals and image pixels remain in the job's page matrix, not embedded in this JSON.

The Reviewer can request source text, previous passages or one page image per request. `processing.evidence_rounds` defaults to 3 per passage and is capped at 8. Missing evidence becomes a recorded limitation after the bounded retrieval allowance. Technical transport failures and lost protected content retain checkpoints and surface an error; editorial uncertainty alone does not impose a human approval gate.

Protection covers fenced/inline code, math, Markdown tables, metadata, destinations, references and recognized identifiers. Syntax diagnostics check JSON/YAML/TOML/XML, CSV/TSV row widths and delimiters. They do not compile arbitrary programs, fully validate Mermaid/TeX semantics or prove factual truth. Source-span accounting detects uncompleted/duplicate processing units; the model's claim-coverage assessment still needs human review. Diagnostics, intervention records and `editorial-report.json` stay under the private library/job work area, outside the finished Markdown.

`feedback` records the original evidence, current reviewed segment, your replacement and reason in the private library database. It does not overwrite the published document, retrain a model or silently change future prompts. These records form a local calibration set for deliberate editorial improvements. Source files, request packets and calibration records contain document content; they are local data, not telemetry.

## Options and limits

Setup exposes editing, visual reading, OCR/language, style, pipeline, page concurrency, classification override and office renderer location. Advanced controls remain available through the schema and `config set`:

```sh
librarian config set processing.pipeline editorial
librarian config set processing.classification_override 636.1
librarian config set processing.page_concurrency 3
librarian config set provider.concurrency 3
librarian config set processing.chunk_bytes 6000
librarian config set processing.evidence_rounds 3
librarian config set processing.house_style 'Use British spelling; retain the author’s profanity.'
librarian config set output.filename '{class} - {title} - {sequence}.md'
```

Filename fields: `{class}`, `{title}`, `{number}` (global job number), `{sequence}` (per-category accession), `{id}`. Classification and accession are separate values. Configure metadata field names in `output.fields`, and title/summary/page markers/frontmatter independently. Markdown is always the document output format.

Setup edits `processing.house_style`, a preference layer separate from the permanent roles. The old default CMOS prompt is recognized and omitted from that layer; a custom legacy `processing.style` is retained as a preference, with `house_style` overrides applied afterward. Existing configurations and job snapshots are preserved. `preserve_original_on_review` applies only to archival compatibility jobs.

Local caches avoid identical completed inference; provider prompt caches reuse stable instructions for new requests. These are different mechanisms. Separate concurrency, requests/minute, tokens/minute, context/output limits and per-job budgets apply. Source is subdivided on output exhaustion; continuation retains actual source material. Original text, page images, OCR, intermediate readings and stage checkpoints remain available for diagnosis and recovery.

Model-bound images have dimension and encoded-size limits. Compact PNGs remain lossless; larger scans use high-quality JPEG before any further resizing. Requests stay within four MiB of base64 image data, and the full source-page PNG remains in the page matrix.

Supported extraction routes include UTF-8 text/Markdown, CSV/TSV, SRT/VTT, HTML, PDF, DOCX/PPTX, common Office formats through the private renderer, and supported PNG/JPEG/BMP/WebP/TIFF/GIF images. Office conversion has a ten-minute deadline. Password-protected files need an accessible copy. The embedded text-print font supports its available glyphs; complex-script shaping is not implemented in that simple print adapter. Native text remains available independently.

## Builds and verification

Four native packages: Apple Silicon, Intel macOS, Windows x86-64 and Linux x86-64. Linux builds use an Ubuntu 22.04 baseline and ship the office renderer's non-glibc dependency closure. A clean Omarchy installation still needs hands-on acceptance; compilation does not prove every Linux environment works.

The WASM package exports the shared document model, editorial prompts and response schema, literal protection/restoration, guarded revision checks, semantic planning, handoffs and rendering contracts. Native filesystem, credentials, OCR and provider execution remain native host services; this is not a complete browser application.

```sh
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --locked -p librarian-core -p librarian-native --lib --test persistence
cargo build --release --locked -p librarian-cli --target aarch64-apple-darwin
python3 tools/package.py aarch64-apple-darwin --check-engines
```

Packaging downloads checksum-pinned LibreOffice, PDFium, ONNX Runtime and OCR models and retains licenses. The package check exercises actual DOCX/PPTX conversion, page ordering, PDF text and OCR. The small automated suite covers Unicode/source ownership, protected artifacts, budgets and publication recovery. These checks do not establish model editing quality; Jake's document runs are the product acceptance step.
