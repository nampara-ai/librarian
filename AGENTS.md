# Librarian collaboration rules

**NFT means minimal, meaningful testing—not a blanket prohibition on tests.** Jake clarified that critical unit/integration tests and cargo/Vitest are allowed.

Prioritize implementation. Write and run only the smallest necessary checks for load-bearing behavior, real invariants, and essentially mandatory coverage. Focus on timing, concurrency, scale, precision, and failure cases a human cannot reliably exercise manually. No trivial file/name assertions, implementation-mirroring tests, empty wrappers, flaky harnesses, or mocks that bypass the behavior under examination. Do not treat green tests as proof the product works. Compilation, packaging, formatting, static analysis, and code review are permitted. Preserve others' existing tests unless Jake requests removal. Earlier assistant-authored tests/harnesses were removed under the original request; do not restore them wholesale.

Build the complete CLI/TUI workflow: configure options/schema, choose source file or folder, choose or create an output folder before processing, queue all selected files, process files one at a time, show live terminal progress, and report outputs and diagnostics. No graphical application work. Jake will supply API keys and real documents for hands-on product testing at the end; targeted automated checks do not replace that work.

Do not modify Cursive or unrelated repositories. Do not delete Keychain entries. Keep the Desktop Librarian prototype read-only.
