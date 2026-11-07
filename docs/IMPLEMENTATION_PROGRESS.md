# Terminal implementation ledger

This ledger records the earlier terminal foundation. The current [editorial contract and work log](EDITORIAL_PIPELINE.md) supersedes its selective-vision and archival-fidelity policies. New runs use parallel native copyediting and page image/OCR/vision readings, Typesetter synthesis and corrective final editing; old jobs retain their saved pipeline. Current commands and controls are in the [terminal guide](RUST_FOUNDATION.md).

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
- [x] One shared first-run wizard and active runtime profile for CLI/TUI/workers; secure platform credential storage plus reconnectable session keys
- [x] Prefilled provider endpoints, dated vision-ranking hints, live model discovery, and explicit fresh-worker vision/protocol verification before activation
- [x] Shared arrows/Tab, Enter/Space, Esc navigation; exact Home hotkeys; Ctrl+S draft preservation, queue pause and exit; revised bundled terminal guide

Build/check evidence on the development Apple Silicon Mac:

- Workspace compilation and Clippy with warnings denied passed during implementation; final revision checks are recorded by CI.
- Apple Silicon and Intel macOS release archives built locally with bundled engines/models/licenses. Both packaged executables initialized PDFium and both OCR model sessions; the Intel executable ran through Rosetta on this Mac.
- Web and Node WASM bindings built successfully.
- Two targeted automated checks passed: byte-for-byte preservation across long Unicode transport/edit boundaries plus publication/index recovery, and concurrent durable cost/token reservations across independent SQLite connections and restart.
- Real PDFium loading and both Paddle OCR session initializations passed after fixing an automatic-device-selection incompatibility. The CPU runtime uses compatible API 21 bindings. A truncated recognition-model download was replaced with the complete upstream archive and its corrected digest.
- The four-platform workflow builds, runs the two critical checks, packages assets, and initializes real bundled engine sessions. Shared model archives are downloaded and verified once on Linux, then verified again during each platform's packaging; this avoids a model-host HTTP 403 encountered on the macOS runner. Platform results must be read from that workflow rather than inferred from local builds.

Latest cross-platform evidence: [workflow run 42363971926](https://github.com/nampara-ai/librarian/actions/runs/42363971926) passed at source commit `c193e5d0482f873d592237dcb9ac4cf846a61a6e` on November 7, 2026. Apple Silicon macOS, Intel macOS, Windows x64, and Linux x64 each passed Clippy, release compilation, the two critical checks, archive packaging, and real bundled PDF/OCR session initialization. Four native artifacts with checksum sidecars and the web/Node WASM artifact were uploaded. This revision includes runtime-profile onboarding, live model discovery, credential storage/session recovery, unified controls, and the rewritten bundled terminal guide.

The Apple Silicon package was also rebuilt locally, initialized its PDF/OCR sessions, and replaced the unpacked app at `dist/librarian-aarch64-apple-darwin/librarian`; previous local copies were retained under `.build-assets/local-backups`. A terminal walkthrough in an isolated temporary workspace exercised first-run setup, arrows/Tab, Space browsing, Esc/back after a discovery failure, Ctrl+C without a TUI exit binding, and Ctrl+S draft preservation/exit. No API key, inference request, or user document was used in that walkthrough. These results establish the recorded build and navigation properties, not document-processing quality or compatibility with every OS configuration.

Still reserved for Jake's hands-on evaluation: real document OCR accuracy, complex reading order, table/math/diagram fidelity, live model behavior, provider cost/cache behavior, terminal ergonomics, and throughput. No API keys or real evaluation documents have been supplied; no live provider calls have been made. Document-processing native FFI executes in the worker process; a native crash can terminate that worker, with committed work recoverable afterward. Setup also initializes its PDF/OCR libraries in the setup process. Browser host storage/OCR/provider adapters and graphical UI are outside this terminal implementation.

NFT clarification: minimal, meaningful testing is permitted for load-bearing invariants and essentially mandatory checks. Avoid performative tests, trivial assertions and flaky harnesses; do not equate green checks with a working product. Earlier broad assistant-authored tests/conformance harnesses were removed rather than restored wholesale. Existing Python/Swift tests remain untouched. Existing Keychain entries are never deleted. User-entered keys can be stored under new Librarian credential references through Setup. Unrelated repositories remain untouched.
