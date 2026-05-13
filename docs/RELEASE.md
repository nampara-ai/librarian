# Release Process

Releases are built from git tags named `v*`.

## Checklist

1. Update `CHANGELOG.md` by moving unreleased entries under the release version and date.
2. Confirm `pyproject.toml` and `src/librarian/version.py` have the intended version.
3. Confirm package naming in `docs/NAMING.md`.
4. Review `SECURITY.md` and public docs for private references.
5. Generate provider eval and performance baselines where credentials are available:

```bash
export RELEASE_VERSION="$(uv run python -c 'from librarian.version import __version__; print(__version__)')"

librarian eval examples/eval_cases.json --output docs/results/eval-provider.json
librarian benchmark --input-path examples/benchmark_text.txt --repeats 3 \
  --output docs/results/benchmark-provider.json
librarian corpus-eval examples/synthetic-corpus/corpus_eval_cases.json \
  --output-dir .librarian/release-corpus-eval \
  --output docs/results/corpus-eval-provider.json \
  --overwrite
uv run python .github/scripts/verify_release_evidence.py \
  --eval docs/results/eval-provider.json \
  --benchmark docs/results/benchmark-provider.json \
  --corpus-eval docs/results/corpus-eval-provider.json \
  --version "v${RELEASE_VERSION}" \
  --require-real-provider \
  --min-corpus-cases 8 \
  --min-corpus-search-recall 1.0 \
  --min-corpus-output-ratio 0.05
```

   Keep private/provider outputs out of Git. Attach sanitized evidence to the release when it is
   appropriate for public distribution.
6. Run secret scanning:

```bash
gitleaks detect --source . --redact --verbose
```

7. Confirm dependency policy:

   - Current alpha releases are dependency-floating. Runtime dependencies use lower bounds and CI,
     release, and Docker builds resolve the current compatible dependency set at build time.
   - Do not promote this policy to stable releases. Before a stable tag, wire release builds to a
     locked dependency graph or publish constraints with the release artifacts.

8. Run local verification:

```bash
ruff check .
pyright
pytest
python -m pip install --upgrade "pip>=26.1"
pip-audit --progress-spinner off --skip-editable
librarian doctor --strict
rm -rf dist
python -m build
docker build -t librarian-release-check .
```

9. Run a container readiness smoke:

```bash
docker run --rm -p 18080:8080 \
  -e LIBRARIAN_API_KEY=change-me \
  -e LIBRARIAN_API_IMPORT_ROOT=/data/imports \
  librarian-release-check

curl http://127.0.0.1:18080/ready
```

10. Tag and push:

```bash
git tag "v${RELEASE_VERSION}"
git push origin "v${RELEASE_VERSION}"
```

The release workflow serializes runs per tag, first runs a read-only full-history Gitleaks scan,
then installs Tesseract/Poppler, verifies that the tag matches the package version and that
`CHANGELOG.md` has no remaining Unreleased entries, runs `librarian doctor --strict`, builds source
and wheel distributions, smoke-installs the built wheel and runs the installed CLI, audits the
resolved Python environment with `pip-audit --skip-editable`, generates sanitized mock eval,
corpus-eval, and benchmark evidence, verifies that evidence against the release tag, generates an
SBOM and `constraints.txt` from `uv.lock`,
publishes a Docker image to GitHub Container Registry, creates provenance attestations for
distributions, release metadata/evidence, and the container image, writes `SHA256SUMS.txt` for
direct artifact verification of the distributions, SBOM, dependency constraints, and sanitized mock
evidence,
verifies the checksum manifest before upload, scans the image for high/critical vulnerabilities,
and creates a verified-tag GitHub release with the distributions, SBOM, dependency constraints,
checksums, and sanitized mock evidence attached. The intermediate Actions artifact is named for the
release tag and retained for 30 days.

For alpha users downloading artifacts directly from GitHub Releases, install the wheel with:

```bash
sha256sum --check SHA256SUMS.txt
gh attestation verify nampara_librarian-0.1.0a4-py3-none-any.whl --repo nampara-ai/librarian
pip install -c constraints.txt "nampara_librarian-0.1.0a4-py3-none-any.whl[all]"
```

The Docker image binds to `0.0.0.0` by default and intentionally refuses to start unless
`LIBRARIAN_API_KEY` and `LIBRARIAN_API_IMPORT_ROOT` are set. Use `docker compose` for the default
service layout, or pass those environment variables explicitly when using `docker run`.

Publishing to a package index should be added only after package ownership and signing policy are
finalized.
