# Librarian Production PRD

## 1. Summary

Librarian is an open-source, local-first knowledge organization system for turning messy source material into a structured personal library. It ingests transcripts, notes, documents, and exports; cleans and normalizes the text; classifies material using library-style taxonomy such as Dewey Decimal; stores source provenance and generated outputs; and exposes the system through both a command-line interface and a web/API service from day one.

The first production release should preserve the prototype's strongest idea: give users a pile of rough material and produce a navigable library with cleaned documents, summaries, classifications, metadata, and search. The production version should not preserve the prototype's implementation shape. It should be a clean package with explicit boundaries, reliable processing runs, testable pipeline stages, provider-agnostic LLM integration, and privacy-conscious defaults.

## 2. Goals

- Support both local CLI use and hosted/API use with the same core engine.
- Process messy transcripts and documents into cleaned, classified, searchable library records.
- Preserve source fidelity: cleaning should improve readability without summarizing away content.
- Track provenance for every generated artifact, including source path, source hash, prompt version, model provider, model name, pipeline version, and timestamps.
- Support long files through deterministic chunking, resumable processing, retries, and validation.
- Make the system open-source friendly: simple install, clear docs, clean license, test suite, no private sample data.
- Keep user data local by default unless a user explicitly configures remote storage or a hosted deployment.
- Make LLM providers replaceable, mockable, and configurable.

## 3. Non-Goals

- Do not build a full multi-user SaaS product in the initial release.
- Do not build a complex frontend before the API and CLI are stable.
- Do not support every document type on day one.
- Do not depend on Dewey Decimal as the only possible taxonomy.
- Do not require users to use a specific LLM vendor.
- Do not include private transcripts, generated personal outputs, or prototype runtime state in the public repo.
- Do not keep ORION-specific presentation/protobuf code unless it is explicitly re-scoped as part of Librarian.

## 4. Target Users

Primary users:

- Researchers, creators, educators, writers, consultants, and operators with large bodies of messy notes or transcripts.
- Technical users who want a local CLI workflow.
- Developers who want an API service for automation or integration into another product.

Secondary users:

- Teams that want a self-hosted document processing service.
- Open-source contributors interested in document pipelines, personal knowledge systems, and LLM-assisted archival tools.

## 5. Product Surfaces

### 5.1 CLI

The CLI is the fastest path for local-first use. It should expose the same core pipeline as the service layer.

Required commands:

- `librarian init`: create a local library workspace.
- `librarian ingest <path>`: ingest one file or directory.
- `librarian process <document-id | path>`: run cleaning, classification, and indexing.
- `librarian batch <path>`: process all supported files in a directory.
- `librarian status [run-id]`: inspect processing status.
- `librarian list`: list library documents.
- `librarian show <document-id>`: show metadata and output paths.
- `librarian search <query>`: search cleaned documents and metadata.
- `librarian export <document-id>`: export cleaned text and metadata.
- `librarian config`: inspect effective configuration.

Useful later commands:

- `librarian reprocess <document-id>` with a new prompt/model/pipeline version.
- `librarian classify <document-id>` for classification-only workflows.
- `librarian clean <document-id>` for cleaning-only workflows.
- `librarian doctor` for environment and provider diagnostics.

### 5.2 API Service

The API service should be production-structured from day one, even if authentication and scaling remain minimal initially. The API should call the same application services used by the CLI.

Required endpoints:

- `GET /health`: service health.
- `GET /version`: app, schema, and pipeline version.
- `POST /documents`: upload or register a document.
- `GET /documents`: list documents.
- `GET /documents/{id}`: retrieve metadata.
- `GET /documents/{id}/content`: retrieve cleaned content.
- `POST /runs`: start a processing run.
- `GET /runs/{id}`: inspect run status.
- `GET /runs/{id}/events`: stream or poll run events.
- `POST /search`: search the library.
- `GET /classifications`: list known taxonomy categories.

Initial service assumptions:

- Single-user or trusted-network deployment by default.
- API key auth optional for local development, required for network exposure.
- File uploads are stored in a configured library data directory.
- Long-running jobs are persisted and resumable.

