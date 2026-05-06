# Release Process

Releases are built from git tags named `v*`.

## Checklist

1. Update `CHANGELOG.md` by moving unreleased entries under the release version and date.
2. Confirm `pyproject.toml` and `src/librarian/version.py` have the intended version.
3. Confirm package naming in `docs/NAMING.md`.
4. Review `SECURITY.md` and public docs for private references.
5. Generate provider eval and performance baselines where credentials are available.
6. Run local verification:

```bash
ruff check .
pyright
pytest
python -m build
```

7. Tag and push:

```bash
git tag v0.1.0a1
git push origin v0.1.0a1
```

The release workflow builds source and wheel distributions, generates an SBOM, publishes a Docker
image to GitHub Container Registry, and creates a GitHub release. Publishing to a package index
should be added only after package ownership and signing policy are finalized.
