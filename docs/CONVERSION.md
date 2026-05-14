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

Normalize a timestamped transcript without calling an LLM:

```bash
librarian transcript-normalize ./input/captions.srt --format md --output ./output/captions.md
librarian transcript-normalize ./input/captions.vtt --format vtt --output ./output/captions.vtt
librarian transcript-normalize ./input/transcript.txt --format csv --output ./output/transcript.csv
librarian transcript-find ./input/captions.srt "quoted source phrase" --json
```

`transcript-normalize` accepts SRT/VTT-style timestamp ranges and line-oriented timestamp
prefixes. By default it merges short timestamp segments into sentence-like spans while preserving
speaker labels and source timestamps. It strips common SRT/VTT caption markup, preserves WebVTT
voice labels as speakers, and unescapes caption entities; use `--no-merge-sentences` to keep
original segment granularity. Output formats are `md`, `txt`, `srt`, `vtt`, and `csv`.
`transcript-find` maps an exact or fuzzy quote back to transcript timestamps and segment indexes,
which is useful for citation checks before importing or publishing cleaned transcript excerpts.

Local conversion and import paths enforce configurable input limits before expensive parsing:
`LIBRARIAN_MAX_SOURCE_BYTES`, `LIBRARIAN_TEXT_MAX_INPUT_BYTES`,
`LIBRARIAN_DOCX_MAX_INPUT_BYTES`, `LIBRARIAN_PDF_MAX_INPUT_BYTES`, and
`LIBRARIAN_PDF_MAX_PAGES`. API uploads are additionally bounded by
`LIBRARIAN_API_MAX_UPLOAD_BYTES`.

## Import Workflow

`librarian import` combines conversion, ingestion, and optional processing:

