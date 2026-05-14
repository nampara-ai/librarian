# Evaluation And Benchmarking

Librarian has two complementary harnesses:

- `librarian eval`: correctness checks over sanitized cases.
- `librarian benchmark`: throughput checks over synthetic or supplied text.
- `librarian corpus-eval`: end-to-end conversion/import/process/search checks over sanitized
  source files.

Both commands use the configured provider stack. CI uses `LIBRARIAN_LLM_PROVIDER=mock` and runs the
shipped benchmark text plus `examples/corpus_eval_cases.json` as end-to-end CLI smoke tests;
provider benchmarks should be run manually with real credentials.
When `--output` is used, eval, benchmark, corpus-eval, and export result files are written through
atomic same-directory replacements and output paths that are symlinks or cross symlinked parents are
rejected.
`librarian corpus-eval` also rejects `--output-dir` values that are symlinks or cross symlinked
parents before writing converted artifacts.
`generate-corpus` uses the same atomic replacement behavior for generated corpus files and rejects
output paths that are symlinks or cross symlinked parents.
Eval suite JSON files are capped at 10 MiB, corpus-eval JSON metadata is capped at 10 MiB, and
benchmark `--input-path` files are capped at 100 MiB. Benchmark repeat counts and synthetic input
dimensions must be positive both in the CLI and the application harness.
Benchmark JSON results include Librarian version, generation timestamp, cleaning prompt version,
aggregate throughput/size/chunk summary metrics, and per-run timing details.
Release-candidate evidence verification recomputes benchmark aggregate input size, chunk count,
total duration, average throughput, and fastest throughput from the per-run records. It also
cross-checks per-record derived metrics, including eval and corpus output character ratios, corpus
search recall, and benchmark run throughput, against the raw size, timing, and search diagnostics.
CI and release builds require at least six prompt-eval cases and verify that the per-case tags cover
the shipped transcript, legal, technical, Markdown, OCR-correction, classification, and
no-summarization fixtures so risk coverage cannot be accidentally dropped.
They also require the corpus-eval evidence tags for DOCX tables/headers, embedded PDFs, scanned OCR
PDFs, noisy OCR, mixed embedded/scanned PDFs, and SRT/VTT caption transcripts.

## Real Provider Run

```bash
export LIBRARIAN_LLM_PROVIDER=openai-compatible
export LIBRARIAN_LLM_MODEL=gpt-4.1-mini
export OPENAI_API_KEY=...

librarian eval examples/eval_cases.json --output eval-openai.json
librarian benchmark --input-path examples/benchmark_text.txt --repeats 3 --output bench-openai.json
librarian corpus-eval examples/corpus_eval_cases.json \
  --output-dir .librarian/corpus-eval \
  --output corpus-eval-openai.json \
  --overwrite
```

For non-OpenAI compatible endpoints, also set `LIBRARIAN_LLM_BASE_URL` and
`LIBRARIAN_LLM_API_KEY_ENV`.

## Opt-In Provider Tests

CI does not call hosted providers. Maintainers can run the real-provider integration tests before a
release candidate:

```bash
export LIBRARIAN_RUN_PROVIDER_TESTS=1
export LIBRARIAN_LLM_PROVIDER=openai-compatible
export LIBRARIAN_LLM_MODEL=gpt-4.1-mini
export OPENAI_API_KEY=...

uv run pytest tests/test_provider_integration.py
```

These tests exercise provider-backed cleaning/classification behavior, prompt provenance on stored
outputs, and low-confidence OCR correction. Separately, normal CI covers missing API key fast-fail
and transient retry behavior without network access.

## Tuning Matrix

Run the same eval and benchmark suite while varying:

- `LIBRARIAN_CHUNK_TARGET_CHARS`: start with `8000`, `12000`, `18000`.
- `LIBRARIAN_CHUNK_OVERLAP_CHARS`: start with `400`, `800`, `1200`.
- `LIBRARIAN_LLM_MAX_CONCURRENCY`: start with `4`, `8`, `16`.
- `LIBRARIAN_COHERENCE_MODE`: compare `fast`, `balanced`, and `max-coherence`.
- `LIBRARIAN_CLEANING_PROMPT_VERSION`: compare bundled prompts `cmos_v1` and `cmos_v2`.
- `LIBRARIAN_CLASSIFICATION_PROMPT_VERSION`: compare bundled prompts `dewey_v1` and `dewey_v2`.

Promote a configuration only when evals pass and benchmark throughput improves on the same input
text. Keep raw JSON results with the commit or release candidate being tuned.

## Eval Case Format

```json
{
  "cases": [
    {
      "name": "classify literature seminar transcript",
      "tags": ["classification", "literature"],
      "input_text": "Professor: This novel uses recurring imagery...",
      "expected_contains": ["novel"],
      "forbidden_contains": ["summary:"],
      "expected_classification_prefix": "800",
      "min_output_chars": 80,
      "min_output_char_ratio": 0.7,
      "max_output_char_ratio": 1.8,
      "allowed_warnings": []
    }
  ]
}
```

Use sanitized text only. Do not commit private transcripts, customer documents, API responses, or
provider logs. Eval suites must contain at least one case, and each case must have a non-empty
`name`, non-empty `input_text`, and positive `min_output_chars`. The shipped prompt suite covers
classification, no-summarization, Markdown structure preservation, and OCR correction risks.

Eval cases fail on unexpected cleaned-output quality warnings by default. Warnings cover likely
rendering regressions such as lost Markdown headings/lists/tables, missing citation markers,
collapsed paragraphs, context-marker leaks, malformed Markdown tables, orphan list markers, and
assistant artifacts. Add `allowed_warnings` only when a fixture intentionally exercises a known
degradation. Use `forbidden_contains` to fail prompt evals when provider output includes known
summarization phrases, context markers, or assistant artifacts.

