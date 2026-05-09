# Performance Baselines

Performance varies heavily by provider, model, document type, OCR path, and concurrency settings.

## Local Mock Baseline

The mock provider is deterministic and measures framework overhead only:

```bash
librarian benchmark --input-path examples/benchmark_text.txt --repeats 3 --output bench-mock.json
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
- average and fastest chars/sec

Do not commit provider outputs that contain private text. Sanitized benchmark JSON may be attached
to releases.

## Large PDF/OCR Baseline

For 500-1000 page PDFs, measure conversion and processing separately:

```bash
time librarian convert ./large.pdf --format md --output ./large.md
time librarian import ./large.md --format md --process
```

Run once with `LIBRARIAN_OCR_LLM_CORRECTION=never` to isolate extraction/OCR throughput, then run
again with `always` and the intended provider/model to measure final correction quality and cost.
Record peak memory separately with the host's normal process monitor.

## Current Status

No real-provider baseline is committed for `v0.1.0a2` because this repository does not include
provider credentials. The harness and release checklist are in place so maintainers can generate and
attach sanitized baselines to later release candidates.