```bash
librarian import ./large.md --format md --process
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
can be resumed with `--resume`. Manifest paths must end in `.json`; existing manifest files must be
Librarian import reports, which prevents accidental overwrite of unrelated JSON. Librarian marks
these JSON files as generated metadata so recursive imports do not ingest their own manifests or
reports. Manifest paths, report output paths, and `new-directory` output paths must not be symlinks
or cross symlinked parents.

## Format Coverage

Built-in support:

- Text-like: `.txt`, `.md`, `.csv`, `.json`
- Transcript captions: `.srt`, `.vtt` converted to timestamped Markdown with sentence merging
- Office/PDF: `.docx`, `.pdf`
- OCR images: `.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`, `.bmp`, `.webp`

Optional broad conversion through MarkItDown:

- `.pptx`, `.xlsx`, `.xls`, `.msg`, `.html`, `.htm`, `.rtf`, `.epub`, `.xml`
- Install with `pip install -e ".[universal]"`.
- Broad conversion rejects inputs larger than `LIBRARIAN_UNIVERSAL_MAX_INPUT_BYTES`
  and stops work after `LIBRARIAN_UNIVERSAL_TIMEOUT_SECONDS`. Archive formats such
  as `.zip`, `.tar`, `.7z`, and `.rar` are intentionally rejected by default.
  Renamed archive signatures are rejected for text and broad-format inputs that are
  not supported ZIP-container document types.
  Unpack archives outside Librarian after scanning them with your organization-approved
  malware tooling, then import the extracted files from a controlled directory.

OCR support:

- Install Python dependencies with `pip install -e ".[ocr]"`.
- Install system tools:
  - macOS: `brew install tesseract poppler`
  - Ubuntu/Debian: `sudo apt-get install tesseract-ocr poppler-utils`
- Run `librarian doctor --strict` before large conversions to verify optional Python packages and
  OCR/rasterization executables are available. CI runs the same strict check after installing
  `tesseract-ocr` and `poppler-utils`.
- Configure language with `LIBRARIAN_OCR_LANGUAGE`, for example `eng` or `eng+spa`.
- Bound OCR work with `LIBRARIAN_OCR_TIMEOUT_SECONDS`, `LIBRARIAN_OCR_PDF_DPI`, and
  `LIBRARIAN_OCR_PDF_MAX_PAGES`.
- Improve difficult scans before Tesseract with `LIBRARIAN_OCR_PREPROCESS_MODE=none|grayscale|threshold|deskew`.
  `threshold` and `deskew` use `LIBRARIAN_OCR_THRESHOLD`, which defaults to `180`.
- Preserve rasterized page images for debugging with `LIBRARIAN_OCR_PRESERVE_PAGE_IMAGES=true`.
  This only writes page images when conversion sidecars are enabled.
- Tune scanned-PDF throughput with `LIBRARIAN_OCR_PAGE_CONCURRENCY`.
- Control LLM OCR correction with `LIBRARIAN_OCR_LLM_CORRECTION=always|never|low-confidence`
  and optionally override the correction model with `LIBRARIAN_OCR_LLM_MODEL`.
  `low-confidence` uses Tesseract TSV word confidence for PDF OCR and only corrects pages below
  `LIBRARIAN_OCR_LOW_CONFIDENCE_THRESHOLD`, which defaults to `85`.

## Output Quality Warnings

Cleaned chunk records include non-fatal warnings when output quality checks find likely rendering
regressions, including collapsed paragraphs, missing Markdown headings/lists/tables, missing
citation markers, malformed Markdown tables, orphan list markers, repeated tails, context-marker
leaks, and assistant artifacts. These warnings are persisted with cleaned chunks so eval suites and
operators can investigate suspicious outputs without blocking successful runs. LLM OCR correction
uses the same warning checks before corrected page text is accepted, and those correction warnings
are attached to the PDF page manifest records for the affected OCR pages.

PDF extraction is page-aware. Librarian reads embedded text from pages that have it and OCRs only
pages where embedded extraction is empty. This avoids the old all-or-nothing scanned-PDF fallback
where mixed PDFs could silently lose scanned pages. PDFs over `LIBRARIAN_PDF_MAX_PAGES` are
rejected before page extraction. Scanned-page OCR is separately bounded by
`LIBRARIAN_OCR_PDF_MAX_PAGES`, which defaults to `1000`.

When sidecars are enabled, PDF conversion also writes a durable
`<output>.pages.json` manifest during extraction. The manifest is keyed by source SHA-256,
page count, and OCR configuration, and stores per-page status plus extracted text. OCR pages retain
`raw_text`, final `text`, and `corrected_text` when LLM correction changed the page, so maintainers
can audit raw-vs-corrected output after long runs. If conversion is retried with the same source and
OCR settings, completed pages are reused instead of OCRed again. Recursive conversion/import treats
these manifests as Librarian metadata and skips them. Existing page manifests are capped at 256 MiB
when read for resume or inspection.
Page manifests also carry `schema_version` and a top-level `summary` with lifecycle status, page
status/source/warning counts, retry attempts, OCR/correction counts, confidence, and max page
duration so operators can inspect large runs without scanning raw page text.
When `LIBRARIAN_OCR_PRESERVE_PAGE_IMAGES=true`, OCR page records also include `image_path` values
pointing at same-directory raster image artifacts for visual debugging.
Scanned pages are written as `pending` before OCR begins; failed pages retain the error, warning
codes, elapsed OCR duration, and retry `attempts`. Resumed extraction increments attempts while
replaying only unfinished or failed OCR pages.

Inspect a manifest without dumping raw page text:

```bash
librarian page-manifest ./out/report.md.pages.json --failures-only
librarian page-manifest ./out/report.md.pages.json --json --failures-only
```

The JSON view includes counts, confidence summary, retry attempts, OCR duration, and page
diagnostics without printing raw or corrected page text, plus the manifest `schema_version` and
top-level `summary` status for external automation, so it is safe to use in CI logs and operator
tickets. The CLI rejects page manifest paths that are symlinks or cross symlinked parents.
API deployments can inspect the same sidecar with
`GET /imports/page-manifest?manifest_path=/data/out/report.md.pages.json`. The path must be under
`LIBRARIAN_API_IMPORT_ROOT`; use `failures_only=true` to page through failed OCR records.
Manifest page records also include structured `warnings` codes such as `low-ocr-confidence`,
`missing-ocr-confidence`, `repeated-tail`, and `ocr-page-failed`.

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
`LIBRARIAN_OCR_LLM_CORRECTION=never` for fully deterministic OCR-only conversion. Set
`LIBRARIAN_OCR_LLM_CORRECTION=low-confidence` to correct only pages whose average word confidence
is below `LIBRARIAN_OCR_LOW_CONFIDENCE_THRESHOLD`; pages without confidence diagnostics are left
uncorrected in this mode.

Batch conversion sidecars include extraction metadata for page count, per-page source
(`embedded`, `ocr`, or `failed`), correction status, and OCR confidence when the extractor can
surface it. The larger page manifest contains the per-page raw/corrected OCR text artifacts.
Conversion outputs, sidecars, and PDF page manifests are written through atomic same-directory
replacements and reject output paths that are symlinks or cross symlinked parents. Recursive
conversion/import skips Librarian-generated sidecars and converted outputs.

## Large-PDF Test Recipe

For a real 500-1000 page PDF, run conversion first, then ingest and process the converted
Markdown:

```bash
export LIBRARIAN_OCR_PDF_MAX_PAGES=1000
export LIBRARIAN_PDF_MAX_PAGES=1000
export LIBRARIAN_OCR_PAGE_CONCURRENCY=2
export LIBRARIAN_OCR_PREPROCESS_MODE=deskew
export LIBRARIAN_OCR_THRESHOLD=180
export LIBRARIAN_OCR_LLM_CORRECTION=never

librarian convert ./massive.pdf --format md --output ./massive.md
librarian ingest ./massive.md
librarian process doc_...
librarian search "known phrase from the document"
```

Use `LIBRARIAN_OCR_LLM_CORRECTION=always` with real provider credentials when measuring final
quality instead of raw OCR throughput.

For a folder of PDFs, use `librarian import ./input --recursive --format md --process` instead.
