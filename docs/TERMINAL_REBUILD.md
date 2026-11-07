# Terminal rebuild architecture

Librarian is a local-first corpus cleaner and organizer. It reads documents, extracts their contents, copyedits prose to a configurable CMOS style, classifies documents using a Dewey-style taxonomy, and indexes the collection for full-text search. All output documents are UTF-8 Markdown (`.md`) from the first release, delivered through a CLI and TUI. Pele is the primary diagram rendering target, with standard Mermaid source and tested compatibility with other renderers. Graphical applications follow terminal acceptance.

This is the design baseline for the rebuild, not a description of implemented capabilities. The first shared Rust/CLI/WASM slice is implemented; see [Rust foundation status and commands](RUST_FOUNDATION.md) for its tested scope. PDF/OCR engine choices remain subject to the native and WASM portability experiment below. Optimize measured document quality, elapsed time, memory, and total cost together.

## Concrete starting stack

| Responsibility | Starting choice |
| --- | --- |
| Shared document model, interpretation policies, validation, assembly, Markdown export | Rust workspace with a platform-independent core; Serde for versioned records |
| Markdown syntax and formatting | Deterministic Rust serializer over structured blocks; pulldown-cmark for parsing and structural checks with explicit extension flags |
| Diagram compatibility | Standard Mermaid fenced blocks; Pele as the primary rendering target and Mermaid.js as the reference compatibility target |
| CLI, then TUI | clap; Ratatui with Crossterm over the same application commands and events |
| Native scheduling and transport | Tokio; reqwest with rustls; bounded CPU/engine workers rather than blocking the async executor |
| Native state and search | rusqlite with bundled SQLite and FTS5; local content-addressed artifact files keyed with BLAKE3 |
| PDF text, geometry, objects, and rendering | PDFium through pdfium-render; separately qualified native and WASM builds |
| DOCX structural extraction | zip and quick-xml, with explicit handling of document relationships, tables, notes, equations, and media |
| Image decoding | Rust image codecs for an explicitly tested format set; preserve orientation and source-to-crop coordinate transforms |
| Local OCR inference | Pinned PaddleOCR ONNX models; ONNX Runtime through ort on native hosts and ONNX Runtime Web in browser hosts |
| Browser integration | wasm-bindgen for the Rust core, thin JavaScript host adapters, workers, and browser persistence |
| Model calls | Provider adapters for text editing and multimodal interpretation, with separate capabilities and budgets |

These are implementation selections to qualify, not claims that the complete integration already works. Pin native libraries, model weights, character dictionaries, preprocessing, decoder settings, and runtime versions together. Record checksums, upstream provenance, and redistribution terms. Development may use Python to export models; deployed native applications do not require Python or Node.

The browser host uses its own storage and networking adapters; native SQLite and filesystem calls do not enter the portable core. The browser package need not be a single WASM file. Its contract is equivalent document behavior with tested model tolerances, not bit-identical floating-point results or native throughput.

### Initial OCR and layout qualification

Start with `PP-OCRv5_mobile_det` and `PP-OCRv5_mobile_rec`, plus separately qualified language-specific recognition packs. This is a portability baseline, not a claim that v5 is the newest or most accurate model. PaddleOCR's browser documentation explicitly demonstrates these models with ONNX assets and a WASM backend. Compare newer models against the baseline on the same held-out documents before replacing it. [PaddleOCR browser deployment](https://www.paddleocr.ai/main/en/version3.x/inference_deployment/cross_platform/browser.html)

Use ONNX Runtime's CPU execution as the required native baseline and its WASM execution as the required browser baseline. Treat GPU execution as an optional optimization after output and operator compatibility tests. Port the specified resizing, normalization, polygon processing, and character decoding carefully; loading the model successfully does not validate an OCR implementation. [ONNX export](https://www.paddleocr.ai/main/en/version3.x/inference_deployment/others/obtaining_onnx_models.html), [ONNX Runtime Web](https://onnxruntime.ai/docs/tutorials/web/), [ort](https://docs.rs/ort/latest/ort/)

Qualify `PP-DocLayout-M` as the first local layout detector alongside native PDF geometry. It identifies candidate text, table, figure, formula, caption, and furniture regions; it does not establish correct reading order or prove completeness. Compare missed-region rates against larger detectors and references, particularly for unfamiliar scripts and layouts. A detector's missing box cannot be interpreted as an empty region. [PaddleOCR layout detection](https://www.paddleocr.ai/main/en/version3.x/module_usage/layout_detection.html)

Use three routes, selected per region:

1. Usable native text and structure: extract directly, then edit prose without image inference.
2. Scanned ordinary text: local detection and recognition, retaining polygons, readings, diagnostics, and language-pack identity. Apply orientation correction or dewarping only when needed.
3. Complex or uncertain content: pass the relevant image crop, labels, and surrounding context to a configured multimodal model. When complexity is known upfront, allow direct vision interpretation rather than first paying for redundant OCR. Preserve disagreements and unresolved readings.

Do not treat recognizer confidence as calibrated correctness. Combine coverage, encoding, layout, and interpretation checks; preserve an explicit route for handwriting, unsupported scripts, and degraded scans. Local OCR works offline after model installation. Vision and editing require either a configured local model endpoint or a remote provider; a CPU-only installation must not imply that a large vision model will be fast locally.

