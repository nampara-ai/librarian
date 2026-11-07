# Librarian

A local-first document cleaner and organizer. Import files or folders, extract text and visual material, copyedit with a configured model, classify documents, and export a searchable **Markdown** collection.

This branch implements the Rust CLI/TUI rebuild. Graphical application work is deferred. The previous Python/Swift implementation remains available as reference; its documentation is in [LEGACY_README.md](docs/LEGACY_README.md).

```sh
cargo build --release --locked -p librarian-cli
./target/release/librarian setup
./target/release/librarian engines install
./target/release/librarian
```

In the dashboard, press `n`, select the source and destination, choose whether to create a missing destination, and start. `Ctrl+B` opens the file/folder browser. Files are processed sequentially, with live progress and pause/resume/cancel controls.

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
