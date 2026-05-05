# Release Process

Releases are built from git tags named `v*`.

## Checklist

1. Update `CHANGELOG.md`.
2. Confirm `pyproject.toml` has the intended version.
3. Run local verification:

```bash
ruff check .
pyright
pytest
python -m build
```

4. Tag and push:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The release workflow builds source and wheel distributions and uploads them as a GitHub Actions
artifact. Publishing to a package index should be added only after package ownership and signing
policy are finalized.
