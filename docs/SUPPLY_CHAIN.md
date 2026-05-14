# Supply Chain

Current release safeguards:

- GitHub Actions CI runs lint, tests, type checking, and Docker build.
- CI verifies prompt-eval, synthetic corpus-eval, and multi-chunk benchmark JSON evidence before building
  distributions or Docker images.
- Release evidence verification rejects corpus-eval page diagnostics that still contain failed or
  pending PDF/OCR page extraction statuses.
- Release evidence verification also cross-checks PDF/OCR corpus tags against page-source and OCR
  metrics so tag strings cannot claim coverage without matching diagnostics.
- CI and tag release workflows run `pip-audit --skip-editable` against the resolved Python
  environment. The local editable package is skipped because it is not a PyPI dependency; resolved
  third-party packages are still audited.
- Pull requests run GitHub dependency review and fail dependency changes that introduce high or
  critical severity advisories.
- CI uses read-only repository token permissions, and workflows disable checkout credential
  persistence unless a later step explicitly receives a token.
- CI, release, CodeQL, dependency review, and secret-scanning jobs use explicit timeouts to avoid
  unbounded hangs.
- Dependabot monitors Python and GitHub Actions dependencies.
- CodeQL runs on pushes to the default branch.
- Secret scanning runs Gitleaks on pushes, pull requests, weekly schedule, and manual dispatch.
- Release workflow builds wheels/sdist from tags.
- Release workflow serializes runs per tag and creates releases with `gh release create
  --verify-tag`.
- Release workflow smoke-installs the built wheel and runs the installed CLI before publishing.
- Release workflow verifies optional OCR/PDF dependencies with `librarian doctor --strict`.
- Release workflow publishes Docker images to GitHub Container Registry.
- Release workflow generates a CycloneDX SBOM artifact.
- Release workflow exports `constraints.txt` exact third-party dependency pins from `uv.lock`.
- Release workflow publishes `SHA256SUMS.txt` for the wheel, source distribution, SBOM,
  constraints, and sanitized mock evidence.
- Release workflow verifies `SHA256SUMS.txt` before uploading release artifacts.
- Release workflow stores the intermediate Actions artifact under the release tag name for 30 days.
- Release workflow generates GitHub artifact attestations for distributions, release
  metadata/evidence assets, and container images.
- Release workflow scans release container images for high/critical vulnerabilities before publish.

## Alpha Dependency Policy

Current alpha releases are dependency-floating. Runtime dependencies are declared with lower bounds
and resolved at build time in CI, release, and Docker builds. This keeps the alpha line simple while
the public API and package ownership settle.

Stable releases should not use this policy. Before a stable tag, release builds should use a locked
dependency graph or publish constraints alongside the wheel, source distribution, SBOM, and
container image.

Alpha release artifacts include `constraints.txt` generated from `uv.lock` so users can reproduce
the tested dependency set while the package metadata itself remains lower-bound based.

Planned hardening before a stable release:

- Signed release artifacts.
- Published package index ownership and trusted publishing.

## Reproducibility Notes

Alpha builds are not byte-for-byte reproducible across arbitrary machines because wheel, sdist, and
container metadata can include build-environment timestamps and toolchain details. They are,
however, evidence-reproducible from a tag:

- `v*` tags must match `src/librarian/version.py` before the release workflow builds artifacts.
- The release workflow builds from the checked-out tag and verifies the tag exists before creating
  the GitHub release.
- `constraints.txt` is generated from `uv.lock` and records exact third-party registry packages for
  the tested release environment.
- The built wheel is smoke-installed with `-c constraints.txt` so release validation uses the same
  dependency pins published to users.
- `SHA256SUMS.txt` records the exact uploaded distribution, SBOM, constraints, and sanitized
  evidence bytes, and the workflow verifies it before publication.
- GitHub artifact attestations bind the published distributions, release metadata/evidence, and
  container image digest to the workflow run that produced them.

To recreate the tested Python dependency environment for a release, download `constraints.txt` with
the wheel and install with:

```bash
pip install -c constraints.txt "nampara_librarian-<version>-py3-none-any.whl[all]"
```

To audit a release candidate locally before tagging, use the same source checkout and run:

```bash
rm -rf dist
python -m build
python .github/scripts/export_constraints.py --output constraints.txt
sha256sum dist/* constraints.txt
```

Treat mismatches against GitHub release assets as a signal to inspect the build host, Python
version, build backend version, and dependency resolver output before distributing artifacts.

## Secret Scanning

Do not commit API keys, provider logs, private documents, `.env` files, or generated eval outputs
that contain private text. CI runs Gitleaks with full git history checkout so both new pull requests
and scheduled scans can detect committed credentials. CI and release scans run the pinned
`zricethezav/gitleaks:v8.30.1` container so secret scanning is not coupled to a GitHub Actions
JavaScript runtime.

Run a local scan before release candidates or after handling credentials:

```bash
docker run --rm -v "$PWD:/repo" -w /repo zricethezav/gitleaks:v8.30.1 \
  detect --source . --no-banner --redact --verbose
```

If a real secret is committed, rotate it before rewriting history or adding an allowlist entry.
Only allowlist deterministic test fixtures or documented false positives.

## Artifact Verification

Download release artifacts from GitHub Releases, then verify local files before installing:

```bash
sha256sum --check SHA256SUMS.txt
```

Verify GitHub provenance attestations for downloaded distributions and release metadata:

```bash
gh attestation verify dist/nampara_librarian-*.whl --repo nampara-ai/librarian
gh attestation verify dist/nampara_librarian-*.tar.gz --repo nampara-ai/librarian
gh attestation verify sbom.json --repo nampara-ai/librarian
gh attestation verify constraints.txt --repo nampara-ai/librarian
gh attestation verify SHA256SUMS.txt --repo nampara-ai/librarian
```

For container images, pin deployments to the digest published by GHCR and verify the registry
attestation for that digest:

```bash
gh attestation verify oci://ghcr.io/nampara-ai/librarian@sha256:<digest> --repo nampara-ai/librarian
```
