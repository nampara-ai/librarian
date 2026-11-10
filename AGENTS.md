# Librarian collaboration rules

## Product contract

The authoritative editorial workflow is [docs/EDITORIAL_PIPELINE.md](docs/EDITORIAL_PIPELINE.md), approved by Jake on 2026-11-07. It supersedes archival-fidelity requirements in older architecture notes. Build continuous, reader-ready, audibly understandable Markdown through parallel native copyediting and page-image/OCR/vision readings, Typesetter synthesis, and a final corrective Copyeditor. Legitimate transcription, number-format and quotation repairs must not cause automatic whole-passage rollback. Keep originals and provenance independently. Preserve explicit technical failures and reliable checkpoint/coverage accounting; do not confuse these with editorial judgments.

## Foundation direction — 2026-11-08

Jake directed a first-principles rebuild: a universal, deterministic file cracker is the baseline, and intelligence is a layer on top. See [docs/PARSING_FOUNDATION.md](docs/PARSING_FOUNDATION.md). New work extracts native text, geometry and structure first (PDFium for PDF), reconstructs layout deterministically, and invokes OCR, vision or models only for pages and regions the routing report identifies. Where the editorial contract requires rendering every page or printing text to images for OCR/vision, this direction governs new work; the editorial pipeline remains for rough-prose copyediting and existing runs until it is rerouted. Every native word must be accounted for exactly once. Measure reconstruction changes with the Nutrient benchmark on more than one document, and never tune thresholds to a single fixture. Benchmark fixtures and third-party outputs stay outside the repository. Judge reconstruction changes on the opendataloader-bench holdout (even-numbered documents, never inspected one by one) as well as the development half, and keep the robustness sweep at zero crashes and zero accounting failures.

**NFT means minimal, meaningful testing—not a blanket prohibition on tests.** Jake clarified that critical unit/integration tests and cargo/Vitest are allowed.

Prioritize implementation. Write and run only the smallest necessary checks for load-bearing behavior, real invariants, and essentially mandatory coverage. Focus on timing, concurrency, scale, precision, and failure cases a human cannot reliably exercise manually. No trivial file/name assertions, implementation-mirroring tests, empty wrappers, flaky harnesses, or mocks that bypass the behavior under examination. Do not treat green tests as proof the product works. Compilation, packaging, formatting, static analysis, and code review are permitted. Preserve others' existing tests unless Jake requests removal. Earlier assistant-authored tests/harnesses were removed under the original request; do not restore them wholesale.

Build the complete CLI/TUI workflow: configure options/schema, choose source file or folder, choose or create an output folder before processing, queue all selected files, process files one at a time, show live terminal progress, and report outputs and diagnostics. No graphical application work. Jake will supply API keys and real documents for hands-on product testing at the end; targeted automated checks do not replace that work.

Do not modify Cursive or unrelated repositories. Do not delete Keychain entries. Keep the Desktop Librarian prototype read-only.

## Terminal UX requirements

Use one shared onboarding and navigation implementation on every native platform. Keep differences in credential storage, data paths, and bundled binaries behind platform adapters; do not create separate user-facing setup flows unless unavoidable.

- Up/Down and Tab/Shift+Tab navigate every list or form. Enter and Space activate the focused button, toggle, or browsable field. Text entry keeps ordinary spaces and cursor movement; global shortcuts must not fire while typing.
- File browser: Enter opens a folder or selects a file; Space selects the current folder; Esc goes back. Preserve Ctrl+B as an additional browse shortcut. Esc must work in every setup/import screen and preserve answers when going back.
- Home (redesigned 2026-11-10 with Jake's permission to make the workflow and controls clear): two panes, actions on the left and documents on the right. Up/Down moves within the focused pane; Tab/Shift+Tab and Left/Right switch panes; Enter/Space runs the focused action or opens the selected document's menu. Every action shows its key and a one-line meaning. The footer shows only the keys that apply to the focused pane.
- Home shortcuts: a Parse PDFs now, n Clean documents with AI (import), q Start queue, p Pause, r Resume or retry, c Cancel, s Search, f Setup, h Help. Esc leaves the outermost screen; q is never Quit.
- Parse PDFs is a guided form (source, destination, options, Parse now) that runs the deterministic engine locally, shows results with a Markdown preview, and keeps a short history. It needs no provider and makes no model calls.
- Before first Home, a one-question-at-a-time bootstrap wizard must establish a usable runtime profile, credentials, model selection, local engines, processing/output defaults, and readiness. Show automatic work and let x continue after completion; x must not bypass errors or required human input.
- The saved runtime profile must be loaded by all CLI/TUI/background workers without requiring shell exports or virtual-environment activation. Never store secret values in ordinary configuration, job snapshots, logs, or command-line arguments. Preserve existing Keychain entries.
- Distinguish configuration saved, credential/model access verified, engines initialized, and actual document quality. Do not label an unverified or failed step ready. Existing setup and queued jobs must not be silently overwritten by a revised wizard.

- Ctrl+S saves accepted setup/import drafts, requests a pause for active/queued work in the current workspace, and exits. Ctrl+C has no application binding in the TUI; terminal clipboard behavior belongs to the terminal. Conventional SIGINT remains supported by non-interactive CLI workers. Esc at Home closes only the dashboard; make continued background processing explicit.
- Provider selection fills the endpoint and retrieves live model choices. Label vision capability and price metadata honestly; never substitute a stale model list after discovery failure. Provider rankings require a dated, named benchmark. Setup must verify the chosen profile through a fresh worker before activating it.
