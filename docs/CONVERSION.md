# Conversion And Batch Processing

Librarian converts source files into Markdown or plain text before ingestion and processing.
Markdown is the canonical structured format; plain text is rendered from Markdown when requested.

## Commands

Convert one file:

```bash
librarian convert ./input/report.docx --format md --output ./output/report.md
librarian convert ./input/report.pdf --format txt --output ./output/report.txt
```

Convert a directory:

```bash
librarian convert-dir ./input --format md
librarian convert-dir ./input --format txt --output-mode original --overwrite
librarian convert-dir ./input --recursive --output-mode new-directory --output-dir ./converted
librarian convert-dir ./input --output-mode subdirectory --subdirectory-name librarian-output
```

Output modes:

- `subdirectory`: write into a subdirectory of the source directory. This is the default.
- `original`: write beside each original file.
- `new-directory`: preserve relative paths under a separate output directory.

Batch conversion continues after individual file failures and prints a per-file summary.
When two source files would produce the same output path, Librarian appends a numeric suffix unless
`--overwrite` is set.
Batch conversion always writes `.json` provenance sidecars next to generated outputs so later
recursive runs can identify and skip Librarian-generated files.

Local conversion and import paths enforce configurable input limits before expensive parsing:
`LIBRARIAN_MAX_SOURCE_BYTES`, `LIBRARIAN_TEXT_MAX_INPUT_BYTES`,
`LIBRARIAN_DOCX_MAX_INPUT_BYTES`, `LIBRARIAN_PDF_MAX_INPUT_BYTES`, and
`LIBRARIAN_PDF_MAX_PAGES`. API uploads are additionally bounded by
`LIBRARIAN_API_MAX_UPLOAD_BYTES`.

## Import Workflow

`librarian import` combines conversion, ingestion, and optional processing:

```bash
librarian import ./input --format md
librarian import ./input --recursive --format md --process
librarian import ./input --format txt --queue
librarian import ./input --output-mode new-directory --output-dir ./converted --process
librarian import ./input --process --manifest import-manifest.json --resume --report report.json
```

Processing modes:

- default: convert and ingest only.
- `--process`: process each document immediately in the current CLI process.
- `--queue`: create processing runs and enqueue them for `librarian worker`.

The command prints converted path, document ID, run ID, and any per-file error.

Import manifests and reports are JSON. Manifests are updated after each file so interrupted imports
can be resumed with `--resume`. Librarian marks these JSON files as generated metadata so recursive
imports do not ingest their own manifests or reports.

## Format Coverage

Built-in support:

- Text-like: `.txt`, `.md`, `.csv`, `.json`
- Office/PDF: `.docx`, `.pdf`
- OCR images: `.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`, `.bmp`, `.webp`

Optional broad conversion through MarkItDown:

- `.pptx`, `.xlsx`, `.xls`, `.msg`, `.html`, `.htm`, `.rtf`, `.epub`, `.xml`
- Install with `pip install -e ".[universal]"`.
- Broad conversion rejects inputs larger than `LIBRARIAN_UNIVERSAL_MAX_INPUT_BYTES`
  and stops work after `LIBRARIAN_UNIVERSAL_TIMEOUT_SECONDS`. Archive formats such
  as `.zip` are intentionally not enabled by default.

OCR support:

- Install Python dependencies with `pip install -e ".[ocr]"`.
- Install system tools:
  - macOS: `brew install tesseract poppler`
  - Ubuntu/Debian: `sudo apt-get install tesseract-ocr poppler-utils`
- Configure language with `LIBRARIAN_OCR_LANGUAGE`, for example `eng` or `eng+spa`.
- Bound OCR work with `LIBRARIAN_OCR_TIMEOUT_SECONDS`, `LIBRARIAN_OCR_PDF_DPI`, and
  `LIBRARIAN_OCR_PDF_MAX_PAGES`.
- Tune scanned-PDF throughput with `LIBRARIAN_OCR_PAGE_CONCURRENCY`.
- Control LLM OCR correction with `LIBRARIAN_OCR_LLM_CORRECTION=always|never|low-confidence`
  and optionally override the correction model with `LIBRARIAN_OCR_LLM_MODEL`. `low-confidence`
  is reserved for extractors that surface confidence scores; current PDF OCR uses `always` or
  `never`.

PDF extraction is page-aware. Librarian reads embedded text from pages that have it and OCRs only
pages where embedded extraction is empty. This avoids the old all-or-nothing scanned-PDF fallback
where mixed PDFs could silently lose scanned pages. PDFs over `LIBRARIAN_PDF_MAX_PAGES` are
rejected before page extraction. Scanned-page OCR is separately bounded by
`LIBRARIAN_OCR_PDF_MAX_PAGES`, which defaults to `1000`.

Markdown PDF output includes stable page boundaries:

```markdown
---
generated_by: librarian
artifact_type: pdf-page-extraction
source_file: report.pdf
page_count: 3
---

# report

<!-- page: 1 source: embedded corrected: false -->
## Page 1

...
```

## OCR Strategy

Tesseract is used as the raw OCR engine. For PDF pages that require OCR,
`LIBRARIAN_OCR_LLM_CORRECTION=always` sends each page through the configured OpenAI-compatible
provider before the document enters the normal cleaning/classification/indexing pipeline. Set
`LIBRARIAN_OCR_LLM_CORRECTION=never` for fully deterministic OCR-only conversion.

Batch conversion sidecars include extraction metadata for page count, per-page source
(`embedded`, `ocr`, or `failed`), correction status, and OCR confidence when the extractor can
surface it. Recursive conversion/import skips Librarian-generated sidecars and converted outputs.

## Large-PDF Test Recipe

For a real 500-1000 page PDF, run conversion first, then ingest and process the converted
Markdown:

```bash
export LIBRARIAN_OCR_PDF_MAX_PAGES=1000
export LIBRARIAN_PDF_MAX_PAGES=1000
export LIBRARIAN_OCR_PAGE_CONCURRENCY=2
export LIBRARIAN_OCR_LLM_CORRECTION=never

librarian convert ./massive.pdf --format md --output ./massive.md
librarian ingest ./massive.md
librarian process doc_...
librarian search "known phrase from the document"
```

Use `LIBRARIAN_OCR_LLM_CORRECTION=always` with real provider credentials when measuring final
quality instead of raw OCR throughput.

For a folder of PDFs, use `librarian import ./input --recursive --format md --process` instead.
