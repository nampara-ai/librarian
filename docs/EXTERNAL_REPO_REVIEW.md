# External Repository Capability Review

Reviewed local repositories:

- `/Users/jakelundahl/NAMPARA/Dolphin-master`
- `/Users/jakelundahl/NAMPARA/olmocr-main`
- `/Users/jakelundahl/NAMPARA/longcut-main`

This review identifies capabilities that can upgrade Librarian while preserving its local-first,
hexagonal Python architecture. It is intentionally an implementation strategy document, not a code
port.

## Executive Summary

The strongest near-term upgrade path is to adapt ideas from `olmocr-main`, especially its page OCR
pipeline, anchor-text prompting, retry/rotation loop, PDF filtering, table-aware evaluation, and
benchmark taxonomy. The code is Apache-2.0, aligns with Librarian's document-processing domain, and
maps cleanly onto existing ports such as `TextExtractor`, `LLMProvider`, corpus eval, and the
durable page manifest.

`Dolphin-master` is useful as an architectural pattern for layout-first document parsing and
element-wise decoding, but direct code copying should be avoided unless licensing is clarified. Its
checked-in `LICENSE` is a Qwen Research license with non-commercial restrictions even though the
README badges MIT. Treat Dolphin as a research reference and design a clean-room optional
`VisualDocumentParser` adapter if a vision model backend is added.

`longcut-main` contains several product capabilities relevant to transcript-heavy libraries:
timestamped quote grounding, transcript chunking, citation-aware chat, notes, exports, usage
limits, and provider adapters. It is AGPL-3.0, so do not copy code into Librarian unless the project
intentionally accepts AGPL obligations. Use it as product inspiration and reimplement selected
capabilities against Librarian's existing transcript utilities and FastAPI/CLI surfaces.

## Dolphin-Master

### Useful Capabilities

- Layout-first parsing. `demo_page.py` runs a two-stage flow: first ask the VLM to parse reading
  order and layout, then crop detected elements for type-specific parsing.
- Element-specific prompts. Tables, formulas, code, text, and figures are parsed with different
  prompts, which reduces one-size-fits-all OCR failures.
- Parallel element decoding. Elements of the same type are batched before inference, creating a
  practical throughput model for document images with many regions.
- Reading-order preservation. Parsed elements carry `reading_order` and are sorted before Markdown
  rendering.
- Structured Markdown rendering. `utils/markdown_utils.py` converts section labels, figures,
  tables, formulas, lists, code, and distorted-page fallbacks into Markdown.
- Layout visualization. `utils/utils.py` can render bounding boxes and labels for debugging.

### Porting Strategy

Build a new optional application port rather than folding Dolphin behavior into the existing
Tesseract extractor:

```text
VisualDocumentParser
  parse_page(image) -> VisualPageParse

VisualPageParse
  elements: [bbox, label, reading_order, text, confidence?, model_metadata]
  markdown: str
  diagnostics: layout image path?, fallback reason?, model/provider?
```

Implementation phases:

1. Add domain-neutral visual parse models and a provider-facing port.
2. Add a clean-room parser adapter that can call an OpenAI-compatible vision model, local vLLM, or
   a future Dolphin-compatible model endpoint without importing Dolphin code.
3. Store layout element diagnostics in PDF page manifests alongside current `embedded` and `ocr`
   page records.
4. Use this parser only when explicitly configured, when Tesseract confidence is low, or when page
   diagnostics suggest tables/formulas/multi-column layout.
5. Add corpus-eval expectations for element reading order, table presence, formula formatting, and
   layout fallback counts.

### Integration Priority

Medium. The capability is powerful, but the licensing ambiguity and GPU/model requirements make it
best as a future optional adapter. The safest immediate reuse is the design pattern: separate layout
detection, typed element parsing, reading-order assembly, and visualization diagnostics.

## Olmocr-Main

### Useful Capabilities

- Page-level OCR pipeline. `olmocr/pipeline.py` renders each PDF page, sends it to a VLM endpoint,
  parses structured page metadata, retries failures, corrects rotation, and falls back to embedded
  `pdftotext` output when needed.
- Anchor text generation. `olmocr/prompts/anchor.py` can derive page anchors from `pdftotext`,
  PDFium, PyPDF, a coherency scorer, or a coordinate-rich PDF report. The PDF-report path is
  especially relevant because it linearizes text and image positions for model prompts.
- Rotation feedback loop. Page responses include `is_rotation_valid` and `rotation_correction`,
  letting the pipeline retry with cumulative rotation.
- Adaptive retry behavior. Temperature increases over attempts, connection errors use exponential
  backoff, and remaining attempts can run in parallel when the inference queue is empty.
- Work queue abstraction. `olmocr/work_queue.py` provides a simple local/S3 queue with lock and done
  flags for large batch jobs. Librarian already has SQLite queues, but the lock/done-file pattern is
  a useful object-store backend reference.
- PDF filtering. `olmocr/filter/filter.py` filters forms, obvious download spam, and unsupported
  languages before spending OCR budget.
- Repeat detection. `olmocr/repeatdetect.py` identifies repeated tail n-grams, useful for catching
  hallucinated or degenerate model output.
- Table evaluation. `olmocr/bench/table_parsing.py` builds graph-like table relations from HTML or
  Markdown tables and can validate row/column/header relationships.
- Rich benchmark taxonomy. `olmocr/bench/tests.py` supports presence, absence, order, table, math,
  formatting, footnote, and baseline tests with normalization and fuzzy matching.

