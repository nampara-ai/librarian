# PDF parsing reference

This document describes the PDF parser of the Librarian terminal program: what it accepts, what it produces, how it reconstructs a page, how its results were measured, and what it does not do. The commands and screens that run the parser are described in the terminal reference, `RUST_FOUNDATION.md` in the repository and `README.md` in a package.

## 1. Purpose

The parser reads a PDF file and produces a typed description of its contents in reading order: titles, headings, paragraphs, list items, tables, code, captions, footnotes, pictures and page furniture. It works from the text and drawing instructions in the file. It calls no model and contacts no service. Its output is the same for the same input.

The parser is the base layer of the program. On 2026-11-08 the owner directed that every later function, including OCR, picture description and copyediting, be built on it and be applied only to the pages and regions the parser identifies as needing them. Those later functions are not yet connected to it. See section 9.

## 2. Input

The parser accepts PDF files. A file is refused, with the reason stated, when it is password-protected, when it is damaged or is not a PDF, when it uses a security scheme the PDF engine does not support, or when the PDF engine cannot read it for another reason.

The PDF engine is PDFium. It is found in the workspace `engines` folder or in the `engines` folder beside the program.

## 3. Output

### 3.1 Files

For a source `NAME.pdf` and a results folder, the parser writes `NAME.md` and `NAME.layout.json`. An existing file of either name is not replaced; the source is reported as failed. Without a results folder, one file is parsed and its Markdown is printed.

### 3.2 Markdown

| Element | Written as |
| --- | --- |
| Title | `# text` |
| Heading | One to six `#` characters by level, then the text |
| Paragraph, caption, footnote | The text |
| List item | `- text`. A printed number or letter label is kept after the dash |
| Code | A fenced block |
| Table | A pipe table. A table with merged cells is written as an HTML table |
| Picture | An HTML comment giving the page and the position. Labels printed inside the picture follow, when they are of readable size |
| Header, footer, page number | Omitted, unless furniture is requested |
| Hidden text | Omitted |

Bold, italic and monospaced runs inside a text are marked with `**`, `*` and backticks. A link is written as `[text](address)`. Marks are omitted where one style covers the whole element. A paragraph that continues on the next page is joined into one. A table that continues on the next page is joined into one, and a repeated header row is dropped. With page markers requested, each page boundary is marked by an HTML comment.

The plain option omits inline marks and picture comments.

### 3.3 JSON

The file `NAME.layout.json` has two top-level members, `summary` and `document`. With the words option it also has `pages`, `fonts` and `outline`. The schema name is `librarian-layout-v2`. Coordinates are in PDF points, with the origin at the top left of the page.

`summary`:

| Member | Meaning |
| --- | --- |
| `source` | Path of the PDF |
| `pages`, `words`, `elements` | Counts |
| `roles` | Count of elements by role |
| `routing` | Count of pages by tier. See section 5 |
| `hyphens` | Line-end hyphens `joined` and `kept` |
| `model_calls` | Always 0 |
| `timing_ms` | `open`, `extract`, `layout`, `total` |

`document`:

| Member | Meaning |
| --- | --- |
| `schema`, `format` | `librarian-layout-v2`, `pdf` |
| `pages` | For each page: `number`, `width`, `height`, `words`, `tier` |
| `elements` | The elements, in reading order |
| `routing` | For each page: `page`, `tier`, `reasons`, and `regions` where the tier applies to part of the page |
| `stats` | Totals, the measured body size and line pitch, and the accounting counts of section 6 |

Each element:

