# Terminal implementation ledger

Scope: implement the terminal corpus pipeline and portable engine described in TERMINAL_REBUILD.md. No graphical application. Existing Python/Swift sources and unrelated repositories are reference-only.

Implemented in this branch (these are implementation records, not quality guarantees):

- [x] Rust CLI/TUI and shared native/WASM document core
- [x] Structured content, page continuation, bounded edit planning and fidelity findings
- [x] SQLite journals, immutable originals, publication recovery, pause/cancel, exact-result caching
- [x] Responses, Chat-compatible and Anthropic adapters; selective vision, retries, durable budget reservations and usage accounting
- [x] UTF-8 text/Markdown, SRT/VTT, CSV/TSV, HTML, DOCX, PDF and raster image adapters
- [x] PDFium text/geometry/rendering; PaddleOCR CPU model sessions; optional Tesseract
- [x] Structured tables, equations, figures and deterministic Mermaid flowcharts
- [x] Metadata/classification, configurable Markdown export and FTS5 search
- [x] Source/destination browser, explicit create-folder choice, sequential queue and live terminal progress
- [x] Four-platform packaging with pinned engines/models/licenses and WASM web/Node bindings

Build/check evidence on the development Apple Silicon Mac:

- Workspace compilation and Clippy with warnings denied passed during implementation; final revision checks are recorded by CI.
- Apple Silicon and Intel macOS release archives built locally with bundled engines/models/licenses. Both packaged executables initialized PDFium and both OCR model sessions; the Intel executable ran through Rosetta on this Mac.
- Web and Node WASM bindings built successfully.
- Two targeted automated checks passed: byte-for-byte preservation across long Unicode transport/edit boundaries plus publication/index recovery, and concurrent durable cost/token reservations across independent SQLite connections and restart.
- Real PDFium loading and both Paddle OCR session initializations passed after fixing an automatic-device-selection incompatibility. The CPU runtime uses compatible API 21 bindings. A truncated recognition-model download was replaced with the complete upstream archive and its corrected digest.
- The four-platform workflow builds, runs the two critical checks, packages assets, and initializes real bundled engine sessions. Shared model archives are downloaded and verified once on Linux, then verified again during each platform's packaging; this avoids a model-host HTTP 403 encountered on the macOS runner. Platform results must be read from that workflow rather than inferred from local builds.

Cross-platform evidence: [workflow run 42357393373](https://github.com/nampara-ai/librarian/actions/runs/42357393373) passed at source commit `50ca8f451e8485cf4545567b66f51590f75dcffa` on November 7, 2026. Apple Silicon macOS, Intel macOS, Windows x64, and Linux x64 each passed Clippy, release compilation, the two critical checks, archive packaging, and real bundled PDF/OCR session initialization. Four native artifacts with checksum sidecars and the web/Node WASM artifact were uploaded. These results establish those build/runtime initialization properties, not document-processing quality or compatibility with every OS configuration.

Still reserved for Jake's hands-on evaluation: real document OCR accuracy, complex reading order, table/math/diagram fidelity, live model behavior, provider cost/cache behavior, terminal ergonomics, and throughput. No API keys or real evaluation documents have been supplied; no live provider calls have been made. Native FFI executes in the worker process; a native crash can terminate that worker, with committed work recoverable afterward. Browser host storage/OCR/provider adapters and graphical UI are outside this terminal implementation.

NFT clarification: minimal, meaningful testing is permitted for load-bearing invariants and essentially mandatory checks. Avoid performative tests, trivial assertions and flaky harnesses; do not equate green checks with a working product. Earlier broad assistant-authored tests/conformance harnesses were removed rather than restored wholesale. Existing Python/Swift tests remain untouched. Keychain entries and unrelated repositories remain outside this work.
