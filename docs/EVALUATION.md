# Evaluation And Benchmarking

Librarian has two complementary harnesses:

- `librarian eval`: correctness checks over sanitized cases.
- `librarian benchmark`: throughput checks over synthetic or supplied text.

Both commands use the configured provider stack. CI uses `LIBRARIAN_LLM_PROVIDER=mock`; provider
benchmarks should be run manually with real credentials.

## Real Provider Run

```bash
export LIBRARIAN_LLM_PROVIDER=openai-compatible
export LIBRARIAN_LLM_MODEL=gpt-4.1-mini
export OPENAI_API_KEY=...

librarian eval examples/eval_cases.json --output eval-openai.json
librarian benchmark --input-path examples/benchmark_text.txt --repeats 3 --output bench-openai.json
```

For non-OpenAI compatible endpoints, also set `LIBRARIAN_LLM_BASE_URL` and
`LIBRARIAN_LLM_API_KEY_ENV`.

## Tuning Matrix

Run the same eval and benchmark suite while varying:

- `LIBRARIAN_CHUNK_TARGET_CHARS`: start with `8000`, `12000`, `18000`.
- `LIBRARIAN_CHUNK_OVERLAP_CHARS`: start with `400`, `800`, `1200`.
- `LIBRARIAN_LLM_MAX_CONCURRENCY`: start with `4`, `8`, `16`.
- `LIBRARIAN_COHERENCE_MODE`: compare `fast`, `balanced`, and `max-coherence`.

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
      "expected_classification_prefix": "800",
      "min_output_chars": 80
    }
  ]
}
```

Use sanitized text only. Do not commit private transcripts, customer documents, API responses, or
provider logs.
