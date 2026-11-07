# Librarian

A local-first document cleaner and organizer. Import files or folders, extract text and visual material, copyedit with a configured model, classify documents, and export a searchable **Markdown** collection.

This branch implements the Rust CLI/TUI rebuild. Graphical application work is deferred. The previous Python/Swift implementation remains available as reference; its documentation is in [LEGACY_README.md](docs/LEGACY_README.md).

Native archives are available from successful [Librarian terminal builds](https://github.com/nampara-ai/librarian/actions/workflows/rust-foundation.yml) runs for this branch. Download the artifact matching your platform, unpack the artifact ZIP and the platform archive inside it, and keep `librarian` beside its `engines` folder. Run `./librarian` (`.\librarian.exe` in PowerShell). The bundled distribution already includes its OCR/PDF engines.

To build from source:

```sh
cargo build --release --locked -p librarian-cli
./target/release/librarian
```

First launch guides you through a runtime profile: workspace, provider, masked API key, secure/session storage, live model selection, processing/Markdown defaults, engine initialization and an explicit connection/vision check. Later launches load the profile automatically. No environment activation or manual key exports are required.

In Home: `n` New Import, `q` Start queue, `p` Pause, `r` Resume, `c` Cancel, `s` Search, `f` Setup, `h` Help. Arrows or Tab navigate; Enter/Space activates; Esc goes back. Source and destination fields open the shared browser. Files run sequentially with live progress. Ctrl+S saves accepted drafts, requests a pause for active/queued work, and exits; Esc at Home leaves a worker running. See the [terminal guide](docs/RUST_FOUNDATION.md) for browser behavior, clipboard controls, credentials and recovery.

```sh
librarian clean /path/to/source-folder -o /path/to/results --create-output --recursive
librarian search 'a phrase'
librarian show 12
librarian print 12
```

Native build targets: Apple Silicon macOS, Intel macOS, Windows x64, and Linux x64. Distribution archives carry pinned PDFium, CPU ONNX Runtime, and PaddleOCR models. Native users do not need Python or Node. The shared document core also builds to WASM; browser hosts supply I/O, inference, and storage.

Supported inputs include UTF-8 text/Markdown, transcripts, CSV/TSV, HTML, DOCX, PDFs, and common raster images, including multi-page TIFFs. Ordinary PDF text uses native extraction; scans use local OCR; complex pages and figures can use vision. Tables, equations, figures, and flowcharts have structured Markdown representations. Pele is the preferred Mermaid renderer.

Originals remain immutable. SQLite journals jobs, accepted blocks, usage reservations, and exact-result caches. Exports never replace existing files. Questionable edits and extraction findings produce explicit review status.

Read the [terminal guide](docs/RUST_FOUNDATION.md) for configuration, credentials, commands, caching, recovery, limits, and builds. See [architecture](docs/TERMINAL_REBUILD.md), [implementation status](docs/IMPLEMENTATION_PROGRESS.md), and [third-party notices](docs/THIRD_PARTY.md).

This is a development rebuild. Compilation and targeted invariant checks do not establish OCR accuracy, model quality, or complete cross-platform runtime behavior. Hands-on evaluation with Jake's documents and API keys follows the terminal implementation.