Use `min_output_char_ratio` and `max_output_char_ratio` for provider-backed prompt evals that must
preserve source fidelity. Ratio bounds catch aggressive summarization, accidental truncation, and
runaway expansion while still allowing normal cleanup edits.

Eval JSON results include an `artifact_type`, an `evidence_tier` (`mock-smoke` or
`real-provider`), Librarian version, generation timestamp, provider/model settings, prompt
versions, aggregate pass/throughput/warning/failure summary metrics, and per-case classification,
warning, output character ratio, and failure details. Release-candidate evidence verification
rejects artifacts whose tier does not match the recorded provider or whose summary counts do not
match the per-case and per-run details. It also rejects incomplete detail records, including missing
case names, tags, warnings, classification results, positive size/timing metrics, corpus page/search
diagnostics, benchmark model names, chunk counts, and throughput measurements. Prompt-eval summary
size, throughput, warning, and failure metrics are recomputed from per-case records by the release
evidence verifier, and per-case output ratios and throughput must match the recorded input/output
sizes and durations.

## Corpus Eval Format

Corpus eval cases point at real sanitized source files, relative to the suite JSON:

```json
{
  "cases": [
    {
      "name": "markdown transcript conversion and search",
      "source_path": "corpus/markdown-transcript.md",
      "tags": ["markdown", "transcript", "search"],
      "format": "md",
      "process": true,
      "expected_contains": ["canter transitions"],
      "expected_text_order": [
        {"before": "warm-up notes", "after": "canter transitions"}
      ],
      "expected_search_phrases": ["canter transitions"],
      "expected_classification_prefix": "636",
      "expected_page_count": 12,
      "expected_page_source_counts": {"embedded": 10, "ocr": 2},
      "min_ocr_pages": 2,
      "min_corrected_pages": 1,
      "min_output_char_ratio": 0.5,
      "max_output_char_ratio": 2.0,
      "max_conversion_seconds": 30,
      "max_processing_seconds": 60,
      "max_peak_memory_bytes": 500000000,
      "require_markdown_headings": true
    }
  ]
}
```

Corpus eval suites must contain at least one case. Each case must have a non-empty `name`,
`expected_page_count` must be positive when provided, and output character ratio bounds must be
non-negative with `max_output_char_ratio >= min_output_char_ratio`. Optional
`expected_page_source_counts` values must be non-negative and are checked exactly against the
conversion sidecar page-source summary. Optional `min_ocr_pages`, `min_corrected_pages`,
`max_conversion_seconds`, `max_processing_seconds`, and `max_peak_memory_bytes` budgets must be
positive when set, except OCR minimums may be zero; budget overruns fail the case.
`expected_text_order` checks converted output after whitespace normalization and can be used to
catch reading-order regressions in transcripts, multi-column documents, and OCR output.

The JSON result records Librarian version, generation timestamp, provider/model settings, prompt
versions, aggregate pass/failure/search/size/OCR/memory summary metrics, conversion time,
processing time, peak traced memory, output character ratio, page count, per-page extraction status
and source counts, OCR warning counts, retry attempts, max page duration, OCR page count, corrected
OCR page count, average OCR confidence, search recall, per-phrase search diagnostics with total
matches and returned document IDs, and classification result.
Local `docs/results/` outputs are ignored by Git and Docker build context; commit only sanitized
summaries intentionally, or attach large generated artifacts to the release.

## Synthetic Large Corpus

The repository includes a small sanitized fixture set at
`examples/synthetic-corpus/corpus_eval_cases.json`. CI runs it end-to-end to cover Markdown,
DOCX with tables/headers/footers, embedded-text PDFs, scanned OCR PDFs, and mixed
embedded/scanned PDFs, and SRT/VTT caption transcripts with search/classification checks. The OCR fixtures are synthetic page
images, so they exercise Tesseract and page-source accounting without committing private scans.
PDF cases assert expected embedded/OCR page-source counts, so a regression that silently skips
scanned pages fails the suite instead of only changing diagnostic output.
Generate larger local suites when measuring scale or release-candidate performance.

Use `generate-corpus` to create deterministic sanitized long-document fixtures without committing
private or large source files:

```bash
librarian generate-corpus \
  --output-dir .librarian/synthetic-corpus \
  --documents 10 \
  --paragraphs 500 \
  --paragraph-sentences 6 \
  --include-docx \
  --include-pdf \
  --include-scanned-pdf \
  --include-noisy-ocr-pdf \
  --include-transcript-captions \
  --overwrite

librarian corpus-eval .librarian/synthetic-corpus/corpus_eval_cases.json \
  --output-dir .librarian/synthetic-corpus/converted \
  --output .librarian/synthetic-corpus/results.json \
  --overwrite
```

Scale `--documents`, `--paragraphs`, and `--paragraph-sentences` to approximate larger page counts.
Use `--include-docx` to add sanitized DOCX fixtures with body paragraphs, tables, headers, and
footers, `--include-pdf` to add embedded-text PDF fixtures with expected page counts, and
`--include-scanned-pdf` to add image-only and mixed embedded/scanned PDF fixtures that exercise OCR
page recovery. Add `--include-noisy-ocr-pdf` to include a deterministic mildly skewed, speckled,
low-contrast scanned page for OCR quality regressions. Add `--include-transcript-captions` to include
SRT/VTT caption fixtures with timestamp, cue-markup, and reading-order expectations. The generated cases include stable search
phrases and classification expectations so results are comparable across commits and provider
configurations.
