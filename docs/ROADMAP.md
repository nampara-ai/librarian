# Roadmap

## v0.1.0a1

- Local CLI and FastAPI service.
- Universal conversion to Markdown/plain text.
- Tesseract OCR for images and scanned PDFs.
- Batch import with manifest/resume and JSON reports.
- SQLite-backed persistence and worker queue.
- Docker Compose deployment.
- Eval and benchmark harnesses.

## v0.2

- Richer retrieval beyond SQLite FTS.
- Batch import API hardening for hosted deployments.
- OpenTelemetry/Prometheus adapter.
- More real-world conversion fixtures.
- Provider-specific performance baselines.

## v0.3

- Multi-user hosted service support.
- Stronger auth model: users, tokens, RBAC, tenant boundaries.
- Networked queue/database adapter for horizontal deployments.
- Admin UI for imports, runs, failures, and exports.

## Later

- Plugin system for custom taxonomies and conversion adapters.
- Native desktop packaging.
- Managed cloud deployment template.
