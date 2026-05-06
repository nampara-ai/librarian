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
librarian convert-dir ./input --format md --sidecar-metadata
```

Output modes:

- `subdirectory`: write into a subdirectory of the source directory. This is the default.
- `original`: write beside each original file.
- `new-directory`: preserve relative paths under a separate output directory.

Batch conversion continues after individual file failures and prints a per-file summary.
When two source files would produce the same output path, Librarian appends a numeric suffix unless
`--overwrite` is set.

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
can be resumed with `--resume`.

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

Scanned PDFs first try normal PDF text extraction. If no embedded text is found, Librarian falls
back to PDF-to-image conversion plus Tesseract OCR.

## OCR Strategy

Tesseract is used as the raw OCR engine. LLM-aided OCR is handled through Librarian's existing
pipeline: convert raw OCR to text/Markdown first, then run normal processing to clean, correct,
classify, and index the result. This keeps OCR deterministic and makes LLM correction provider
agnostic.
