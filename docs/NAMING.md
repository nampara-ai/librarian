# Naming And Package Stability

The Python package name is `nampara-librarian`.

The import namespace and CLI command are intentionally shorter:

- Python import: `librarian`
- CLI command: `librarian`
- Docker image: `ghcr.io/nampara-ai/librarian`

Before tagging `v0.1.0-alpha`, maintainers should confirm this naming is final enough for public
use. After the first public tag, changing the package name, import namespace, or CLI command should
be treated as a breaking change and documented in `CHANGELOG.md`.
