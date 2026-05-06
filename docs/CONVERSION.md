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

## Import Workflow

`librarian import` combines conversion, ingestion, and optional processing:

```bash
librarian import ./input --format md
librarian import ./input --recursive --format md --process
librarian import ./input --format txt --queue
librarian import ./input --output-mode new-directory --output-dir ./converted --process
```

Processing modes:

- default: convert and ingest only.
- `--process`: process each document immediately in the current CLI process.
- `--queue`: create processing runs and enqueue them for `librarian worker`.

The command prints converted path, document ID, run ID, and any per-file error.

## Format Coverage

Built-in support:

- Text-like: `.txt`, `.md`, `.csv`, `.json`
- Office/PDF: `.docx`, `.pdf`
- OCR images: `.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`, `.bmp`, `.webp`

Optional broad conversion through MarkItDown:

- `.pptx`, `.xlsx`, `.xls`, `.msg`, `.html`, `.htm`, `.rtf`, `.epub`, `.xml`, `.zip`
- Install with `pip install -e ".[universal]"`.

OCR support:

- Install Python dependencies with `pip install -e ".[ocr]"`.
- Install system tools:
  - macOS: `brew install tesseract poppler`
  - Ubuntu/Debian: `sudo apt-get install tesseract-ocr poppler-utils`

Scanned PDFs first try normal PDF text extraction. If no embedded text is found, Librarian falls
back to PDF-to-image conversion plus Tesseract OCR.

## OCR Strategy

Tesseract is used as the raw OCR engine. LLM-aided OCR is handled through Librarian's existing
pipeline: convert raw OCR to text/Markdown first, then run normal processing to clean, correct,
classify, and index the result. This keeps OCR deterministic and makes LLM correction provider
agnostic.
