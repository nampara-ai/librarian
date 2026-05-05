# Architecture

Librarian uses hexagonal architecture so the core can run through a CLI, an API service, tests, or future workers without changing business logic.

## Design Rules

- Domain code has no framework, database, filesystem, or model-provider imports.
- Application services depend on domain entities and ports.
- Infrastructure adapters implement ports.
- CLI and API adapters are thin request/response translators.
- Long-running work is represented as persisted processing runs, not transient function calls.
- Every generated output is traceable to source hash, chunk IDs, prompt version, model provider, model name, and pipeline version.

## Layers

```text
Adapters
  CLI: Typer
  API: FastAPI
  Storage: SQLite + filesystem content store
  LLM: OpenAI-compatible, mock
  Extraction: txt, md, csv, json, docx, pdf

Application
  IngestDocument
  ProcessDocument
  SearchLibrary
  ExportDocument

Domain
  Document
  SourceFile
  Chunk
  ProcessingRun
  CleanedOutput
  Classification
  Taxonomy

Ports
  DocumentRepository
  RunRepository
  ContentStore
  TextExtractor
  LLMProvider
  TaxonomyProvider
  SearchIndex
  EventSink
  RunQueue
```

## Fast Processing Model

The pipeline is a resumable DAG:

1. Extract source text.
2. Normalize text.
3. Build deterministic chunks.
4. Clean chunks concurrently where the selected coherence mode allows it.
5. Validate chunk outputs.
6. Assemble the document.
7. Classify and tag.
8. Index for search.

The default execution model should favor throughput:

- async LLM calls
- bounded provider concurrency
- chunk-result caching by source hash, prompt version, model, and chunk hash
- SQLite WAL mode
- immediate persistence after each stage
- event streaming for CLI/API progress

## Coherence Modes

- `fast`: parallel chunk cleaning with overlap windows and boundary smoothing.
- `balanced`: parallel chunk groups with local carry-forward inside each group.
- `max-coherence`: sequential carry-forward across the full document.

The prototype used a max-coherence style. Production defaults should start with `balanced` after benchmarking.

## Prompt Governance

Prompts live under `src/librarian/prompts`. Prompt text is versioned and recorded in run metadata. The cleaning prompt is intentionally ported from the prototype without wording changes until evals justify changes.

## Migrations

SQLite schema changes live in `src/librarian/storage/migrations` and are applied in filename order. Applied versions are recorded in `schema_migrations`.

## Jobs And Events

The API submits processing work through an application-level job runner instead of FastAPI
`BackgroundTasks`. The default runner is bounded and in-process for local use. Production
deployments can set `LIBRARIAN_JOB_BACKEND=sqlite` and run `librarian worker` as a separate
process. The SQLite queue uses leases, retry backoff, attempt limits, and persisted state so API
processes can restart independently of workers.

Run events can be fetched as JSON or streamed over server-sent events.

## Benchmarking

The `librarian benchmark` command uses deterministic synthetic text and the configured cleaner to measure chunking and cleaning throughput. This is the baseline harness for comparing chunking policies, coherence modes, providers, and concurrency settings.

## Evaluation

The `librarian eval` command runs JSON eval suites against the configured chunking, prompt, and
provider stack. Evals are intentionally file-based so contributors can add sanitized cases without
coupling the harness to private corpora. See `docs/EVALUATION.md` for provider tuning commands and
the baseline tuning matrix.
