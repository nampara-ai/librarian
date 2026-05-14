# Roadmap

## v0.1 Alpha

- Local CLI and FastAPI service.
- Universal conversion to Markdown/plain text.
- Tesseract OCR for images and scanned PDFs.
- Page-aware PDF extraction with 1,000-page OCR defaults.
- Upgraded v2 cleaning/classification prompt stack.
- Batch import with manifest/resume and JSON reports.
- SQLite-backed persistence and worker queue.
- Docker Compose deployment.
- Eval and benchmark harnesses.
- Direct SRT/VTT transcript normalization, quote matching, and conversion.
- Release evidence gates for prompt eval, corpus eval, benchmark, and semantic
  corpus tag coverage.
- External repository review for Dolphin, olmOCR, and LongCut upgrade paths.

## v0.2

- 1,000-page PDF/OCR performance baselines on real corpora.
- Larger public benchmark artifacts for page-level OCR resume and diagnostics.
- Richer retrieval beyond SQLite FTS.
- Batch import API hardening for hosted deployments.
- More real-world conversion fixtures.
- Provider-specific performance baselines.
- Optional olmOCR-inspired VLM page OCR adapter with anchor-text prompts,
  rotation retry, fallback diagnostics, and provider cost/page metrics.
- Corpus-eval extensions for table relations, math/formula preservation,
  footnotes, formatting, and repeated-tail hallucination checks.
- Transcript quote/citation evidence in search results and exports.

## v0.3

- Multi-user hosted service support.
- Stronger auth model: users, tokens, RBAC, tenant boundaries.
- Networked queue/database adapter for horizontal deployments.
- Admin UI for imports, runs, failures, and exports.
- Local notes/annotations linked to documents, selected text, timestamps, and
  classifications.
- Grounded Q&A over processed documents with Librarian-validated citations.
- Hosted-mode request throttling and audit controls.

## Later

- Plugin system for custom taxonomies and conversion adapters.
- Native desktop packaging.
- Managed cloud deployment template.
- Optional visual layout parser plugin interface with Dolphin-like typed element
  parsing and layout visualization artifacts.