### 5.3 Web UI

A web UI is useful, but it should not lead the architecture. Initial UI can be thin and API-backed.

Initial UI views:

- Document list with status, category, and processed time.
- Document detail with source metadata, summary, cleaned output, and provenance.
- Processing runs view with logs/events.
- Search view.
- Settings view for model provider and library paths.

## 6. Core Workflows

### 6.1 Local Batch Processing

1. User runs `librarian init`.
2. User places files in a folder or points CLI at a directory.
3. Librarian discovers supported files.
4. Librarian extracts text and stores a source record.
5. Librarian chunks the text deterministically.
6. Librarian cleans each chunk with context carry-forward.
7. Librarian validates chunk outputs.
8. Librarian assembles the cleaned document.
9. Librarian classifies the document.
10. Librarian stores metadata, content, search index, and run logs.
11. User searches or exports the cleaned library.

### 6.2 API Processing

1. Client uploads a file with `POST /documents`.
2. Client starts a run with `POST /runs`.
3. Service queues or starts the run.
4. Client polls `GET /runs/{id}` or subscribes to `GET /runs/{id}/events`.
5. Service persists intermediate state after each stage.
6. Client retrieves final metadata and content.

### 6.3 Reprocessing

1. User selects an existing document.
2. User chooses a new model, prompt version, taxonomy, or pipeline version.
3. Librarian creates a new run without overwriting prior outputs.
4. User can compare run outputs.

## 7. Functional Requirements

### 7.1 Document Ingestion

MVP supported input types:

- `.txt`
- `.md`
- `.csv`
- `.json`
- `.docx`
- `.pdf` with extractable text

Later input types:

- HTML
- EPUB
- audio transcript formats such as `.srt` and `.vtt`
- OCR image/PDF workflows

Ingestion requirements:

- Compute source file hash.
- Preserve original filename and media type.
- Store extracted raw text separately from cleaned output.
- Avoid duplicate ingestion unless explicitly requested.
- Record extraction warnings and failures.

### 7.2 Text Cleaning

Requirements:

- Remove speech-to-text noise while preserving meaning.
- Keep original voice and content flow.
- Avoid unwanted summarization.
- Support domain vocabulary correction through optional glossaries.
- Support prompt versioning.
- Validate output for common LLM artifacts.
- Support deterministic chunk IDs and resumable chunk processing.

Cleaning modes:

- `light`: punctuation, capitalization, filler cleanup.
- `standard`: default transcript cleanup.
- `editorial`: stronger prose cleanup while preserving meaning.
- `verbatim-plus`: minimal cleanup for legal/interview/research sensitivity.

### 7.3 Classification

Requirements:

- Assign a primary classification code and human-readable label.
- Store classification confidence or rationale when available.
- Support Dewey Decimal defaults.
- Allow custom taxonomies.
- Allow forced category assignment.
- Support later reclassification without changing source or cleaned content.

MVP taxonomy:

- Bundled Dewey-inspired taxonomy with pragmatic categories.
- User-extensible taxonomy file.

### 7.4 Library Storage

Requirements:

- Local-first SQLite database by default.
- Store documents, source files, extracted text metadata, chunks, runs, outputs, classifications, tags, and events.
- Store large content either in SQLite or content-addressed files, with the decision hidden behind a repository interface.
- Support schema migrations.
- Support full-text search over cleaned content and metadata.

### 7.5 Search

MVP search:

- Keyword search using SQLite FTS.
- Filter by classification, source filename, tags, date, and processing status.
- Return snippets and document IDs.

Later search:

- Embedding search.
- Hybrid keyword/vector search.
- Similar document detection.

### 7.6 Processing Runs

Requirements:

- Every pipeline execution creates a run record.
- Runs are append-only.
- Runs can be resumed after interruption.
- Runs expose stage-level status.
- Runs store structured events and user-readable logs.
- Failed runs preserve error details.

Run stages:

