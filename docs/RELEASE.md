# Release Process

Releases are built from git tags named `v*`. The first public alpha tag is `v0.1.0a1`.

## Checklist

1. Update `CHANGELOG.md` by moving unreleased entries under the release version and date.
2. Confirm `pyproject.toml` and `src/librarian/version.py` have the intended version.
3. Confirm package naming in `docs/NAMING.md`.
4. Review `SECURITY.md` and public docs for private references.
5. Generate provider eval and performance baselines where credentials are available.
6. Confirm dependency policy:

   - `v0.1.0a1` is a dependency-floating alpha. Runtime dependencies use lower bounds and CI,
     release, and Docker builds resolve the current compatible dependency set at build time.
   - Do not promote this policy to stable releases. Before a stable tag, wire release builds to a
     locked dependency graph or publish constraints with the release artifacts.

7. Run local verification:

```bash
ruff check .
pyright
pytest
python -m build
docker build -t librarian-release-check .
```

8. Tag and push:

```bash
git tag v0.1.0a1
git push origin v0.1.0a1
```

The release workflow builds source and wheel distributions, generates an SBOM, publishes a Docker
image to GitHub Container Registry, and creates a GitHub release.

The Docker image binds to `0.0.0.0` by default and intentionally refuses to start unless
`LIBRARIAN_API_KEY` and `LIBRARIAN_API_IMPORT_ROOT` are set. Use `docker compose` for the default
service layout, or pass those environment variables explicitly when using `docker run`.

Publishing to a package index should be added only after package ownership and signing policy are
finalized.
