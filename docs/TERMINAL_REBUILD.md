# Terminal rebuild architecture

Librarian is a local-first corpus cleaner and organizer. It reads documents, extracts their contents, copyedits prose to a configurable CMOS style, classifies documents using a Dewey-style taxonomy, and indexes the collection for full-text search. The first product delivers configurable UTF-8 plain text through a CLI and TUI. Markdown exports and graphical applications follow terminal acceptance.

This is the design baseline for the rebuild, not a description of implemented capabilities. Rust is the recommended shared implementation language; engine choices remain subject to the native and WASM portability experiment below. Optimize measured document quality, elapsed time, memory, and total cost together.

## Central design

Represent a document explicitly before asking a model to edit it. Physical pages, logical content, and inference requests are three different units. Their boundaries must be independent.

The pipeline is an incremental build graph:

```text
Original bytes
    -> page or structural observations
    -> reading order and source-linked logical blocks
    -> bounded editing jobs with explicit source ownership
    -> accepted text and unresolved passages
    -> classification, search index, and configurable text rendering
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
| DOCX | Structural XML extraction of paragraphs, tables, notes, and embedded media |
| PDF with usable text | Native text and layout extraction; targeted interpretation for ambiguous regions |
| Scan or image | OCR or a qualified vision transcription route, selected by measured quality and cost |
| Chart, diagram, complex table, or equation | Visual interpretation of the relevant crop and necessary surrounding context |

A nonempty text layer is not proof of a good extraction. Check for suspicious encodings, coverage gaps, broken reading order, missing regions, and disagreement with inexpensive visual/layout signals. Sampling can detect systematic problems but cannot certify every page. Record what was examined and escalate suspect regions.

Choose the cheapest route that meets the tested quality requirement. Do not automatically pay for OCR, full-page vision, and a second transcription on every page. Crop carefully: preserve labels, legends, surrounding units, and resolution sufficient to read the evidence. Large tables may need tiled views and a shared coordinate frame.

Vision returns source-linked readings and structured content. Text from the native parser and text from vision are alternatives or complementary regions, not two strings to concatenate blindly. Keep table cells and relationships structured internally. Render them as readable aligned text or row records. Render equations as textual notation, and label inferred figure descriptions so they are distinguishable from source quotations. Mermaid and Markdown rendering are later output capabilities.

Unknown formats, encrypted documents requiring a password, and unreadable regions produce explicit outcomes. A universal front door should identify and report every input reliably; unsupported content must not masquerade as successful empty extraction.

## Editing and assembly

Pack adjacent logical blocks into a request sized for the provider's actual input and output budgets. Include a bounded amount of immutable neighboring text and relevant terminology. Reserve output and reasoning headroom where the provider requires it. Character counts are estimates, not token budgets.

The normal prose path should need one editing generation per packed job. Return text for locally assigned block identifiers; avoid a verbose patch language for routine prose. Small corrections can use patches when measured to reduce total tokens and failure rates. Code, equations, table values, quotations, and other protected material receive policies appropriate to their roles.

Use deterministic checks for complete block accounting, malformed responses, truncation, obvious content loss, altered protected values, and context duplication. Word retention and structural checks are evidence, not proofs that meaning survived. Legitimate filler removal must not trigger indefinite repair loops.

Retry or subdivide only the failed owned region, with the original source available. Never ask a model to invent a continuation from the generated tail. Model review is a measured escalation for risky cases and a component of offline evaluation, not an automatic second and third generation for every paragraph.

Assemble accepted blocks in the resolved logical order. Render separators from boundary decisions. A whole-document generative smoothing pass is unnecessary for ordinary assembly and risks reintroducing omissions.

Track content and execution separately: a job can finish while some passages still need review. CLI exit policy, JSON reports, TUI status, and exported diagnostics must consistently distinguish complete output, output needing review, failed execution, and cancellation. A plain-text-only export may omit annotations by user choice, but the run still retains and reports unresolved work.

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

Use Rust for the shared document model, planning, validation, cache dependency calculation, and text rendering. Keep filesystem access, HTTP transport, process management, and persistence behind a small host interface. Native binaries and WASM hosts execute the same core rules.

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

## Configurable plain text and organization

Keep configuration in named, validated profiles. Separate interpretation and editorial settings from presentation settings. Start with understandable choices for body-only output, page labels, title, summary, tags, source information, classification, numbering, and filename templates. Store metadata independently so changing its presentation does not require re-editing prose.

Treat class codes as strings and distinguish taxonomy from accession numbering. For example, a Dewey-style class `636.1` and a local sequence `19` are separate fields; the filename may display both. Support a versioned user taxonomy and overrides. Estimate document-level classification from representative content across the document, and retain evidence locations. Summary text is metadata, never a replacement for source content in the cleaning pipeline.

Preserve table relationships in a plain-text rendering and provide a clear textual representation of equations and figures. Exact original page positions cannot always be transferred to rewritten wording; page labels must describe their placement policy and the internal source mapping must remain available.

## Acceptance and implementation order

The first implementation should prove the difficult boundary and reuse behavior in a small vertical slice:

1. Create the shared core and native/WASM build harness. Test source-span ownership, Unicode boundaries, and deterministic assembly with synthetic fixtures. Prove a paragraph can cross pages without duplicate or missing ownership.
2. Implement text ingestion, a bounded provider adapter, durable jobs, plain-text export, and local inference-result reuse. Demonstrate interruption/resume and a zero-inference repeat run.
3. Add PDF text/layout extraction and region-level vision through the observation contract. Test scans, mixed pages, multiple columns, split paragraphs, tables, equations, footnotes, headers, and unreadable pages. Qualify the native and WASM parser packaging before expanding dependencies.
4. Add DOCX and image adapters, document classification, metadata profiles, and full-text indexing. Prove formatting changes do not invalidate cleaning.
5. Add the terminal dashboard over the working engine. Test cancellation, pause/resume, progress, review status, costs, and cache explanations from the real event stream.
6. Qualify all four native packages and the WASM library. GUI and Markdown work begins after terminal acceptance.

Keep a frozen evaluation set separate from development examples. Include real licensed/public documents, consented private local fixtures, and synthetic edge cases. Use public extraction benchmarks such as [olmOCR-Bench](https://github.com/allenai/olmocr) alongside independent editorial assessment and blind human checks. Extraction performance cannot substitute for editorial quality.

Measure completeness, reading order, table relationships, factual and quotation fidelity, style improvement, unresolved work, end-to-end latency, memory, provider calls, and total cost. Report cold runs, local-result reuse, provider-prefix reuse, and interruption recovery separately. Include very long documents and use storage-backed stress fixtures to verify bounded memory.

Release invariants include no unexplained dropped source regions, no duplicate ownership, no false complete status, no regenerated committed work on an unchanged resume, and zero inference for presentation-only changes when metadata is already available. Human or reference-based evaluation establishes whether the accounted-for text is actually good.

To substantiate leadership, compare quality at equal cost and speed at equal quality against strong current baselines on the same held-out documents. Architecture and language choice create the opportunity; reproducible results establish the claim.