Do not use ocrs as the universal default: its current README describes preview accuracy and Latin-only recognition. Keep alternative engines as measured evaluation baselines rather than bundling several OCR stacks without evidence that they improve the product. [ocrs status and language support](https://github.com/robertknight/ocrs/blob/main/README.md)

## Rust/WASM boundary informed by Cursive

Reviewed Cursive at `2be3524f21c664c0a770959391bc522f9489560a` on 2026-11-06. This was source inspection, not execution of Cursive's test suite. Its `cursive-kernel` is a framework-independent Rust crate built as both `rlib` and `cdylib`; the native application links it directly and the web bridge calls feature-gated wasm-bindgen functions. The kernel owns deterministic document rules while host adapters own I/O. Adopt that separation. [Kernel manifest](https://github.com/nampara-ai/cursive/blob/2be3524f21c664c0a770959391bc522f9489560a/crates/cursive-kernel/Cargo.toml), [kernel contract](https://github.com/nampara-ai/cursive/blob/2be3524f21c664c0a770959391bc522f9489560a/crates/cursive-kernel/src/lib.rs)

Use explicit crate boundaries for the initial Librarian workspace:

| Crate | Owns | Dependencies permitted across this boundary |
| --- | --- | --- |
| `librarian-core` | Source spans, structured content, interpretation/validation rules, dependency keys, deterministic assembly and Markdown rendering | Portable Rust libraries; no filesystem, network, database, process, clock, terminal, or JS APIs |
| `librarian-app` | Commands, pipeline state transitions, requested effects, completion events, retry/cancellation policy | Core and platform-neutral contracts; explicit time/identity inputs from the host |
| `librarian-native` | Files, SQLite, HTTP, timers, worker processes, PDFium/ONNX Runtime adapters | App/core plus native libraries and Tokio |
| `librarian-wasm` | Versioned wasm-bindgen interface to core/app; compact input/output conversion | App/core plus WASM bindings; browser host executes effects through its adapters |
| `librarian-cli` | Terminal arguments, commands, output, and later the TUI | Native host and shared application API |

Dependency direction is inward. Native CLI code calls Rust directly; it does not run an embedded browser or invoke WASM. A browser worker uses the WASM bindings for the same decisions. PDF/OCR execution remains behind the observation contract because those engines have platform-specific builds. Enforce crate dependency rules in CI so an accidental dependency cannot pull native SQLite or a desktop framework into the core. Do not introduce a desktop GUI dependency during terminal development.

### Contracts worth carrying over

- **One implementation, tested across hosts.** Cursive runs native/WASM parity fixtures and also exercises its committed web artifact for ledger operations. Librarian should compare all public deterministic operations on the same fixtures, including source ownership, page joins, table continuation, cancellation, invalid input, and Unicode. Execute the actual browser distribution in a worker as well as testing native and Node-loaded WASM. Use recorded observations/responses for deterministic pipeline tests; evaluate PDF/OCR runtime differences separately with explicit tolerances. [WASM parity](https://github.com/nampara-ai/cursive/blob/2be3524f21c664c0a770959391bc522f9489560a/scripts/kernel-wasm-parity.mjs), [shipped ledger checks](https://github.com/nampara-ai/cursive/blob/2be3524f21c664c0a770959391bc522f9489560a/scripts/kernel-ledger.test.mjs)
- **Compact, revision-bound data transfers.** Cursive's search worker holds one source revision, reuses it for subsequent queries, and transfers typed-array results. Librarian should retain bounded page/block buffers in their owning worker, batch operations, and exchange handles or compact ranges where practical. Keep images as binary buffers, not base64 inside internal JSON. Account for copies into WASM linear memory; typed arrays do not automatically make that transfer zero-copy. Budget caches by bytes and define eviction/release explicitly. [Search worker](https://github.com/nampara-ai/cursive/blob/2be3524f21c664c0a770959391bc522f9489560a/src/components/code/search/search.worker.ts)
- **Explicit coordinate types.** Use distinct UTF-8 byte spans, browser UTF-16 spans, page coordinates, and crop pixels, with tested conversions and transforms. Validate character boundaries and preserve original source bytes. Any Unicode normalization used for search/matching is a derived view with its own mapping. Version the wire schema and avoid native-width integers or JavaScript number conversions that can lose large offsets. Cursive's handling of surrogate pairs and JavaScript whitespace shows why this must be deliberate. [String semantics](https://github.com/nampara-ai/cursive/blob/2be3524f21c664c0a770959391bc522f9489560a/crates/cursive-kernel/src/jsstr.rs)
- **Validate against immutable evidence.** Cursive rejects invalid source-edit ledgers, including overlapping ranges and mismatched old text. Librarian should apply the same principle to accepted block results: require the owned source revision, valid spans, and protected-content checks before publication. A malformed result stays unresolved rather than being silently repaired. This does not require sending verbose patch ledgers for ordinary prose or claim that structural checks prove semantic fidelity. [Ledger verification](https://github.com/nampara-ai/cursive/blob/2be3524f21c664c0a770959391bc522f9489560a/crates/cursive-kernel/src/ledger.rs)
- **Cancellation governs publication.** Tag asynchronous work with workspace, document revision, job, and attempt identities. A late provider response cannot publish after cancellation or supersession even when the remote request could not be stopped. Recheck identity within the publication transaction. A reusable exact-input result may be retained separately without reviving the cancelled run. Cursive checks its stop state after provider completion and between returned tool calls. [Agent cancellation checks](https://github.com/nampara-ai/cursive/blob/2be3524f21c664c0a770959391bc522f9489560a/src-tauri/src/code_agent.rs)
- **Authority precedes projections.** Preserve originals, accepted readings, edits, and run history as authoritative records. Search indexes and exports are rebuildable views. Cursive's journal store derives SQLite rows from authoritative history, bounds cache memory, and periodically compares incremental derivation with full replay. For Librarian, compare cheap deterministic derivations; verification must not silently repeat paid model calls. Use stage-level provenance suited to a batch pipeline rather than importing an editor's entire branching/versioning machinery. [Journal persistence](https://github.com/nampara-ai/cursive/blob/2be3524f21c664c0a770959391bc522f9489560a/src-tauri/src/journal_store.rs)

### Deliberate differences for a fresh implementation

Cursive retains a TypeScript implementation as a startup/failure fallback during its kernel migration. Librarian starts with a single implementation of domain rules. If the shared engine cannot initialize or an operation fails, return an explicit capability/error outcome and preserve resumable work. Qualified alternative OCR engines remain possible through the same evidence contract; a second implementation of core rules is unnecessary. [Cursive loader](https://github.com/nampara-ai/cursive/blob/2be3524f21c664c0a770959391bc522f9489560a/src/kernel/runtime.ts)

Cursive commits generated WASM and maintains a normalized-source manifest for a selected set of Rust files. Its tests cover manifest freshness and selected shipped-artifact behavior, but that manifest is not a complete build-input identity. Librarian should produce the WASM binary, JS glue, and type declarations together in a pinned build, and record the complete relevant source/dependency set, Cargo.lock, features, toolchain/build flags, schema version, and output checksums. Include separate engine/model manifests for PDFium, ONNX Runtime, weights, and decoding data. Test the exact bundle that is distributed; a passing freshly compiled module does not establish the identity of another packaged module. [Artifact generator](https://github.com/nampara-ai/cursive/blob/2be3524f21c664c0a770959391bc522f9489560a/tools/build/kernel-wasm.mjs), [source manifest](https://github.com/nampara-ai/cursive/blob/2be3524f21c664c0a770959391bc522f9489560a/scripts/generate-wasm-manifest.mjs)

Retain the distinction between successful computation, published output, and confirmed durability. Cursive's source writer stages content, checks the destination baseline, uses platform-specific replacement, and reports when publication occurred but durability could not be confirmed. Librarian needs equivalent tested native publication semantics plus a recovery protocol for artifact files and SQLite commits. Atomic rename alone does not make those two stores one transaction. Browser persistence needs its own explicit durability contract. [Source publication](https://github.com/nampara-ai/cursive/blob/2be3524f21c664c0a770959391bc522f9489560a/src-tauri/src/atomic_fs.rs)

## HTML extraction informed by Defuddle

Reviewed Defuddle at `3e8bde9f5f855bc968e95837a750c5968a9fe639`. Source inspection covered extraction, retries, math, images, tables, public response types, and fixture/performance tests; no comparative runtime measurements were made. Use it as a reference and benchmark for the HTML input adapter. Its ordinary extraction is deterministic and does not require an LLM, so an HTML cost comparison must not invent model expenses for that baseline. [Repository](https://github.com/kepano/defuddle/tree/3e8bde9f5f855bc968e95837a750c5968a9fe639)

Borrow its semantic recovery work: recover embedded MathML/LaTeX before considering image interpretation; preserve footnotes and callouts before hidden-element cleanup; resolve lazy-image sources and captions; preserve code language and remove only presentation gutters; combine DOM structure with page metadata. Recognize useful non-executable data inside script elements before discarding script containers. HTML acquisition must not execute page scripts just to extract embedded data. [Math recovery](https://github.com/kepano/defuddle/blob/3e8bde9f5f855bc968e95837a750c5968a9fe639/src/elements/math.base.ts), [extraction order](https://github.com/kepano/defuddle/blob/3e8bde9f5f855bc968e95837a750c5968a9fe639/src/defuddle.ts)

Extend these ideas for a corpus pipeline:

- Preserve immutable input plus snapshot-bound node identities and source locations where available. Record every inclusion, exclusion, and normalization with a rule version and evidence reference. Exclusion changes the export view; it does not erase source evidence. Diagnostic verbosity must not change extraction decisions.
- Separate the user's selection policy from normalization: article body, conversation including replies, or preservation of substantive reference material. Footnotes, captions, and appendices are substantive content. Site selectors and CSS visibility are clues, not sufficient grounds to discard a region. Preserve uncertainty when candidates disagree.
- Capture semantic structure once and reuse candidate features rather than requiring repeated full-document cleanup just to compare candidates. Defuddle's retry logic uses word-count thresholds and cloned-document passes; Librarian should assess source-region coverage, linked notes, structural completeness, and expected content type as well. More words alone do not establish a better extraction. Full DOM parsing still needs an explicit memory/input-size budget; do not claim universal constant-memory HTML parsing.
- Distinguish source metadata from generated summaries, classifications, and image descriptions. Record conflicting metadata candidates and their provenance instead of silently treating an inferred value as authored source text.
- Return explicit complete, needs-review, unsupported, and failed outcomes. Defuddle can return a sanitized whole-body fallback, while caught errors appear in response diagnostics only with debug enabled. Librarian may retain such salvage output but must not silently mark it as an accepted clean extraction. [Response contract](https://github.com/kepano/defuddle/blob/3e8bde9f5f855bc968e95837a750c5968a9fe639/src/types.ts), [fallback tests](https://github.com/kepano/defuddle/blob/3e8bde9f5f855bc968e95837a750c5968a9fe639/tests/degraded-fallback.test.ts)
- Keep fetching separate from parsing. A saved local HTML file uses the local snapshot by default; remote image, transcript, and third-party enrichment follow the selected acquisition policy and become recorded, cached source assets. Defuddle supports injected fetch and async extractors, which is a useful adapter pattern. [Extractor interface](https://github.com/kepano/defuddle/blob/3e8bde9f5f855bc968e95837a750c5968a9fe639/src/extractors/_base.ts)

Defuddle already preserves merged-cell tables as HTML inside Markdown when a pipe table would be inadequate. Librarian should preserve that fidelity while also retaining a typed cell model shared by HTML, DOCX, PDF, and scanned tables. Do not mistake a different output representation for an extraction-quality improvement. [Table rendering](https://github.com/kepano/defuddle/blob/3e8bde9f5f855bc968e95837a750c5968a9fe639/src/markdown.ts)

Adopt its minimal regression-fixture discipline and compare the HTML adapter on equivalent inputs, with network disabled for offline evaluation. Add independent expected region/relationship annotations, held-out sites, long inputs, and cross-format versions of the same document. Missing expected results must fail CI; baseline creation is a separate reviewed action. Test harmless wrapper/class changes, hidden footnotes, intentional repetition, code, math, merged tables, and optional replies. Report omissions, incorrect exclusions, relation preservation, latency, memory, and review burden. A tenfold improvement requires a named metric and measured baseline. Keep Defuddle as development/reference tooling unless a production host integration earns its runtime cost. [Fixture suite](https://github.com/kepano/defuddle/blob/3e8bde9f5f855bc968e95837a750c5968a9fe639/tests/fixtures.test.ts), [performance measurement](https://github.com/kepano/defuddle/blob/3e8bde9f5f855bc968e95837a750c5968a9fe639/tests/perf.test.ts)

## Central design

Represent a document explicitly before asking a model to edit it. Physical pages, logical content, and inference requests are three different units. Their boundaries must be independent.

The pipeline is an incremental build graph:

```text
Original bytes
    -> page or structural observations
    -> reading order and source-linked logical blocks
    -> bounded editing jobs with explicit source ownership
    -> accepted text and unresolved passages
    -> classification, search index, and configurable Markdown rendering
```

Persist each stage's inputs, results, and dependencies. A changed export template rebuilds rendering. A failed page retries extraction for that page and work that depends on it. Neither operation automatically reruns an entire document.

The original Desktop prototype has a useful direct workflow: clean, classify, name, save. Preserve that clarity. Its continuation routine, however, asks for more text using only the generated tail, without the remaining source. Its final deduplication can also remove intentional repetition. Every new generation must be grounded in identified source material; deterministic assembly must not delete repeated prose merely because it looks duplicated.

## Source evidence and logical content

Use a small document representation shared by all input adapters:

| Record | Contents |
| --- | --- |
| Source asset | Original bytes or immutable local copy, digest, detected media type, revision |
| Observation | Page or structural location, region geometry when available, observed text or visual content, extraction method, engine version, diagnostics |
| Logical block | Stable occurrence identity, ordered source spans, role, chosen reading, unresolved alternatives |
| Boundary decision | Continue paragraph, start paragraph, continue table, attach caption or footnote, retain or remove line-wrap hyphen; supporting observations |
| Editing job | Owned blocks or spans, read-only context, frozen style context, request budget, exact dependency identities |
| Result | Candidate text, source mapping, method and model identity, validation findings, completion state |

Keep raw observations immutable. OCR interpretation and copyediting are separate transformations: a corrected OCR reading must not overwrite the evidence from which it was inferred. A numeric OCR correction is an interpretation decision; later copyediting must preserve the accepted value. The model's self-reported confidence is not sufficient evidence to accept a reading.

Every source region receives an explicit disposition: included, represented by an interpretation, excluded as identified page furniture, or unresolved. Each logical source span has one output owner. Neighboring jobs may read it as context but cannot publish another copy. This proves structural accounting, not semantic fidelity; model output still needs quality evaluation.

Content identities and processing cache keys are separate. A revised PDF has a new asset digest, but unchanged page observations can still reuse computational results when their actual inputs and context match. Repeated occurrences remain distinct even when their text is identical.

## Page boundaries and reading order

Consider a PDF with these observations:

```text
Page 12, body: The results indicate that the
Page 12, footer: 12
Page 13, running header: FIELD NOTES
Page 13, body: intervention improved retention.
```

After resolving the boundary, the logical paragraph is:

```text
The results indicate that the intervention improved retention.
```

It retains source spans on pages 12 and 13. The page transition is metadata inside the paragraph. The editor receives the paragraph as one unit when it fits the request budget. Optional page labels are added by the renderer after editing.

Use geometry, font/layout signals, structural tags, and neighboring text for ordinary reading-order and boundary decisions. Confirm repeated headers using position and repetition across pages. Do not remove repeated sentences throughout a document. For ambiguous joins, request a small, source-grounded decision using adjacent regions, with visual crops when needed. Preserve an unresolved boundary when the evidence is insufficient.

Tables, footnotes, captions, and body text have explicit relationships. A table continuing onto the next page stays a table, while a footnote does not become the middle of a body sentence. DOCX, transcripts, and text files use their structural locations directly; they do not need artificial pagination.

Operate with a rolling page window and bounded lookahead. Persist unfinished paragraphs and table continuations on disk. An unusually long paragraph may span multiple bounded jobs with exact span ownership and small neighboring context. Never require an entire book to fit in memory or a model context window.

Freeze the source interpretation and relevant glossary/style context before editing a region. Neighboring editing jobs read the same interpretation snapshot. They do not depend cyclically on one another's generated drafts. A later source correction creates a new revision and invalidates only the dependencies it actually changes.

## Extraction and selective vision

Every page is inspected locally. Inference is selected at the region level where possible:

| Input or region | Initial route |
| --- | --- |
| Plain text and transcripts | Encoding and structure parsing; text editing |
| HTML and saved web pages | Structural extraction, semantic recovery, content selection, and linked asset handling |
| DOCX | Structural XML extraction of paragraphs, tables, notes, and embedded media |
| PDF with usable text | Native text and layout extraction; targeted interpretation for ambiguous regions |
| Scan or image | OCR or a qualified vision transcription route, selected by measured quality and cost |
| Chart, diagram, complex table, or equation | Visual interpretation of the relevant crop and necessary surrounding context |

A nonempty text layer is not proof of a good extraction. Check for suspicious encodings, coverage gaps, broken reading order, missing regions, and disagreement with inexpensive visual/layout signals. Sampling can detect systematic problems but cannot certify every page. Record what was examined and escalate suspect regions.

Choose the cheapest route that meets the tested quality requirement. Do not automatically pay for OCR, full-page vision, and a second transcription on every page. Crop carefully: preserve labels, legends, surrounding units, and resolution sufficient to read the evidence. Large tables may need tiled views and a shared coordinate frame.

Vision returns source-linked readings and structured content. Text from the native parser and text from vision are alternatives or complementary regions, not two strings to concatenate blindly. Keep table cells and relationships structured internally. Render them as Markdown tables or faithful embedded HTML tables when necessary. Render equations as mathematical notation, and label inferred figure descriptions so they are distinguishable from source quotations. Preserve existing Mermaid source and generate supported diagram types from accepted structure as part of Markdown export.

Unknown formats, encrypted documents requiring a password, and unreadable regions produce explicit outcomes. A universal front door should identify and report every input reliably; unsupported content must not masquerade as successful empty extraction.

### Complex material stays structured

The internal representation is richer than the output file. Model responses must satisfy a versioned typed schema with valid source references. Syntactic validity alone does not establish faithful interpretation.

| Material | Internal evidence and structure | Markdown rendering |
| --- | --- | --- |
| Table | Cells with row/column coordinates, merged spans, header relationships, units, caption, notes, and source boxes | GFM pipe table for simple grids; minimal HTML table within the Markdown file for merged or complex cells |
| Equation | Original textual/structured notation when available, accepted transcription, source crop, symbol relationships, unresolved symbols | Inline or display TeX math, preserving grouping and uncertainty |
| Figure or photograph | Original asset/crop, caption, observed text, and a separate generated description | Relative image link, useful alt text, caption, and optional labeled description |
| Plot | Axes, units, legend, series, explicitly labeled values, and separately identified estimates | Source image with supported data table and description; estimates remain labeled |
| Flowchart | Nodes, directed edges, edge labels, grouping, and source evidence | Standard Mermaid fenced block for an accepted supported graph, with a concise textual description |
| Other diagram | Diagram kind and appropriate relationships, such as sequence participants/messages; retain original when unsupported | Supported Mermaid syntax or original image with a faithful description and review status |
| Existing Mermaid/code | Original source, language/type where known, and validation status | Language-tagged fenced block with literal contents protected from copyediting |

Read DOCX table grids and native equation structures directly when supported. For PDFs, combine text coordinates and vector rules with visual interpretation where necessary. For scanned tables, infer the grid as well as the cell text. Check for overlapping spans, orphaned cells, lost units, and unmatched source regions. These checks detect problems without claiming to prove every value correct.

Join tables across pages only with evidence such as compatible columns, header roles, continuation labels, and surrounding structure. Retain repeated-header observations even when the renderer displays the header once. Large tables use bounded row groups and a shared schema; they cannot require an entire table in one model response.

Preserve equation structure and exact values through the prose-editing stage. If a minus sign, exponent, subscript, or symbol cannot be read reliably, retain the uncertainty rather than inventing a plausible formula. Chart values inferred from pixel positions are estimates and must not become claimed exact source data.

For a raster diagram, recover its supported relationships before producing any diagram language. Do not ask the model to generate Mermaid freely and treat parseable syntax as evidence of fidelity. A deterministic Mermaid serializer emits supported diagram types from accepted structure; unsupported types retain their source and description. Preserve existing Mermaid source in fenced blocks and record its validation status. Pele and Mermaid parser success establishes syntax compatibility, not correspondence with the source image. [Mermaid parse API](https://mermaid.js.org/config/setup/mermaid/interfaces/Mermaid.html#parse)

## Editing and assembly

Pack adjacent logical blocks into a request sized for the provider's actual input and output budgets. Include a bounded amount of immutable neighboring text and relevant terminology. Reserve output and reasoning headroom where the provider requires it. Character counts are estimates, not token budgets.

The normal prose path should need one editing generation per packed job. Return text for locally assigned block identifiers; avoid a verbose patch language for routine prose. Small corrections can use patches when measured to reduce total tokens and failure rates. Code, equations, table values, quotations, and other protected material receive policies appropriate to their roles.

Use deterministic checks for complete block accounting, malformed responses, truncation, obvious content loss, altered protected values, and context duplication. Word retention and structural checks are evidence, not proofs that meaning survived. Legitimate filler removal must not trigger indefinite repair loops.

Retry or subdivide only the failed owned region, with the original source available. Never ask a model to invent a continuation from the generated tail. Model review is a measured escalation for risky cases and a component of offline evaluation, not an automatic second and third generation for every paragraph.

Assemble accepted blocks in the resolved logical order. Render separators from boundary decisions. A whole-document generative smoothing pass is unnecessary for ordinary assembly and risks reintroducing omissions.

Track content and execution separately: a job can finish while some passages still need review. CLI exit policy, machine-readable reports, TUI status, and exported diagnostics must consistently distinguish complete output, output needing review, failed execution, and cancellation. A body-only Markdown export may omit annotations by user choice, but the run still retains and reports unresolved work.

## Caching and incremental reuse

Implement three distinct layers:

1. **Local artifacts:** source observations, renderings, crops, OCR readings, logical blocks, metadata, and index data.
2. **Local inference results:** completed responses and accepted results for exact processing inputs. A valid hit avoids generation entirely.
3. **Provider prompt caches:** reusable prefix processing for requests that still need new generation. This reduces some input work; it does not cache the answer.

Build keys from canonical, versioned processing inputs: stage and implementation version, relevant content and geometry, neighboring context, prompt/schema/style versions, provider endpoint and account namespace, model identity, and all generation-affecting options. Include response model/fingerprint information when available. Mutable model aliases need a defined refresh policy. Never use filenames, timestamps, or an entire application's version as a substitute for the actual dependency set.

Keep output presentation out of editing keys. Include the classifier's taxonomy version only in classification dependencies. Keep local credentials out of persisted keys and logs. Coalesce identical concurrent requests within the same workspace so multiple jobs do not pay for the same work simultaneously.

| User action | Work to repeat |
| --- | --- |
| Rerun unchanged source and processing profile | Reuse completed artifacts and accepted results; no new inference |
| Change filename template, page labels, or metadata layout | Rendering only, when the requested metadata already exists |
| Add a previously ungenerated summary | Summary generation and rendering |
| Change classification taxonomy | Classification and affected index/export metadata |
| Correct one page's interpretation | That page's logical dependencies and affected editing jobs |
| Change editorial style | Affected editing and downstream stages; preserve extraction |
| Resume after interruption | Unfinished or invalidated jobs; retain committed work |

Store failed attempts for diagnosis and retry accounting, separately from reusable successful results. Unreadable-page output cannot populate a successful extraction cache. A fresh evaluation mode must bypass local inference-result reuse, and its prompt-cache conditions must be recorded.

### Provider prompt layout

Use deterministic request serialization and place stable material before changing material:

```text
Stable task instructions and output contract
Stable CMOS profile and relevant examples
    [reusable prefix boundary, where supported]
Frozen document or section terminology, when useful across jobs
    [optional reusable prefix boundary]
Read-only neighboring source context
Owned source blocks or visual regions for this request
```

Keep changing run identifiers, clocks, progress counters, and retry explanations out of the shared prefix. Prefix reuse does not justify sending the whole book to every request or padding a short prompt with useless tokens. Long cached context still consumes context capacity and may incur input charges and rate-limit usage.

Implement caching controls per provider and model generation. OpenAI currently distinguishes older implicit caching from newer explicit breakpoint controls; stable text alone does not guarantee a useful cache boundary. Claude exposes `cache_control`, with creation/read accounting and selectable lifetimes. These capabilities belong in adapters, not a universal assumption about OpenAI-compatible endpoints. See [OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching) and [Claude prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).

Schedule nearby jobs that share a useful prefix within its reuse window, while preserving fairness and responsiveness. Where cache visibility requires the first response to begin, use the first real job to establish the prefix before a large fan-out when the latency tradeoff is worthwhile. Prewarming is optional and must earn its extra request or write cost.

For a reusable prefix of B tokens, uncached rate u, write rate w, read rate r, additional reuse count N, and storage cost S, caching pays when:

```text
N * B * (u - r) > B * (w - u) + S
```

Use the provider's actual billing semantics when applying this comparison. Unknown prices remain unknown rather than being reported as zero. Measure cache reads, cache writes, uncached input, output/reasoning usage, elapsed time, and total document cost. Optimize cost per satisfactorily processed document rather than cache-hit percentage alone.

## Scheduling and durability

Use a bounded, persistent job graph with separate resource limits for parsing/rendering, OCR, text inference, vision inference, and storage writes. Control both requests and tokens per provider; many small files must not multiply concurrency beyond a single account budget. Adapt concurrency to observed throughput and throttling. Apply backpressure before queued page images consume memory.

Persist completion at useful page/block boundaries. Commit accepted results transactionally, publish output atomically, and use recoverable leases for interrupted work. Rendering and indexing must be repeatable. A network timeout after a provider accepted a request can still cause duplicate external work; track request identities and retrieve existing responses where supported instead of promising exactly-once billing.

Retries share one bounded policy across layers. Honor provider delays, add jitter, distinguish transient failures from invalid credentials or exhausted credits, and enforce per-job attempt, elapsed-time, and spending limits. A stronger model is an explicit escalation with a budget, not an unbounded fallback chain. Choose interactive scheduling or deferred batch execution according to the user's latency preference and provider capabilities.

## Language and platform boundary

Use Rust for the shared document model, planning, validation, cache dependency calculation, and Markdown rendering. Keep filesystem access, HTTP transport, process management, and persistence behind a small host interface. Native binaries and WASM hosts execute the same core rules.

Use established parsing engines where they outperform practical alternatives. PDFium is the initial PDF candidate: it exposes text, layout, and page rendering, and Rust bindings document native and WASM integration. It is not thread-safe; use serialized access per instance/process and bounded native worker processes where parallelism or crash isolation is needed. Verify that process startup and IPC costs are justified. [PDFium Rust bindings](https://docs.rs/pdfium-render/latest/pdfium_render/)

WASM requires real host adapters. Browser filesystem, networking, persistence, and worker execution differ from native operating systems; merely compiling code that calls native APIs is insufficient. Browser PDF rendering may use a PDFium WASM build or another qualified adapter. Keep parser behavior behind the same observation contract and test parity with fixtures. [Rust WASM target limitations](https://doc.rust-lang.org/rustc/platform-support/wasm32-unknown-unknown.html)

The initial native components are a CLI, an application runtime, bundled SQLite with FTS5, and qualified parser/provider adapters. Add a Ratatui TUI over the same commands and event stream once the CLI workflow is reliable. No HTTP server is required for a local terminal application. [SQLite FTS5](https://www.sqlite.org/fts5.html), [Ratatui](https://ratatui.rs/)

Ship one coordinated version with four native distributions:

| Distribution | Initial target |
| --- | --- |
| macOS Apple Silicon | aarch64-apple-darwin |
| macOS Intel | x86_64-apple-darwin |
| Windows | x86_64-pc-windows-msvc |
| Linux | x86_64-unknown-linux-gnu, with an explicit tested system-library baseline |

Windows and Linux x86-64 are the initial architecture assumption. The shared WASM package is an additional library build, not another desktop product. WASI support can be added through a separate host when a concrete consumer needs it.

Bundle required parsing libraries and runtime data. End users should not need Python, Node, Homebrew, or development tools. Linux compatibility must be tested with the shipped artifact on a clean Arch/Omarchy environment and representative Debian/Ubuntu and Fedora environments. Packaging native PDF libraries may set a higher compatibility floor than Rust itself. A musl executable alone does not make a glibc-linked parser portable.

Build and execute acceptance cases on each native architecture, including Intel macOS. Run WASM fixtures in an actual host. Compile checks and emulation are useful but do not replace native release acceptance. Test final signed/packaged artifacts, then tag the validated commit; avoid introducing untested code between qualification and publication.

## Markdown output and organization

Markdown is the sole document output format from the first release. Retain structure throughout extraction and editing; serialize accepted blocks into `.md` files directly. A plain-text intermediate must not discard headings, emphasis, lists, notes, table cells, links, code, or math that the renderer would then have to guess back into existence.

Define a versioned output profile based on CommonMark with GFM tables, task lists, and strikethrough, plus explicitly supported footnotes, TeX math, Mermaid fences, and optional YAML frontmatter. Extensions are not uniformly supported by every Markdown viewer. Use `pulldown-cmark` with selected extension flags for parsing and structural checks; keep automatic smart-punctuation conversion disabled there because editorial policy owns punctuation. [GFM specification](https://github.github.com/gfm/), [parser extensions](https://docs.rs/pulldown-cmark/latest/pulldown_cmark/struct.Options.html)

The default style uses a coherent heading hierarchy, comfortable blank-line spacing, consistent lists, readable pipe tables, language-tagged code fences, stable footnote references, useful link text, and captions attached to their figures. Join PDF line wraps into paragraphs. Preserve meaningful line breaks in poetry, code, and other structured material. Do not invent headings or rewrite code to make the document appear polished. Formatting must be deterministic and must not require an additional model pass.

Choose fences and escaping according to content: embedded backticks must not terminate code, literal pipes must not create cells, and currency must not become accidental math. Validate internal references, footnote targets, links, asset paths, and block structure by reparsing the generated Markdown. For complex tables, validate the embedded table structure separately. Round-trip checks establish structural preservation; they do not prove semantic fidelity or visual quality. Qualify representative rendered output as well as raw Markdown readability.

Preserve merged table cells using minimal generated HTML inside the `.md` document when pipe syntax cannot express them. A compatibility profile may instead expand header paths into ordinary pipe-table columns when that transformation preserves the relationships. Never silently flatten a table into unrelated text to avoid an output limitation. Original source evidence remains available for review.

Retained figures use relative links to local assets with stable names and hashes, keeping the document and its asset directory portable and usable offline. Publish required assets before the Markdown file that references them and make interruption recovery explicit. All output documents remain `.md`; images are supporting assets, not alternate document exports. Alt text, source captions, and generated descriptions retain distinct provenance.

### Pele and Mermaid compatibility

Pele is the primary diagram renderer and visual qualification target. Export standard `mermaid` fenced blocks so other Mermaid-capable viewers can render the same source. Keep fonts, colors, and CSS-variable theming in the rendering host, outside document semantics. The native terminal application produces Markdown without requiring a browser or a user-installed JavaScript runtime. Use Pele and Mermaid.js in the development compatibility harness; a future graphical host can use Pele without changing stored documents. [Pele API](https://pele.run/api)

Pele documents support for Mermaid's built-in diagram types, with different layout behavior and limits on features such as arbitrary HTML labels and external plugins. Target the tested intersection of Pele and pinned Mermaid.js versions; do not promise all Mermaid libraries or identical layouts. Generated diagrams default to established syntax with stable ASCII node identifiers, correctly escaped text labels, meaningful relationships, and no required custom icons, JavaScript callbacks, beta-only syntax, or renderer-specific theme directives. Preserve unsupported imported source and report its compatibility status. [Pele compatibility](https://pele.run/compatibility)

Test both parsers/renderers on each supported generated diagram type, plus representative imported diagrams, Unicode labels, long labels, narrow layouts, and size limits. Compare graph or diagram relationships where parser models permit and inspect rendered results for clipping or misleading layout. A diagram that merely renders is not automatically faithful. Math in diagram labels requires a configured typesetter in Pele; prefer readable plain labels with separate Markdown equations in the portable default profile. Scope initial generated types to those we can qualify, starting with flowcharts; retain source images and descriptions for other material until its serializer passes acceptance.

### Metadata and naming

Keep configuration in named, validated profiles. Separate interpretation and editorial settings from presentation settings. Start with understandable choices for body-only output, page labels, title, summary, tags, source information, classification, numbering, and filename templates. Store metadata independently so changing its presentation does not require re-editing prose.

Treat class codes as strings and distinguish taxonomy from accession numbering. For example, a Dewey-style class `636.1` and a local sequence `19` are separate fields; the filename may display both. Support a versioned user taxonomy and overrides. Estimate document-level classification from representative content across the document, and retain evidence locations. Summary text is metadata, never a replacement for source content in the cleaning pipeline.

Serialize optional frontmatter with typed, escaped values; class codes and accession identifiers remain strings. Distinguish source descriptions from generated summaries. Exact original page positions cannot always be transferred to rewritten wording; optional page markers belong at safe block boundaries and must describe their placement policy. Internal source mappings retain transitions within paragraphs without inserting artificial sentence breaks.

## Acceptance and implementation order

The first implementation should prove the difficult boundary and reuse behavior in a small vertical slice:

Before accepting the native engine choices, run a dependency qualification experiment: load the exact PDFium and ONNX Runtime artifacts, extract/render a PDF, and run the pinned OCR models on the same fixtures across the four native targets and an actual browser WASM host. Include the layout-model export and its postprocessing. Measure cold start, peak memory, throughput, and recognition differences. A successful Rust compile is insufficient. Keep model and parser adapters replaceable if this experiment exposes unacceptable distribution or quality costs.

1. Create the shared core and native/WASM build harness. Test source-span ownership, Unicode boundaries, and deterministic Markdown serialization with synthetic fixtures. Prove a paragraph can cross pages without duplicate or missing ownership. Establish the output dialect and Pele/Mermaid compatibility fixtures immediately.
2. Implement text and Markdown ingestion, a bounded provider adapter, durable jobs, Markdown export, and local inference-result reuse. Demonstrate interruption/resume and a zero-inference repeat run.
3. Add PDF text/layout extraction and region-level vision through the observation contract. Test scans, mixed pages, multiple columns, split paragraphs, tables, equations, footnotes, headers, and unreadable pages. Qualify the native and WASM parser packaging before expanding dependencies.
4. Add DOCX, image, and HTML adapters, document classification, metadata profiles, and full-text indexing. Prove formatting changes do not invalidate cleaning. Qualify complex tables, math, footnotes, assets, and supported diagram serializers before advertising their support.
5. Add the terminal dashboard over the working engine. Test cancellation, pause/resume, progress, review status, costs, and cache explanations from the real event stream.
6. Qualify all four native packages, the WASM library, and their generated Markdown artifacts. GUI work begins after terminal acceptance.

Keep a frozen evaluation set separate from development examples. Include real licensed/public documents, consented private local fixtures, and synthetic edge cases. Use public extraction benchmarks such as [olmOCR-Bench](https://github.com/allenai/olmocr) alongside independent editorial assessment and blind human checks. Extraction performance cannot substitute for editorial quality.

Measure completeness, reading order, table relationships, factual and quotation fidelity, style improvement, unresolved work, end-to-end latency, memory, provider calls, and total cost. Report cold runs, local-result reuse, provider-prefix reuse, and interruption recovery separately. Include very long documents and use storage-backed stress fixtures to verify bounded memory.

Release invariants include no unexplained dropped source regions, no duplicate ownership, no false complete status, no regenerated committed work on an unchanged resume, and zero inference for presentation-only changes when metadata is already available. Human or reference-based evaluation establishes whether the accounted-for text is actually good.

To substantiate leadership, compare quality at equal cost and speed at equal quality against strong current baselines on the same held-out documents. Architecture and language choice create the opportunity; reproducible results establish the claim.
