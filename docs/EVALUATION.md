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
`name`, non-empty `input_text`, and positive `min_output_chars`.

Eval cases fail on unexpected cleaned-output quality warnings by default. Warnings cover likely
rendering regressions such as lost Markdown headings/lists/tables, missing citation markers,
collapsed paragraphs, context-marker leaks, malformed Markdown tables, orphan list markers, and
assistant artifacts. Add `allowed_warnings` only when a fixture intentionally exercises a known
degradation. Use `forbidden_contains` to fail prompt evals when provider output includes known
summarization phrases, context markers, or assistant artifacts.

Use `min_output_char_ratio` and `max_output_char_ratio` for provider-backed prompt evals that must
preserve source fidelity. Ratio bounds catch aggressive summarization, accidental truncation, and
runaway expansion while still allowing normal cleanup edits.

Eval JSON results include Librarian version, generation timestamp, provider/model settings, prompt
versions, aggregate pass/throughput/warning/failure summary metrics, and per-case classification,
warning, output character ratio, and failure details.

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
      "expected_search_phrases": ["canter transitions"],
      "expected_classification_prefix": "636",
      "expected_page_count": 12,
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
`max_conversion_seconds`, `max_processing_seconds`, and `max_peak_memory_bytes` budgets must be
positive when set; budget overruns fail the case.

The JSON result records Librarian version, generation timestamp, provider/model settings, prompt
versions, aggregate pass/failure/search/size/OCR/memory summary metrics, conversion time,
processing time, peak traced memory, output character ratio, page count, per-page extraction source
counts, OCR page count, corrected OCR page count, average OCR confidence, search recall, per-phrase
search diagnostics with total matches and returned document IDs, and classification result.
Local `docs/results/` outputs are ignored by Git and Docker build context; commit only sanitized
summaries intentionally, or attach large generated artifacts to the release.

## Synthetic Large Corpus

The repository includes a small sanitized fixture set at
`examples/synthetic-corpus/corpus_eval_cases.json`. CI runs it end-to-end to cover Markdown,
DOCX with tables/headers/footers, and embedded-text PDF extraction with search/classification
checks. Generate larger local suites when measuring scale or release-candidate performance.

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
  --overwrite

librarian corpus-eval .librarian/synthetic-corpus/corpus_eval_cases.json \
  --output-dir .librarian/synthetic-corpus/converted \
  --output .librarian/synthetic-corpus/results.json \
  --overwrite
```

Scale `--documents`, `--paragraphs`, and `--paragraph-sentences` to approximate larger page counts.
Use `--include-docx` to add sanitized DOCX fixtures with body paragraphs, tables, headers, and
footers, and `--include-pdf` to add embedded-text PDF fixtures with expected page counts. The
generated cases include stable search phrases and classification expectations so results are
comparable across commits and provider configurations.