- `ingest`
- `extract`
- `normalize`
- `chunk`
- `clean`
- `validate`
- `classify`
- `assemble`
- `index`
- `complete`

### 7.7 LLM Provider Layer

Requirements:

- Provider-agnostic interface.
- OpenAI-compatible provider support.
- DeepSeek support through OpenAI-compatible configuration.
- Mock provider for tests.
- Clear handling of rate limits, timeouts, retries, and model errors.
- Configurable model, base URL, API key environment variable, max tokens, temperature, and timeout.
- No provider secrets stored in the database.

### 7.8 Configuration

Configuration sources, highest precedence first:

- CLI flags
- environment variables
- project config file
- user config file
- defaults

MVP config keys:

- library data directory
- database path
- model provider
- model name
- API key environment variable
- base URL
- cleaning mode
- taxonomy path
- log level
- API host/port
- API auth mode

### 7.9 Observability

Requirements:

- Structured logs.
- Progress events for CLI and API.
- User-readable failure messages.
- Debug logs that do not leak API keys.
- Optional trace IDs for API requests and processing runs.

## 8. Non-Functional Requirements

Reliability:

- Chunk-level retries.
- Resume interrupted runs.
- Do not lose source records after failed processing.
- Do not overwrite prior outputs without explicit user action.

Privacy:

- Local storage by default.
- No telemetry by default.
- Clear docs explaining which content is sent to model providers.
- `.env` and local data ignored by Git.
- Sample data must be synthetic or explicitly licensed.

Security:

- Never log secrets.
- API auth available from day one.
- Restrict uploaded file paths to configured data directories.
- Validate file types and request sizes.
- Avoid arbitrary file reads through user-controlled paths in service mode.

Performance:

- Handle small documents interactively.
- Handle large documents through chunking and resumable runs.
- Avoid loading entire libraries into memory.
- Use streaming or polling for long run status.

Maintainability:

- Typed code.
- Clear module boundaries.
- Unit tests for deterministic behavior.
- Integration tests with mock providers.
- CI for linting, typing, and tests.

Portability:

- Works on macOS, Linux, and Docker.
- Python package installable with `pipx` or `uv tool`.
- Docker image for API service.

## 9. Architecture

### 9.1 Recommended Shape

Use a layered architecture:

- `librarian_core`: domain models, pipeline, storage interfaces, taxonomy, provider contracts.
- `librarian_cli`: CLI adapter.
- `librarian_api`: HTTP adapter.
- `librarian_workers`: background job execution, initially in-process.

The CLI and API should not contain business logic. They should call application services from the core package.

### 9.2 Proposed Python Package Layout

```text
src/librarian/
  __init__.py
  cli.py
  config.py
  logging.py
  models.py
  api/
    app.py
    auth.py
    routes.py
    schemas.py
  ingest/
    extractors.py
    registry.py
  llm/
    base.py
    openai_compatible.py
    mock.py
  pipeline/
    chunking.py
    cleaning.py
    classification.py
    validation.py
    assembly.py
    runner.py
  storage/
    database.py
    migrations/
    repositories.py
  taxonomy/
    dewey.py
    custom.py
  search/
    fts.py
tests/
```

### 9.3 Data Model

Core entities:

- `Document`: logical library item.
- `SourceFile`: original file metadata and hash.
- `ExtractedText`: raw extracted content metadata.
- `Chunk`: deterministic segment of extracted text.
- `ProcessingRun`: one execution of the pipeline.
- `RunEvent`: structured status/log event.
- `CleanedOutput`: generated cleaned document.
- `Classification`: taxonomy result.
- `Tag`: searchable label.

### 9.4 Job Execution

MVP:

- In-process job runner.
- API starts jobs in-process with persisted run state.
- CLI runs jobs synchronously by default.

Later:

- Optional external queue such as Redis/RQ, Dramatiq, Celery, or cloud queues.
- Worker process can be deployed separately.

## 10. API Contract Sketch

### Create Document

`POST /documents`

Inputs:

- multipart file upload, or
- JSON body with local path when service is explicitly configured to allow local path registration.

