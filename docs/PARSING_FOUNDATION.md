# Parsing foundation — 2026-11-08

## Direction

Jake directed a first-principles rebuild on 2026-11-08. Librarian starts from a universal, deterministic file cracker: any file is opened, its positioned text and structure are extracted, and a typed, LLM-ready document is produced quickly and without model calls. Intelligence layers (OCR, vision, table confirmation, CMOS copyediting of rough prose) sit on top and are invoked only where the deterministic layer reports that they earn their cost.

The reference point is Nutrient's `/extraction/parse`. On Jake's two test documents it processed 57 pages in 11.7 s (agentic mode) and 244 pages in 57.5 s (auto mode). Analysis of its output showed that it reuses the PDF's native text layer (word confidences of exactly 1.0 match the native word count), works in a 200 DPI coordinate grid, and routes each page to a processing tier. Librarian's previous editorial pipeline instead rendered every page to an image, ran OCR and vision on it, and chained Typesetter and Reviewer calls: the same 57-page book took 38 minutes and 235 requests and lost passages that were present in the native text layer.

## Architecture

1. **Miner** (`librarian-native::parse`): format-specific extraction into one input model. The PDF miner uses the pinned, bundled PDFium build: words with tight glyph boxes, baselines, font name/size/weight/italic/monospace, line-break hyphens, image placements and thin vector rules. PDFium matched or exceeded Poppler on both test documents and is permissively licensed.
2. **Layout model** (`librarian-core::layout`, WASM-compatible): `DocumentInput` (pages, words, fonts, images, rules) in, `Document` (typed, ordered elements and per-page routing) out. Coordinates are PDF points with a top-left origin, so OCR, Office XML and HTML miners can feed the same model later.
3. **Deterministic reconstruction**: lines from source order and geometry; page furniture from margin position, cross-page recurrence and a consistent folio offset; ruled tables from horizontal rule segments and aligned tables from recurring column anchors; reading order by recursive XY-cut with a column-gutter preference; blocks from baseline pitch, alignment and style; roles from font statistics measured on the document itself (body size, typical pitch); cross-page paragraph continuation and line-break de-hyphenation. Thresholds are relative to document statistics, never to a fixture's coordinates.
4. **Routing**: every page receives a tier and reasons: `deterministic`, `review` (uncertain table grid), `vision` (pictures large enough to carry information), or `ocr` (no or undecodable text layer). `parse` reports routing and makes no model calls.
5. **Projections**: reader Markdown (furniture omitted, continuations joined, GFM tables, pictures as comments) and the JSON document model, both from the same extraction.

Accounting is a hard invariant: every native word must appear in exactly one element. `parse` refuses to publish if any word is unassigned or duplicated.

## Command

```sh
librarian parse document.pdf
librarian parse document.pdf -o results --create-output
librarian parse document.pdf -o results --words --furniture --page-markers
```

Without `-o`, Markdown goes to stdout and a JSON summary to stderr. With `-o`, `<name>.md` and `<name>.layout.json` are written without replacing existing files. `--words` adds word indices, positioned words and fonts to the JSON.

## Benchmark against Nutrient

`crates/librarian-native/examples/layout_bench.rs` aligns our words with a Nutrient spatial response (one-to-one, page-local, normalized text and nearest center) and reports coverage, grouping, role agreement, table pages, reading-order discordance and timing. Fixtures stay outside the repository.

```sh
cargo run --release -p librarian-native --example layout_bench -- \
  --pdfium /path/to/libpdfium.so document.pdf nutrient.json [--explain]
```

Results on 2026-11-08 (single thread, 4-core Linux container):

| Measure | Make It Rain (57 pp) | Apple Style Guide (244 pp) |
| --- | --- | --- |
| Librarian parse | 63 ms | 330 ms |
| Nutrient | 11,746 ms | 57,532 ms |
| Our words matched to Nutrient | 99.89% | 99.90% |
| Grouping precision | 99.97% | 99.91% |
| Reading-order discordance | 0.012% | 0.006% |
| Heading precision / recall | 98.4% / 44.3% | 90.0% / 80.6% |
| List items precision / recall | 100% / 100% | 99.8% / 99.7% |
| Table words precision / recall | none in either | 98.7% / 99.8% |
| Footers precision / recall | 98.1% / 100% | 100% / 100% |
| Table pages ours / Nutrient | 0 / 0 | 13 / 13 shared |

How to read these numbers:

- They measure agreement with Nutrient, not verified truth. Nutrient's own output has errors: it merges whole columns of glossary entries into one paragraph, merges separate paragraphs, labels bold sentences as titles, and left all 57 Apple pictures undescribed. Low grouping recall mostly reflects Nutrient's coarser blocks.
- Nutrient's Title labels are inconsistent, so titles are scored as headings on both sides. Nutrient serializes table cells column by column; the benchmark reads them row by row.
- Make It Rain is the holdout. The earlier Python prototype, tuned on Apple, scored 7.5% heading precision and 0% footers on it.
- Make It Rain heading recall is low because Nutrient labels emphasized body sentences ("It was Gary Bencivenga.") as headings. Apple code recall is low because monospace runs are mostly inline.

## Limits

- PDF only in this slice. Text, Markdown, HTML, DOCX, PPTX and images still go through `librarian clean`.
- Pages routed to `ocr` or `vision` are reported, not yet processed by `parse`.
- No multi-column test document exists yet; a focused invariant test covers two-column reading order and word accounting.
- Reading order inside complex mixed layouts, rotated text and right-to-left scripts is untested.
- The editorial pipeline still renders and reads every page. Rerouting it onto this foundation is the next milestone.

## Next milestones

1. Route `clean` through `parse`: deterministic pages skip OCR and vision; OCR only for `ocr` pages or image regions; vision only on cropped pictures; table review only for `review` pages.
2. Editorial layer for rough prose (transcripts, captions): one CMOS copyedit pass per chunk, chunks in parallel, deterministic coverage accounting, reviewer only on flagged spans. Clean born-digital prose skips it unless requested.
3. Native miners for DOCX/PPTX/XLSX XML, HTML and plain text into the same layout model.
4. Benchmark set: add a scanned PDF and a raw transcript, and run the benchmark before every change to reconstruction.
