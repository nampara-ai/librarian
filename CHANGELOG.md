# Changelog

## 0.1.0a1 - 2026-05-06

- Added OSS governance files, templates, Dependabot, and CodeQL.
- Added combined import manifests, resume mode, JSON reports, and run/queue controls.
- Added collision-safe conversion outputs, sidecar metadata, OCR language configuration, and
  conversion failure classification.
- Added API endpoints for imports, document deletion/reprocess, run cancel/retry, and run listing.
- Added release workflow support for GHCR images and SBOM artifacts.
- Initial production rewrite foundation.
- CLI and FastAPI surfaces over shared application services.
- SQLite persistence with append-only migrations.
- Durable optional SQLite run queue and external worker command.
- Prompt/versioned cleaning and Dewey classification stack.
- Export, search, benchmark, and eval commands.