Output:

```json
{
  "id": "doc_...",
  "filename": "transcript.txt",
  "status": "ingested"
}
```

### Start Run

`POST /runs`

```json
{
  "document_id": "doc_...",
  "cleaning_mode": "standard",
  "taxonomy": "dewey",
  "model": "configured-default"
}
```

Output:

```json
{
  "id": "run_...",
  "status": "queued"
}
```

### Run Status

`GET /runs/{id}`

```json
{
  "id": "run_...",
  "document_id": "doc_...",
  "status": "running",
  "stage": "clean",
  "completed_chunks": 12,
  "total_chunks": 30
}
```

## 11. Release Plan

### Milestone 0: Repo Sanitation

- Initialize Git repo.
- Remove local/generated/private artifacts from tracked scope.
- Add `.gitignore`, `.dockerignore`, license, README skeleton, and `.env.example`.
- Decide project name/package name.

### Milestone 1: Core Package

- Add `pyproject.toml`.
- Add config system.
- Add typed domain models.
- Add SQLite storage and migrations.
- Add text extraction for MVP file types.
- Add deterministic chunking and validation tests.

### Milestone 2: LLM Pipeline

- Add provider abstraction.
- Add OpenAI-compatible provider.
- Add mock provider.
- Add cleaning, classification, assembly, and run tracking.
- Add integration tests with mock provider.

### Milestone 3: CLI

- Add `init`, `ingest`, `process`, `batch`, `status`, `list`, `show`, `search`, and `export`.
- Add readable progress output.
- Add docs for local use.

### Milestone 4: API Service

- Add FastAPI service.
- Add document upload, run creation, run status, content retrieval, and search.
- Add API key auth.
- Add Dockerfile and compose example.

### Milestone 5: Public Open Source Release

- Add CI.
- Add example synthetic fixtures.
- Add contributor docs.
- Add security/privacy docs.
- Tag `v0.1.0a1` for the first public alpha.

## 12. Success Metrics

For `v0.1.0a1`:

- A user can install and run Librarian locally from a clean machine.
- A user can process a directory of text/Markdown/PDF/DOCX files from the CLI.
- A user can run the API service in Docker and process a document through HTTP.
- Failed runs are inspectable and do not corrupt the library.
- The repository contains no private data, generated dependencies, local virtualenvs, or secrets.
- CI passes linting and tests.

## 13. Open Questions

- Should the public package name be `librarian`, `librarian-ai`, or something more unique?
- Should the default taxonomy be true Dewey, Dewey-inspired, or explicitly "library classification" to avoid overclaiming precision?
- Should cleaned document content live in SQLite, content-addressed files, or both?
- Is multi-user auth needed for the first hosted deployment, or is single-tenant API key auth enough?
- Should the first web UI ship with a later alpha, or should it follow once CLI/API contracts
  stabilize?
- Which model providers should be documented as first-class at launch?

## 14. Runtime Recommendation

Recommendation: use Python for the core, CLI, and API.

Python is the best fit because the product is document extraction, text processing, SQLite, CLI tooling, and LLM orchestration. The ecosystem is strong for PDFs, DOCX, text processing, FastAPI, Typer, Pydantic, SQLAlchemy, Alembic, pytest, and OpenAI-compatible clients. The current prototype is already Python, so the useful behavior can be ported without a language migration tax.

Use TypeScript only if a substantial web frontend becomes part of the product. In that case, keep TypeScript in the frontend and possibly a thin SDK, not as the core processing engine.

Avoid Go or Rust for the initial version. They would produce nice binaries, but the document/LLM ecosystem friction is not worth it this early. A later Rust helper for performance-sensitive extraction or packaging is reasonable, but not for the first production rewrite.

Recommended stack:

- Python 3.12+
- Typer for CLI
- FastAPI for API
- Pydantic for settings and API schemas
- SQLite with SQLAlchemy/Alembic
- pytest, ruff, and pyright or mypy
- Docker for service deployment
- Optional simple web UI later with React/TypeScript or server-rendered templates
