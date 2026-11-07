# Rust foundation: implemented scope

The rebuild has a working CLI and a shared Rust/WASM document assembly engine. This is the first implementation slice, not a replacement release for the legacy application. The full pipeline design is in [Terminal rebuild architecture](TERMINAL_REBUILD.md).

## Try the CLI

From the repository root, with Rust installed:

```sh
cargo run --locked -p librarian-cli -- convert source.txt -o result.md
cargo run --locked -p librarian-cli -- convert source.md
cargo run --locked -p librarian-cli -- inspect source.md
```

`convert` accepts UTF-8 `.txt`, `.md`, and `.markdown` files. Text becomes literal Markdown paragraphs; punctuation that would introduce Markdown syntax is escaped. Existing Markdown is preserved, with a final newline added if missing. Tables, footnotes, math, and Mermaid source in existing Markdown remain intact. This does not yet normalize an existing document into typed table/list blocks or copyedit its prose.

The default output is stdout. `-o` writes a new `.md` file, using a temporary file in the destination directory and refusing to replace an existing file. The destination directory must exist. File contents are synced before publication; Unix hosts also sync the directory afterward and distinguish a post-publication durability failure. This is a single-file export contract, not yet a recoverable corpus transaction.

`inspect` reports syntax counts as JSON. Counting a Mermaid fence does not validate its syntax or confirm that a diagram matches its source material.

## Shared implementation

| Crate | Responsibility |
| --- | --- |
| `librarian-core` | Source spans, coverage validation, explicit block assembly, deterministic Markdown rendering, BLAKE3 source fingerprints |
| `librarian-app` | Text/Markdown import and versioned JSON commands; no host I/O |
| `librarian-native` | Bounded UTF-8 file reads and publication of new Markdown files |
| `librarian-cli` | clap commands over the native adapter |
| `librarian-wasm` | wasm-bindgen entry point over the same application implementation |

Source spans use half-open UTF-8 byte offsets. Every source byte must belong to exactly one block fragment or an explicit exclusion. Invalid character boundaries, missing coverage, overlapping ownership, and inconsistent whitespace exclusions fail before rendering. Source identities distinguish occurrences; a content fingerprint never merges repeated passages or pages.

A logical paragraph can own fragments from multiple pages. The caller explicitly chooses a join: no inserted character, a space, or a line break. The current engine verifies and assembles those decisions; automatic reading order, boundary inference, and model-assisted decisions are future work. Byte accounting alone does not prove that an interpretation or exclusion is correct.

The result includes Markdown, output byte ranges for each block, originating source spans, source fingerprints, and syntax counts. These are block-level mappings, not character-level alignments after copyediting. Source offsets refer to the supplied UTF-8 observation, not coordinates in a PDF or image.

The JSON interface accepts `schema_version: 1` with `convert` or `assemble` actions and returns a versioned `ok` or `error` response. Unknown fields and unsupported versions are rejected. See the independently authored [conformance fixtures](../tools/conformance/fixtures.mjs) for examples. Each batch currently allows 16 MiB of source text; streaming and durable checkpoints for long documents are not implemented yet.

## Verify the foundation

The repository pins Rust 1.96.0, Cargo dependencies, and JavaScript development dependencies. Native checks:

```sh
cargo fmt --all --check
cargo test --workspace --locked
cargo clippy --workspace --all-targets --locked -- -D warnings
```

For native/WASM equivalence and diagram compatibility, install Node.js 24 and wasm-pack 0.15.0, then run:

```sh
cargo install wasm-pack --version 0.15.0 --locked
npm ci --prefix tools/conformance --ignore-scripts --no-audit --no-fund
node tools/conformance/boundaries.mjs
wasm-pack build crates/librarian-wasm --target nodejs --out-dir pkg --release -- --locked
wasm-pack build crates/librarian-wasm --target web --out-dir pkg-web --release -- --locked
cd tools/conformance
npx playwright install chromium
npm test
```

On Linux, Playwright may also require `npx playwright install --with-deps chromium`. To use an existing Chromium executable, set `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` when running the test harness.

The harness runs independently authored golden cases through native Rust, Node-loaded WASM, and WASM in a real Chromium worker. It compares the full responses between hosts as well as expected Markdown/error outcomes. It also parses and renders two Mermaid samples with Pele 1.13.0 and Mermaid.js 12.3.0, checking SVG validity and expected labels. These samples establish an executable compatibility test; they do not establish support for every diagram type, layout, or Mermaid renderer. JavaScript packages and Chromium are development tools, not dependencies of the native executable.

[Rust foundation CI](../.github/workflows/rust-foundation.yml) is configured to test and build on Apple Silicon, Intel macOS, Windows x86-64, and Linux x86-64, plus run the WASM/browser harness. Local verification on the development Mac does not establish that the other native jobs have passed. Uploaded CI artifacts are development builds; signed releases and complete build-input/model manifests remain future work.

## Next implementation gates

1. Qualify pinned PDFium and OCR/layout model assets across all four native targets and the browser, including preprocessing, decoding, licenses, redistribution, and model manifests. Measure extraction quality and resource use on representative documents.
2. Add persistent jobs and artifacts, cancellation, crash recovery, incremental processing, and exact-result caching. Keep source evidence authoritative; indexes and exports must be rebuildable.
3. Implement document adapters, reading order and paragraph continuity, structured tables/equations/figures, and provider adapters with budgets, prompt caching, retries, and validation.
4. Add configurable copyediting/classification/export policy, SQLite FTS5 search, and the TUI. Establish quality, speed, memory, and cost baselines before making performance claims.

There is currently no PDF/DOCX/HTML extraction, OCR, model call, copyediting, automatic classification, search index, TUI, or GUI in the Rust implementation. The Python/Swift application remains available as reference during migration.
