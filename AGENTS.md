# Librarian collaboration rules

**NFT means minimal, meaningful testing—not a blanket prohibition on tests.** Jake clarified that critical unit/integration tests and cargo/Vitest are allowed.

Prioritize implementation. Write and run only the smallest necessary checks for load-bearing behavior, real invariants, and essentially mandatory coverage. Focus on timing, concurrency, scale, precision, and failure cases a human cannot reliably exercise manually. No trivial file/name assertions, implementation-mirroring tests, empty wrappers, flaky harnesses, or mocks that bypass the behavior under examination. Do not treat green tests as proof the product works. Compilation, packaging, formatting, static analysis, and code review are permitted. Preserve others' existing tests unless Jake requests removal. Earlier assistant-authored tests/harnesses were removed under the original request; do not restore them wholesale.

Build the complete CLI/TUI workflow: configure options/schema, choose source file or folder, choose or create an output folder before processing, queue all selected files, process files one at a time, show live terminal progress, and report outputs and diagnostics. No graphical application work. Jake will supply API keys and real documents for hands-on product testing at the end; targeted automated checks do not replace that work.

Do not modify Cursive or unrelated repositories. Do not delete Keychain entries. Keep the Desktop Librarian prototype read-only.

## Terminal UX requirements

Use one shared onboarding and navigation implementation on every native platform. Keep differences in credential storage, data paths, and bundled binaries behind platform adapters; do not create separate user-facing setup flows unless unavoidable.

- Up/Down and Tab/Shift+Tab navigate every list or form. Enter and Space activate the focused button, toggle, or browsable field. Text entry keeps ordinary spaces and cursor movement; global shortcuts must not fire while typing.
- File browser: Enter opens a folder or selects a file; Space selects the current folder; Esc goes back. Preserve Ctrl+B as an additional browse shortcut. Esc must work in every setup/import screen and preserve answers when going back.
- Home shortcuts: n New Import, q Start queue, p Pause, r Resume, c Cancel, s Search, f Setup, h Help. Esc leaves the outermost screen; q is never Quit.
- Before first Home, a one-question-at-a-time bootstrap wizard must establish a usable runtime profile, credentials, model selection, local engines, processing/output defaults, and readiness. Show automatic work and let x continue after completion; x must not bypass errors or required human input.
- The saved runtime profile must be loaded by all CLI/TUI/background workers without requiring shell exports or virtual-environment activation. Never store secret values in ordinary configuration, job snapshots, logs, or command-line arguments. Preserve existing Keychain entries.
- Distinguish configuration saved, credential/model access verified, engines initialized, and actual document quality. Do not label an unverified or failed step ready. Existing setup and queued jobs must not be silently overwritten by a revised wizard.

- Ctrl+S saves accepted setup/import drafts, requests a pause for active/queued work in the current workspace, and exits. Ctrl+C has no application binding in the TUI; terminal clipboard behavior belongs to the terminal. Conventional SIGINT remains supported by non-interactive CLI workers. Esc at Home closes only the dashboard; make continued background processing explicit.
- Provider selection fills the endpoint and retrieves live model choices. Label vision capability and price metadata honestly; never substitute a stale model list after discovery failure. Provider rankings require a dated, named benchmark. Setup must verify the chosen profile through a fresh worker before activating it.
