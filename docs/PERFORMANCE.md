# Performance Baselines

Performance varies heavily by provider, model, document type, OCR path, and concurrency settings.

## Local Mock Baseline

The mock provider is deterministic and measures framework overhead only:

```bash
librarian benchmark --input-path examples/benchmark_text.txt --repeats 3 --output bench-mock.json
librarian corpus-eval examples/corpus_eval_cases.json \
  --output-dir .librarian/corpus-eval \
  --output corpus-eval-mock.json \
  --overwrite
```

Use this to catch obvious regressions in chunking, orchestration, or serialization.

## Real Provider Baseline

Run this before release candidates with real credentials:

```bash
export LIBRARIAN_LLM_PROVIDER=openai-compatible
export LIBRARIAN_LLM_MODEL=gpt-4.1-mini
export OPENAI_API_KEY=...

librarian eval examples/eval_cases.json --output eval-provider.json
librarian benchmark --input-path examples/benchmark_text.txt --repeats 3 --output bench-provider.json
librarian corpus-eval examples/corpus_eval_cases.json \
  --output-dir .librarian/corpus-eval-provider \
  --output corpus-eval-provider.json \
  --overwrite
```

Record:

- model/provider/base URL
- `LIBRARIAN_LLM_MAX_CONCURRENCY`
- `LIBRARIAN_OCR_PAGE_CONCURRENCY`
- `LIBRARIAN_OCR_LLM_CORRECTION`
- chunk target and overlap
- coherence mode
- document page count and scanned-page count
- eval pass/fail
- corpus eval search recall, output character ratio, peak traced memory, and OCR confidence
- `/metrics` OCR pages/sec, OCR failure counts, corrected page counts, and conversion failures by
  type during API-driven imports
- average and fastest chars/sec

Do not commit provider outputs that contain private text. Sanitized benchmark JSON may be attached
to releases.

## Large PDF/OCR Baseline

For 500-1000 page PDFs, measure conversion and processing separately:

```bash
time librarian convert ./large.pdf --format md --output ./large.md
time librarian ingest ./large.md
time librarian process doc_...
```

Run once with `LIBRARIAN_OCR_LLM_CORRECTION=never` to isolate extraction/OCR throughput, then run
again with `always` and the intended provider/model to measure final correction quality and cost.
Record peak memory separately with the host's normal process monitor.

For repeatable non-private load testing, generate a synthetic corpus and run the end-to-end harness:

```bash
librarian generate-corpus \
  --output-dir .librarian/synthetic-corpus \
  --documents 20 \
  --paragraphs 800 \
  --paragraph-sentences 6 \
  --overwrite

librarian corpus-eval .librarian/synthetic-corpus/corpus_eval_cases.json \
  --output-dir .librarian/synthetic-corpus/converted \
  --output .librarian/synthetic-corpus/results.json \
  --overwrite
```

This does not replace real PDF/OCR baselines, but it gives deterministic chunking, processing,
search, and memory numbers for regression comparisons.

## Current Status

No real-provider baseline is committed for `v0.1.0a20` because this repository does not include
provider credentials. The harness and release checklist are in place so maintainers can generate and
attach sanitized baselines to later release candidates.
