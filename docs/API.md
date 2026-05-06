# API Surface

The FastAPI service exposes the same core workflows as the CLI.

## Documents

- `POST /documents`: upload one file. Uploads are capped by
  `LIBRARIAN_API_MAX_UPLOAD_BYTES`.
- `GET /documents?limit=100&offset=0`: list documents.
- `GET /documents/{id}`: document metadata.
- `DELETE /documents/{id}`: delete a document and dependent records.
- `POST /documents/{id}/reprocess`: create a new processing run.
- `GET /documents/{id}/content`: latest cleaned output.
- `GET /documents/{id}/export?format=json|txt|md`: export latest output.

## Imports

- `POST /imports`: convert a server-local directory, ingest outputs, and optionally process or
  queue runs.

Example:

```json
{
  "source_dir": "/data/input",
  "format": "md",
  "output_mode": "subdirectory",
  "recursive": true,
  "overwrite": false,
  "processing_mode": "queue"
}
```

`processing_mode` may be `none`, `process`, or `queue`.

## Runs

- `POST /runs`: create a processing run for one document.
- `GET /runs?limit=100&offset=0`: list runs.
- `GET /runs/{id}`: run status.
- `POST /runs/{id}/cancel`: mark a run canceled.
- `POST /runs/{id}/retry`: replay a failed run as a new run.
- `GET /runs/{id}/events`: run events.
- `GET /runs/{id}/events/stream`: server-sent event stream.

## Search And Metadata

- `POST /search`: full-text search over cleaned outputs.
- `GET /classifications`: built-in Dewey labels.
- `GET /config`: selected runtime settings.
- `GET /health`: health check.
- `GET /metrics`: process-local request metrics.

## CLI Parity

- `librarian import` maps to `POST /imports`.
- `librarian process` maps to `POST /runs`.
- `librarian run-cancel` maps to `POST /runs/{id}/cancel`.
- `librarian run-retry` maps to `POST /runs/{id}/retry`.
- `librarian search` maps to `POST /search`.
- `librarian export` maps to `GET /documents/{id}/export`.
