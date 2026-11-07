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
5. A final Copyeditor repairs the assembled document. CSV/code/math fences are protected during prose editing. Ordinary number formatting, repaired quotations and contextual transcription changes do not trigger whole-chunk rollback.
6. Final metadata, classification, category accession, atomic Markdown publication and full-text indexing complete the run.

There is no human approval gate for ordinary editorial decisions. Genuine engine, provider, budget and I/O failures remain visible and resumable. A completed document still requires human evaluation of the model's editorial/visual accuracy.

The default image reader requests narrative explanations that make figures/tables/diagrams understandable aloud, alongside CSV, TeX and portable Mermaid where appropriate. CSV and diagram source are useful structured supplements; an external TTS reader may still speak code fences unless configured to skip them. No TTS player or graphical renderer is included.

## Commands

```sh
librarian clean /path/source.pdf --output /path/finished --create-output
librarian clean /path/folder --output /path/finished --recursive
librarian run
librarian status --json
librarian show 3
librarian pages 3
librarian pages 3 --page 2
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

`prepare` renders page images and runs local OCR without making model calls or publishing a cleaned document. Its status is `prepared`. `pages` exposes the ordered matrix: A=image, B=OCR, C=visual reading, plus native/copyedited text. With `--page`, it prints that page's actual contents. The hidden work area is `<workspace>/.librarian-work/job-ID/pages`, with numbered PNG/JSON/Markdown sidecars.

`retry` creates a new job from the retained original and **current** settings. It preserves the prior result. `resume` continues checkpoints with the job's original settings. Old queued jobs retain their archival pipeline; retry them to use the new editorial pipeline. Changing Setup does not rewrite queued jobs. Re-export makes no model calls.

## Options and limits

Setup exposes editing, visual reading, OCR/language, style, pipeline, page concurrency, classification override and office renderer location. Advanced controls remain available through the schema and `config set`:

```sh
librarian config set processing.pipeline editorial
librarian config set processing.classification_override 636.1
librarian config set processing.page_concurrency 3
librarian config set provider.concurrency 3
librarian config set processing.chunk_bytes 6000
librarian config set output.filename '{class} - {title} - {sequence}.md'
```

Filename fields: `{class}`, `{title}`, `{number}` (global job number), `{sequence}` (per-category accession), `{id}`. Classification and accession are separate values. Configure metadata field names in `output.fields`, and title/summary/page markers/frontmatter independently. Markdown is always the document output format.

The exact former built-in copyediting style upgrades to Jake's original contextual CMOS prompt on configuration load. Custom styles survive. `preserve_original_on_review` applies only to archival compatibility jobs, not the editorial pipeline.

Local caches avoid identical completed inference; provider prompt caches reuse stable instructions for new requests. These are different mechanisms. Separate concurrency, requests/minute, tokens/minute, context/output limits and per-job budgets apply. Source is subdivided on output exhaustion; continuation retains actual source material. Original text, page images, OCR, intermediate readings and stage checkpoints remain available for diagnosis and recovery.

Model-bound images have dimension and encoded-size limits. Compact PNGs remain lossless; larger scans use high-quality JPEG before any further resizing. Requests stay within four MiB of base64 image data, and the full source-page PNG remains in the page matrix.

Supported extraction routes include UTF-8 text/Markdown, CSV/TSV, SRT/VTT, HTML, PDF, DOCX/PPTX, common Office formats through the private renderer, and supported PNG/JPEG/BMP/WebP/TIFF/GIF images. Office conversion has a ten-minute deadline. Password-protected files need an accessible copy. The embedded text-print font supports its available glyphs; complex-script shaping is not implemented in that simple print adapter. Native text remains available independently.

## Builds and verification

Four native packages: Apple Silicon, Intel macOS, Windows x86-64 and Linux x86-64. Linux builds use an Ubuntu 22.04 baseline and ship the office renderer's non-glibc dependency closure. A clean Omarchy installation still needs hands-on acceptance; compilation does not prove every Linux environment works.

The WASM package exports the shared document model, editorial prompts, semantic planning, handoffs and rendering contracts. Native filesystem, credentials, OCR and provider execution remain native host services; this is not a complete browser application.

```sh
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --locked -p librarian-core -p librarian-native --lib --test persistence
cargo build --release --locked -p librarian-cli --target aarch64-apple-darwin
python3 tools/package.py aarch64-apple-darwin --check-engines
```

Packaging downloads checksum-pinned LibreOffice, PDFium, ONNX Runtime and OCR models and retains licenses. The package check exercises actual DOCX/PPTX conversion, page ordering, PDF text and OCR. The small automated suite covers Unicode/source ownership, protected artifacts, budgets and publication recovery. These checks do not establish model editing quality; Jake's document runs are the product acceptance step.
