# Parsing foundation

Direction set by Jake on 2026-11-08. Engine rebuilt and measured 2026-11-09 and 2026-11-10.

## Direction

Librarian starts from a universal, deterministic file cracker. A file is opened, its positioned text and drawn structure are extracted, and a typed, LLM-ready document comes out quickly with no model calls. Intelligence layers (OCR, vision, table confirmation, copyediting of rough prose) sit on top and run only where the deterministic layer reports that they earn their cost.

The same typed blocks are the foundation for later uses, including editing documents block by block. None of that is built yet. Everything stays in the terminal.

The reference point is Nutrient's `/extraction/parse`. It reuses the PDF's native text layer, works on a 200 DPI grid, and routes each page to a processing tier. On Jake's two documents it took 11.7 s for 57 pages and 57.5 s for 244 pages. Librarian's earlier editorial pipeline rendered every page and chained model calls: the same 57-page book took 38 minutes and 235 requests.

## What `parse` does

```sh
librarian parse document.pdf                      # Markdown to stdout, summary to stderr
librarian parse document.pdf -o results --create-output
librarian parse folder -o results -r              # every PDF, including subfolders
librarian parse document.pdf -o results --plain --furniture --page-markers --words
```

With `-o`, each PDF becomes `<name>.md` and `<name>.layout.json`. Existing files are never replaced. One unreadable file does not stop a folder. `--plain` drops bold, italic, link and picture markup. `--furniture` keeps running headers, footers and page numbers. `--words` adds positioned words, fonts and the outline to the JSON. In the terminal interface the same engine is the first action on Home: **a Parse PDFs now**.

The JSON model (`librarian-layout-v2`) is a list of elements in reading order. Each has a role (title, heading, paragraph, list item, table, code, caption, footnote, picture, header, footer, page number, hidden), a page box in PDF points, text, inline spans (bold, italic, code, link), table cells with row and column spans, the evidence it rests on, and a per-page routing tier.

## Architecture

| Layer | Where | Job |
| --- | --- | --- |
| Miner | `librarian-native::parse` | PDFium to one input model: words with glyph boxes, baselines and fonts; images; line segments, fills and drawings from every page object including nested forms; links; bookmarks |
| Geometry | `layout/geom.rs` | Rectangles, clustering, interval arithmetic |
| Text | `layout/text.rs` | Document statistics, lines, list markers, hyphen decisions, span composition |
| Drawn grids | `layout/lattice.rs` | Tables from ruled lines: piecewise borders, implied edges, merged cells |
| Tables | `layout/tables.rs` | Cells from grids, row-ruled and rule-sectioned tables, tables from aligned text |
| Regions | `layout/regions.rs` | Whitespace gutters, each classified as text columns, a table, or noise |
| Blocks | `layout/blocks.rs` | Paragraphs, list items, headings, contents entries inside one column |
| Document | `layout/reconstruct.rs` | Page assembly, furniture, heading levels, joins across pages, routing, accounting |
| Projections | `layout/render.rs` | Markdown and the JSON model from the same elements |

`librarian-core` has no I/O and compiles to WebAssembly. Other miners (OCR, Office XML, HTML) can feed the same input model.

### Principles that made it accurate

1. **Evidence has an order.** What the author declared beats what was drawn, which beats what is inferred from spacing. Bookmarks declare headings. A caption that begins "Table 3" or "Figure 2" declares what the region beside it is. Ruled lines declare a grid. Whitespace is the fallback.
2. **Every threshold is relative to the document.** Body size, line pitch, vocabulary and hyphenation habits are measured per document. Nothing is tuned to a fixture's coordinates.
3. **Whitespace is classified, not just found.** A gutter between two strips is text columns if the strips read as prose, a table if rows align across it, and noise otherwise. This one decision replaced separate column and table detectors that disagreed with each other.
4. **Line-end hyphens follow the document's own vocabulary.** "four-" + "day" stays "four-day" when the document writes "four-day" elsewhere, or when the document does not break words at line ends at all. Only a true soft hyphen is removed without that check.
5. **Text you cannot see is not read.** Text outside the page box, invisible text, and rotated margin text are kept in the model as hidden and left out of the reading flow. An invisible layer over a scanned image is recognised as an OCR layer and used.
6. **Accounting is a hard guard.** Every native word must land in exactly one element, and the non-space characters going in must equal the characters coming out plus the hyphens deliberately removed. `parse` refuses to publish otherwise. This guard caught three bugs during the rebuild.

### Routing

Every page gets a tier with reasons: `deterministic`, `review` (a table whose grid is uncertain), `vision` (pictures large enough to carry information), or `ocr` (no usable text layer). `parse` reports routing and processes none of it.

## How it is measured

Fixtures and third-party outputs stay outside the repository. Four independent checks are used, because each one alone can be gamed.

### 1. Human ground truth: opendataloader-bench

