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
- chunk target and overlap
- coherence mode
- eval pass/fail
- average and fastest chars/sec

Do not commit provider outputs that contain private text. Sanitized benchmark JSON may be attached
to releases.

## Current Status

No real-provider baseline is committed for `v0.1.0a1` because this repository does not include
provider credentials. The harness and release checklist are in place so maintainers can generate and
attach sanitized baselines to later release candidates.
