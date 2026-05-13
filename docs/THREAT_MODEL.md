# Threat Model

This document covers the local-first alpha deployment model: one operator-controlled Librarian
instance, local SQLite storage, optional API exposure, and optional OpenAI-compatible provider calls.

## Assets

- Source documents, converted Markdown/text, cleaned outputs, OCR page manifests, and eval results.
- Provider API keys, `LIBRARIAN_API_KEY`, and `LIBRARIAN_API_KEYS` values.
- SQLite database and upload/import directories.
- Release artifacts, container images, SBOMs, and provenance attestations.

## Trust Boundaries

- CLI users are trusted local operators.
- API callers are untrusted unless they present the configured API key.
- Files under `LIBRARIAN_API_IMPORT_ROOT` are operator-staged input, but their contents are still
  untrusted.
- LLM providers are external services. Source text is sent to them only for processing or OCR
  correction modes that require LLM work.
- Generated sidecars, reports, and page manifests are internal metadata and must not be treated as
  user-authored corpus input.

## API Import Threats

- Path traversal or symlink escapes from the import root.
  Mitigation: API import paths are resolved and constrained to `LIBRARIAN_API_IMPORT_ROOT`; recursive
  discovery skips files whose resolved path leaves the allowed root.
- Import output recursion or self-ingestion.
  Mitigation: conversion output directories and Librarian metadata sidecars/reports are excluded from
  discovery.
- Overwriting unrelated operator files through import manifests.
  Mitigation: manifest paths must be `.json`, must stay under the import root, and existing manifest
  files must be Librarian import reports.
- Archive bombs, malware, or unsafe recursive unpacking.
  Mitigation: archive/container formats are rejected by default, and API uploads reject known
  archive extensions before writing upload bytes. Operators should scan and unpack archives outside
  Librarian with approved malware tooling.
- Unauthorized hosted access.
  Mitigation: public API binds require `LIBRARIAN_API_KEY` or `LIBRARIAN_API_KEYS`, plus
  `LIBRARIAN_API_IMPORT_ROOT`; requests use constant-time API-key comparison, and deployments can
  keep multiple keys active during rotation. Multi-user auth and tenant isolation are future
  hosted-mode work.

## Data Leakage Threats

- Source text in logs.
  Mitigation: run-stage logs carry IDs, stages, status, and durations, not source or generated text.
  Persisted run/queue error strings are redacted and length-capped before they are exposed through
  status APIs. Unhandled API exceptions return a generic `server_error` response instead of
  exposing internal exception text.
- Credential leakage in logs.
  Mitigation: JSON and plain-text logging redact common API-key assignments, bearer tokens, and
  `sk-...` tokens.
- Secrets committed to the repository.
  Mitigation: CI runs Gitleaks on push, pull request, scheduled, and manual workflows. Maintainers
  also run `gitleaks detect --source . --redact --verbose` before release candidates.
- Private eval or provider output committed as benchmark evidence.
  Mitigation: benchmark artifacts must be sanitized before attaching to releases or committing.
  Local `docs/results/` outputs are ignored by Git and Docker build context by default.

## Availability Threats

- Large files exhausting memory or disk.
  Mitigation: configurable max input/upload sizes, PDF page limits, OCR page limits, and bounded LLM
  concurrency.
- Long OCR jobs losing progress.
  Mitigation: PDF page extraction manifests persist per-page status and reusable OCR/correction
  artifacts.
- SQLite writer contention.
  Mitigation: WAL mode, foreign keys, `synchronous=NORMAL`, 5-second busy timeout, queue leases, and
  maintenance commands for checkpoint/optimize/VACUUM.

## Supply Chain Threats

- Compromised dependencies or release artifacts.
  Mitigation: Dependabot, pull-request dependency review, `pip-audit`, CodeQL, release SBOMs,
  container vulnerability scanning, and GitHub artifact attestations. Stable releases should add
  signed artifacts and locked release constraints.
- Container running with excessive privileges.
  Mitigation: the Docker image runs as an unprivileged `librarian` user and stores mutable data under
  `/data`.

## Residual Risks

- Static API-key auth is not enough for multi-user hosted service. Hosted deployments need users,
  scoped tokens, RBAC, audit logs, and tenant-specific data roots.
- External LLM providers receive document text during processing. Operators must choose providers
  whose data-use terms fit their corpus.
- Local filesystem malware scanning is out of scope. Operators remain responsible for scanning input
  files before import.