200 single-page PDFs with hand-made answers ([opendataloader-bench](https://github.com/opendataloader-project/opendataloader-bench), commit `7af1d8f`). Three scores: NID (reading order and text), TEDS (table structure), MHS (heading hierarchy). The scorer was reimplemented on the same libraries and reproduces the published scores of three engines to four decimals.

Odd-numbered documents were the development set. Even-numbered documents were a holdout: scored in aggregate, never inspected one by one.

| Split | Librarian | Nutrient | Best published engine |
| --- | --- | --- | --- |
| Development (100) | 0.880 | 0.876 | 0.901 |
| Holdout (100) | 0.859 | 0.894 | 0.912 |
| All 200 | 0.870 | 0.885 | 0.907 |

| All 200, by score | Librarian | Nutrient | Best published engine |
| --- | --- | --- | --- |
| Reading order and text (NID) | 0.915 | 0.925 | 0.934 |
| Table structure (TEDS) | 0.680 | 0.708 | 0.928 |
| Heading hierarchy (MHS) | 0.796 | 0.819 | 0.821 |

The engine before the rebuild scored 0.747 and crashed on five pages. Among the 14 published engines Librarian now places fifth, behind opendataloader-hybrid (0.907), Nutrient (0.885), docling (0.882) and one opendataloader variant (0.877). Every engine above it uses a trained model. Librarian parses all 200 pages in 1.7 s on one thread.

The honest reading: Librarian beats Nutrient on the pages it was developed on and trails it by 0.035 on pages it never saw. The holdout gap is mostly tables (0.626 against 0.702) and headings (0.780 against 0.848). The development result overstates general accuracy; the holdout result is the one to trust.

### 2. Author truth: tagged PDFs

Nine tagged PDFs (432 pages). The author's own structure tags, read at test time with Poppler, are the answer key. On the Apple Style Guide: list items 0.99 precision and 1.00 recall, text similarity 0.999, headings 1.00 recall and 0.66 precision. The Apple file has 91 real heading tags; an earlier note said 203, but 112 of those are empty tags. The surplus headings are mostly bold lead-in labels that the author tagged as paragraphs.

The product does not read structure tags. PDFium exposes them only through unsafe bindings, and `librarian-native` forbids unsafe code.

### 3. Agreement with Nutrient on Jake's documents

`crates/librarian-native/examples/layout_bench.rs` aligns our words with a Nutrient spatial response.

| Measure | Make It Rain (57 pp) | Apple Style Guide (244 pp) |
| --- | --- | --- |
| Librarian parse | 104 ms | 757 ms |
| Nutrient | 11,746 ms | 57,532 ms |
| Our words matched to Nutrient | 99.89% | 99.90% |
| Reading-order discordance | 0.012% | 0.005% |
| Headings precision / recall | 96.8% / 44.3% | 74.0% / 80.6% |
| List items precision / recall | 100% / 100% | 99.5% / 98.5% |
| Table words precision / recall | none in either | 100% / 94.6% |
| Footers precision / recall | 98.1% / 100% | 100% / 100% |
| Table pages ours / Nutrient / shared | 0 / 0 / 0 | 12 / 13 / 12 |

This measures agreement, not truth. Nutrient labels emphasised sentences as headings in Make It Rain, which is why recall there is low. The parse times are higher than the first version (63 ms and 330 ms) because the miner now walks every drawing on the page.

### 4. Robustness sweep

1,869 PDFs from the test suites of pdf.js, pdfminer, pdfplumber, pypdf, camelot, OCRmyPDF, veraPDF, docling and unstructured, each parsed in its own process.

| Outcome | Files |
| --- | --- |
| Parsed | 1,822 |
| Refused with a plain reason (password-protected, damaged or not a PDF) | 47 |
| Crashed, timed out, or failed accounting | 0 |

Average 3.7 ms per page.

A further 18 multi-page documents with another engine's output as reference rose from 0.569 to 0.713. That is agreement with a different parser, not truth, and is used only to spot regressions.

## Limits

- **Scanned pages are reported, not read.** `parse` routes them to `ocr` and stops. OCR inside `parse` is not built.
- **Tables without ruled lines are the weakest area.** The best engines use a trained table model and score 0.93 where Librarian scores 0.68.
- **Headings are over-detected in documents that use bold lead-in labels.**
- **Structure tags are not read** in the product, for the reason above.
- **Fonts that cannot be decoded to Unicode are not yet detected.** Such pages produce wrong characters instead of an `ocr` route.
- **A printed table of contents is recognised but not used** to confirm heading levels.
- **PDF only.** Text, Markdown, HTML, DOCX, PPTX and images still go through `librarian clean`.
- **`clean` still uses the earlier pipeline** that renders and reads every page. It is as slow as before.

## Next milestones

1. A model layer for the pages and regions the routing report names: OCR for `ocr` pages, a table model for `review` regions, descriptions for `vision` pictures. This is the route to the top of the benchmark.
2. Route `clean` through `parse`, so clean born-digital pages skip OCR and vision entirely.
3. An editorial layer for rough prose such as transcripts: one copyedit pass per chunk, chunks in parallel, deterministic coverage accounting.
4. Native miners for DOCX, PPTX, XLSX, HTML and plain text into the same model.
5. Block editing on the typed model, in the terminal first.