### Porting Strategy

Favor direct adaptation where license-compatible and where code can be reshaped into Librarian's
style.

High-value near-term work:

1. Extend `PdfPageExtraction` metadata with optional orientation, table/diagram flags, fallback
   source, retry reason, and provider token usage.
2. Add an optional VLM OCR correction/extraction adapter behind existing extractor settings:
   `LIBRARIAN_PDF_VLM_OCR=never|low-confidence|always`.
3. Add an anchor-text helper that produces compact page reports from embedded PDF text and image
   positions. Use it in VLM prompts and keep it independent of any one model.
4. Add rotation retry support to the page manifest: first failed/rotated page records should be
   replayable without restarting the document.
5. Add repeat-tail checks to cleaned chunk and OCR-correction validation.
6. Add PDF preflight filters for forms and download-spam signals, with default `warn` behavior so
   local-first users do not unexpectedly lose documents.
7. Expand corpus-eval with table relation, math rendering, formatting, footnote, and old-scan
   expectations modeled after olmOCR-Bench.

Longer-term work:

- Add an S3/object-store content and queue backend only after local SQLite boundaries are saturated.
- Add benchmark reports that compare Tesseract-only, Tesseract plus LLM correction, and VLM-page OCR
  on the same sanitized cases.
- Add provider cost/page and pages/sec measurements to `librarian benchmark` and release evidence.

### Integration Priority

High. The pipeline and benchmark ideas directly address Librarian's remaining v0.2 risks:
real-provider baselines, 1,000-page OCR durability, OCR quality diagnostics, and richer conversion
evals.

## Longcut-Main

### Useful Capabilities

- Transcript chunking for long media. `lib/ai-processing.ts` chunks transcripts by time windows
  with overlap and has a reduce step for final highlight selection.
- Quote grounding. `lib/quote-matcher.ts` builds transcript indexes, uses exact and normalized
  matching, then falls back to word/ngram fuzzy matching to map quoted text back to segments.
- Citation-aware chat. `app/api/chat/route.ts` prompts for timestamped JSON answers, validates
  input, extracts timestamps, and maps citations back to transcript moments.
- Sentence reconstruction. `lib/transcript-sentence-merger.ts` merges caption fragments while
  bounding sentence duration, word count, and segment count.
- Export formats. `lib/transcript-export.ts` supports TXT, SRT, and CSV exports, including
  translated transcript variants and speaker metadata.
- Notes workflow. Notes can preserve selected transcript/chat/takeaway source context and jump back
  to timestamps.
- Provider registry. `lib/ai-providers/` defines a provider adapter registry with fallback behavior.
- API safety patterns. The app uses Zod validation, CSRF protection, request size caps, rate
  limiting, and audit logging.

### Porting Strategy

Use LongCut as a product and UX reference, not a source-code donor, because AGPL-3.0 is not a good
fit for direct copying into this project without an explicit licensing decision.

Clean-room implementation phases:

1. Promote transcript quote matching from CLI utility to a first-class application service that can
   attach `TranscriptMatch` evidence to search results, exports, and future chat responses.
2. Add transcript-specific search facets: speaker, timestamp range, source format, and quote-match
   confidence.
3. Add a `DocumentAnnotation` or `Note` model for local notes tied to document IDs, selected text,
   source offsets, transcript timestamps, and classification metadata.
4. Add grounded Q&A as an optional application service:
   `answer_question(document_id, question) -> answer, citations`.
   Citations should be computed and validated by Librarian, not trusted solely from provider output.
5. Extend transcript export to include quote bundles and citation JSON for downstream editors.
6. Add API security middleware equivalents for hosted mode: request size caps are already present in
   several places, but hosted deployments would benefit from endpoint-specific rate limits, audit
   events, and CSRF/session controls once multi-user support lands.

### Integration Priority

Medium-high for transcript-heavy use cases, especially quote-grounded chat and notes. Do the work as
native Librarian services so the CLI and API stay in parity.

## Recommended Roadmap Impact

### v0.2 Candidate Work

- Add olmOCR-inspired PDF anchor reports and VLM page OCR as optional adapters.
- Add rotation/retry/fallback diagnostics to page manifests.
- Expand corpus-eval with table relation, math, footnote, format, and repeated-tail checks.
- Add transcript quote/citation evidence to search results and exports.
- Add real-provider benchmark fields for pages/sec, cost/page, retry count, fallback count, and
  OCR-correction quality.

### v0.3 Candidate Work

- Add local notes/annotations linked to documents, selected text, timestamps, and classification.
- Add grounded Q&A over processed documents with citation validation.
- Add hosted-mode request throttling/audit controls.
- Add optional object-store queue/content adapters if large deployments exceed SQLite/local storage
  limits.

### Later Candidate Work

- Add a visual layout parser plugin interface with optional Dolphin-like typed element parsing.
- Add layout visualization artifacts for problematic pages.
- Add table/formula specialized extraction models once licensing and deployment constraints are
  settled.

## Copying Guidance

- Safe to adapt with attribution: `olmocr-main` Apache-2.0 code and tests, after reshaping into
  Librarian style and keeping license notices where required.
- Do not directly copy without legal review: `Dolphin-master`, because the repository contains a
  Qwen Research non-commercial license despite an MIT badge in the README.
- Do not directly copy without accepting AGPL obligations: `longcut-main`.
- Prefer clean-room design for LongCut and Dolphin features, using this document as requirements and
  the existing Librarian architecture as the implementation boundary.
