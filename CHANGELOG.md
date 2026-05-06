# Changelog

## Unreleased

- Fixed Docker runtime data directory permissions.
- Hardened public API binds to require authentication and an import root.
- Made canceled runs terminal and durable workers resilient to failed jobs.
- Scoped chunk IDs to documents while preserving content-hash cache reuse.
- Avoided conversion work for resumed import manifest entries.
- Prevented failed extraction attempts from persisting valid-looking documents.
- Returned controlled errors for malformed SQLite FTS search queries.
- Marked prerelease tags as prereleases in release automation.

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
