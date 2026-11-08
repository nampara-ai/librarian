# Librarian

A local-first document cleaner and organizer. Import files or folders, extract text and visual material, copyedit with a configured model, classify documents, and export a searchable **Markdown** collection.

This branch implements the Rust CLI/TUI rebuild. Graphical application work is deferred. The previous Python/Swift implementation remains available as reference; its documentation is in [LEGACY_README.md](docs/LEGACY_README.md).

Native archives are available from successful [Librarian terminal builds](https://github.com/nampara-ai/librarian/actions/workflows/rust-foundation.yml) runs for this branch. Download the artifact matching your platform, unpack the artifact ZIP and the platform archive inside it, and keep `librarian` beside its `engines` folder. Run `./librarian` (`.\librarian.exe` in PowerShell). The bundled distribution includes OCR, PDF and private Office layout engines.

To build from source:

```sh
cargo build --release --locked -p librarian-cli
./target/release/librarian
```

Source builds can install the OCR/PDF engines during setup; Office layout additionally needs LibreOffice configured through `processing.office_renderer`. The full native packages include that renderer.

First launch guides you through a runtime profile: workspace, provider, masked API key, secure/session storage, live model selection, processing/Markdown defaults, engine initialization and an explicit connection/vision check. Later launches load the profile automatically. No environment activation or manual key exports are required.

In Home: `n` New Import, `q` Start queue, `p` Pause, `r` Resume, `c` Cancel, `s` Search, `f` Setup, `h` Help. Arrows or Tab navigate; Enter/Space activates; Esc goes back. Source and destination fields open the shared browser. Files run sequentially with live progress. Ctrl+S saves accepted drafts, requests a pause for active/queued work, and exits; Esc at Home leaves a worker running. See the [terminal guide](docs/RUST_FOUNDATION.md) for browser behavior, clipboard controls, credentials and recovery.

```sh
librarian parse /path/to/document.pdf -o /path/to/parsed --create-output
librarian clean /path/to/source-folder -o /path/to/results --create-output --recursive
librarian search 'a phrase'
librarian show 12
librarian print 12
librarian prepare /path/to/slides.pptx
librarian pages 12 --page 2
librarian catalog
librarian stats
```

Native build targets: Apple Silicon macOS, Intel macOS, Windows x64, and Linux x64. Distribution archives carry pinned PDFium, CPU ONNX Runtime, PaddleOCR models and LibreOffice. Native users do not need Python or Node. The shared document core also builds to WASM; browser hosts supply I/O, inference, and storage.

Supported inputs include UTF-8 text/Markdown, transcripts, CSV/TSV, HTML, PDF, DOCX/PPTX and other supported Office formats, plus common raster images including multi-page TIFFs. Native text receives CMOS copyediting while ordered page images receive local OCR and vision interpretation. The Typesetter reconciles these readings, restores continuity and combines narrative visual explanations with CSV, TeX and portable Pele/Mermaid. A final Copyeditor repairs the assembled Markdown before cataloging and publication.

Originals remain immutable. SQLite journals page readings, editorial checkpoints, usage reservations, category accessions and exact-result caches. Exports never replace existing files. Contextual spelling, punctuation, quotation and number-format repairs do not trigger archival-fidelity rejection. Technical failures remain explicit and resumable. Old jobs keep their original pipeline; `retry` or **r** on a finished job starts a new run with current settings and preserves the earlier result.

`parse` is the new deterministic foundation: it turns a PDF into typed layout blocks, reader Markdown and JSON in well under a second with no model calls, and reports which pages would need OCR, vision or review. The [parsing foundation](docs/PARSING_FOUNDATION.md) describes the direction, architecture and benchmark against Nutrient.

Read the [terminal guide](docs/RUST_FOUNDATION.md) for configuration, credentials, commands, caching, recovery, limits, and builds. The [editorial contract and work log](docs/EDITORIAL_PIPELINE.md) describe the current architecture. Earlier [architecture notes](docs/TERMINAL_REBUILD.md) and [implementation history](docs/IMPLEMENTATION_PROGRESS.md) remain as background. See [third-party notices](docs/THIRD_PARTY.md) for bundled components.

This is a development rebuild. Compilation and targeted invariant checks do not establish OCR accuracy, model quality, or complete cross-platform runtime behavior. Hands-on evaluation with Jake's documents and API keys follows the terminal implementation.