| Member | Meaning |
| --- | --- |
| `id` | Identifier, unique in the document |
| `page` | Page number, from 1 |
| `role` | `title`, `heading`, `paragraph`, `list_item`, `table`, `code`, `caption`, `footnote`, `picture`, `header`, `footer`, `page_number` or `hidden` |
| `level` | Heading level, 1 to 6 |
| `text` | The text. For a table, the cell texts |
| `bbox` | `x`, `y`, `w`, `h` |
| `order` | Position in the reading order of the document |
| `font_size`, `bold` | Dominant size and weight |
| `spans` | Inline styles: `start` and `end` byte offsets into `text`, with `bold`, `italic`, `code` or `link` |
| `rows`, `cols`, `cells` | For a table. Each cell has `row`, `col`, `rowspan`, `colspan`, `header`, `text`, `bbox` |
| `continues` | The element is completed by the next body element |
| `tight` | The join to the next element completes a word and takes no space |
| `origin` | `native`, or `ocr_layer` for invisible text laid over a scanned page by an earlier OCR |
| `evidence` | `layout` (measured from the page), `ruling` (lines drawn in the file) or `declared` (the file's bookmarks) |
| `confidence` | A number from 0 to 1 |
| `words` | Indices into the page's word list. Present with the words option |

With the words option, `pages` holds for each page its words (text, box, font, size, baseline, and flags for hidden and rotated text), image positions, line segments, filled rectangles, other drawings and links. `fonts` holds each font's name and whether it is bold, italic or monospaced. `outline` holds the bookmarks with level, title and page.

## 4. Method

The parser has two stages. The first, in `crates/librarian-native/src/parse.rs`, reads the file through PDFium. The second, in `crates/librarian-core/src/layout/`, reconstructs the layout. The second stage performs no file access and also compiles to WebAssembly.

### 4.1 Extraction

For each page the first stage records:

- every word, with its box, baseline, font, size, and whether it is drawn invisibly or rotated;
- every image position;
- every straight horizontal or vertical line segment, every filled rectangle, and the bounds of all other drawings, including those inside nested forms;
- every link;
- the number of characters that could not be decoded.

Positions are corrected for a page whose visible area is an offset or rotated window on its coordinate space. Words are separated where the file has no space characters but leaves a gap between letters. The document's bookmarks are recorded.

### 4.2 Reconstruction

The second stage applies the following steps. Each threshold is a proportion of a quantity measured on the document itself, such as its body type size or its line pitch. No threshold refers to a particular test document.

1. **Measurement.** The body type size, the usual line pitch, the vocabulary, and whether the document breaks words at line ends are determined.
2. **Text outside the reading flow.** Words outside the page box, invisible words and rotated words are set aside as hidden. If nearly all text of a page is invisible, the page carries an OCR layer; that text is then used and is marked `ocr_layer`.
3. **Pictures.** Images are grouped into picture regions. A cluster of type much smaller than body size is taken as the content of an illustration. Groups of drawings form vector figures. A single shape that encloses running text is a panel or frame, not a figure, and is ignored. A line of text is never divided by the edge of a figure.
4. **Captions.** A line beginning "Figure" or "Table" and a number declares the kind of the block beside it. A ruled grid captioned as a figure is treated as a picture.
5. **Ruled tables.** Line segments are merged into strokes. Where borders are drawn one cell at a time, the points at which they break mark the cell corners. Borders that break at the same position are joined into one grid when a drawn line crosses both or when they share the same extent. Borders that run past the last drawn line to a common end close a row. Cells not separated by a line are merged. Tables with horizontal rules only take their columns from the text.
6. **Regions.** The remaining words are divided by vertical bands of white space. Each band is classified. If the text on both sides reads as prose, the band separates columns. If the lines on both sides align row for row, the region is a table. Otherwise the band is ignored. A column of bullets beside its text, and a list of titles beside page numbers, are not tables. Text set in widely different sizes within one column is not a table.
7. **Blocks.** Within a column, lines are grouped into blocks by spacing, indentation, type size and weight, and list markers. A block is a heading when it is larger or heavier than body text, or numbered as a section, and is short. A block opening with a list marker is a list item. Small type low on the page that opens with a reference mark is a footnote. A monospaced block is code.
8. **Line-end hyphens.** A word divided at a line end is joined. The hyphen is kept when the document writes the hyphenated form elsewhere, or when the document does not divide words at line ends.
9. **Page furniture.** Text in the top or bottom margin that recurs across pages is a header or footer. A number that advances with the page is a page number.
10. **Bookmarks.** A block whose text matches a bookmark on its page is a heading at the bookmark's level. Where a document has at least eight such headings and its bookmarks have more than one level, a line that typography alone made a heading is returned to a paragraph if it is of body size, smaller than every bookmarked heading, not numbered as a section, and named by no bookmark.
11. **Heading levels and title.** Remaining headings are ranked by type size. The largest type on the first pages, used once or twice, is the title.
12. **Continuation.** A paragraph that ends without closing punctuation at the foot of a column or page is marked as continuing in the next. A table whose columns match the table that opens the next page is marked as continuing.

## 5. Routing

Each page is assigned one tier. The tier states what further processing the page would need. The parser reports the tier and performs none of that processing.

| Tier | Assigned when | Reason text |
| --- | --- | --- |
| `ocr` | The page has fewer than 16 letters and digits | "no usable text layer", or "scanned or image-only page: no usable text layer" |
| `ocr` | More than 2 percent of characters could not be decoded | "text layer contains undecodable characters" |
| `ocr` | More than one fifth of the readable characters are private-use code points | "much of the text uses a font with no Unicode mapping" |
| `review` | The text is an OCR layer | "text comes from an embedded OCR layer of unknown quality" |
| `vision` | The page has a picture covering at least 3 percent of its area | "N figure(s) may carry information not in the text layer" |
| `review` | A table found from text alignment has low confidence | "sparse table grid: cell structure needs confirmation" |
| `deterministic` | None of the above | "native text layer reconstructed deterministically" |

Where several apply, the page takes the highest of `ocr`, `vision`, `review`, `deterministic`, and all reasons are listed.

## 6. Accounting

After reconstruction the parser verifies that every word of the file belongs to exactly one element, and that the count of non-space characters in the elements, plus the hyphens removed at line ends, equals the count in the file. If either check fails the parser writes nothing for that file and reports the counts. The parser's automated tests assert this check, and every measurement run of section 7 exercises it.

## 7. Measurements

The figures below were taken on 2026-11-10 with the build that is on the branch. Test documents and the outputs of other products are kept outside the repository. The scripts are in `tools/bench/`. They need Python with the packages in `tools/bench/requirements.txt`.

### 7.1 Human ground truth

The collection is [opendataloader-bench](https://github.com/opendataloader-project/opendataloader-bench) at commit `7af1d8f`: 200 single-page PDFs with hand-made reference Markdown. Three scores are computed. NID measures text and reading order. TEDS measures table structure. MHS measures the heading hierarchy. The overall score is their mean over the documents to which each applies. The scorer in `tools/bench/score.py` was written for this project from the published definitions; it reproduces the published scores of three other engines to four decimal places.

Odd-numbered documents were used during development. Even-numbered documents were held out: their aggregate was read, and no single document was examined.

| Documents | Librarian | Nutrient | Highest published |
| --- | --- | --- | --- |
| Development half (100) | 0.887 | 0.876 | 0.901 |
| Holdout half (100) | 0.866 | 0.894 | 0.912 |
| All (200) | 0.876 | 0.885 | 0.907 |

| Score, all 200 | Librarian | Nutrient | Highest published |
| --- | --- | --- | --- |
| NID | 0.921 | 0.925 | 0.934 |
| TEDS | 0.707 | 0.708 | 0.928 |
| MHS | 0.802 | 0.819 | 0.821 |

The parser before the rebuild scored 0.747 and failed on five documents. Of the 14 engines with published results, four score higher on all 200 documents: opendataloader-hybrid (0.907), Nutrient (0.885), docling (0.882) and one opendataloader variant (0.877).

The holdout figure is the better estimate of performance on unseen documents. On the holdout half the parser is 0.028 below Nutrient. The difference is in tables (0.654 against 0.702) and headings (0.793 against 0.848).

```sh
python3 tools/bench/benchmark.py --librarian target/release/librarian \
  --bench /path/to/opendataloader-bench --out /path/to/results
```

### 7.2 Structure declared by the author

Nine tagged PDFs, 432 pages. Each file's own structure tags are read with Poppler and serve as the reference. On the Apple Style Guide (244 pages): list items, precision 0.99 and recall 1.00; headings, precision 0.82 and recall 1.00; text similarity 0.999. That file has 91 heading tags that carry text. An earlier note in this project gave 203; the other 112 are empty tags.

The parser itself does not read structure tags. The PDFium interface to them requires unsafe code, which the native crate forbids.

```sh
python3 tools/bench/tagged.py --librarian target/release/librarian \
  --pdfs /path/to/tagged-pdfs --out /path/to/results
```

### 7.3 Agreement with Nutrient on two of the owner's documents

`crates/librarian-native/examples/layout_bench.rs` matches the parser's words to a Nutrient response for the same file and compares the labels.

| Measure | Make It Rain (57 pages) | Apple Style Guide (244 pages) |
| --- | --- | --- |
| Librarian parse time | 100 ms | 666 ms |
| Nutrient response time | 11,746 ms | 57,532 ms |
| Librarian words matched to Nutrient words | 99.89% | 99.90% |
| Reading-order discordance | 0.012% | 0.005% |
| Headings, precision / recall | 96.8% / 44.3% | 75.9% / 69.7% |
| List items, precision / recall | 100% / 100% | 99.5% / 99.5% |
| Table words, precision / recall | no tables | 100% / 94.6% |
| Footers, precision / recall | 98.1% / 100% | 100% / 100% |
| Pages with a table: Librarian / Nutrient / both | 0 / 0 / 0 | 12 / 13 / 12 |

These figures measure agreement between two programs. They are not a measure of correctness. Nutrient labels emphasized sentences in Make It Rain as headings, and labels bold lead-in lines in the Apple Style Guide as headings where the author's tags mark them as paragraphs. Following the author's tags lowered heading agreement with Nutrient on that file and raised precision against the tags from 0.66 to 0.82.

```sh
cargo run --release -p librarian-native --example layout_bench -- \
  --pdfium /path/to/libpdfium document.pdf nutrient.json
```

### 7.4 Robustness

1,869 PDFs from the test collections of pdf.js, pdfminer, pdfplumber, pypdf, camelot, OCRmyPDF, veraPDF, docling and unstructured were parsed, each in its own process.

| Outcome | Files |
| --- | --- |
| Parsed | 1,822 |
| Refused with a stated reason | 47 |
| Crashed, exceeded 60 seconds, or failed accounting | 0 |

Of the 47 refused, 24 were damaged or not PDF files and 23 were password-protected. Parse time was between 4 and 14 milliseconds per page on the two machines used.

```sh
python3 tools/bench/sweep.py --librarian target/release/librarian /path/to/pdf-collections
```

### 7.5 One brochure

On 2026-11-10 the owner supplied a six-page product brochure with photographs, a shaded panel of captioned illustrations, and a 39-row specification table beneath a dimensioned drawing. The first parse was wrong in the ways listed. Each fault was corrected by a rule of section 4.2. With those rules the holdout score of section 7.1 rose from 0.859 to 0.866.

| Fault | Rule |
| --- | --- |
| Text in the shaded panel was taken as one picture and its columns were interleaved | A single shape that encloses running text is a panel |
| The label column of the table became one cell | Borders that break at the same position form one grid when a drawn line crosses both |
| The drawing above the table was joined to it | Borders with no line across both and with different extents are separate |
| The shaded header band was left out of the table | Borders that run past the last drawn line to a common end close a row |
| A line of text was divided at the edge of a figure | A line of text is never divided by the edge of a figure |
| An italic lead-in was separated from its sentence | An italic line that runs on into a sentence is not a separate block |
| The footer, set partly in large type, was taken as a table | Widely different sizes within one column are not a table |
| Footnotes marked with daggers were merged | A reference mark opens a footnote |

Three faults remain on that brochure. Photograph captions follow the text of their page instead of standing beside their photograph. A decorative dash under the cover line is kept as a line of text. The telephone number and web address are labelled as headings because they are set in large type.

### 7.6 Regression reference

Eighteen multi-page documents from the docling test collection are compared with docling's own output for them. The overall agreement rose from 0.569 to 0.714 during the rebuild. docling's output contains errors of its own. This comparison is used only to detect unintended changes.

## 8. Rules for changing the parser

These rules are stated in `AGENTS.md` and apply to every change to reconstruction.

1. Measure on more than one document. Do not set a threshold to suit one document.
2. Run the benchmark of section 7.1 and read both halves. Do not examine individual holdout documents.
3. Run the sweep of section 7.4. It must end with no crash, no timeout and no accounting failure.
4. Keep test documents and the outputs of other products outside the repository.

## 9. What the parser does not do

- **Scanned pages.** A page without a usable text layer is assigned the tier `ocr` and is not read. OCR is not connected to the parser.
- **Pictures.** A picture is recorded by position. It is not described.
- **Tables without ruled lines.** These are the least accurate result. The engines that score highest on table structure use a trained table model.
- **Bold lead-in lines.** In a document without bookmarks, a short bold line above a paragraph may be labelled a heading.
- **Fonts without a Unicode mapping.** A page is assigned to `ocr` when its text is replacement characters or mostly private-use code points. A font that yields wrong but valid letters is not detected.
- **Structure tags.** They are not read.
- **A printed table of contents.** It is recognized as such. It is not used to confirm headings.
- **Other formats.** Text, Markdown, HTML, office documents and images are handled only by `clean`.
- **The editorial pipeline.** `clean` does not use the parser. It renders and reads every page as before.

## 10. Work not yet done

1. A model layer applied to the pages and regions named by routing: OCR for `ocr`, a table model for `review`, description for `vision`.
2. Passing `clean` through the parser, so that pages reconstructed deterministically are not sent to OCR or to a vision model.
3. Extraction of office documents, HTML and plain text into the same layout model.
4. Editing of a document block by block on the layout model, in the terminal interface.
