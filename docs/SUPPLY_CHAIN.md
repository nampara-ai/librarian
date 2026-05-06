# Supply Chain

Current release safeguards:

- GitHub Actions CI runs lint, tests, type checking, and Docker build.
- Dependabot monitors Python and GitHub Actions dependencies.
- CodeQL workflow scaffold is present. Enable GitHub code scanning in repository settings before
  running it.
- Release workflow builds wheels/sdist from tags.
- Release workflow publishes Docker images to GitHub Container Registry.
- Release workflow generates a CycloneDX SBOM artifact.

## Alpha Dependency Policy

`v0.1.0a1` is a dependency-floating alpha. Runtime dependencies are declared with lower bounds and
resolved at build time in CI, release, and Docker builds. This keeps the first alpha simple while the
public API and package ownership settle.

Stable releases should not use this policy. Before a stable tag, release builds should use a locked
dependency graph or publish constraints alongside the wheel, source distribution, SBOM, and
container image.

Planned hardening before a stable release:

- Artifact attestations for distributions and images.
- Signed release artifacts.
- Published package index ownership and trusted publishing.
- Container image vulnerability scanning.
- Reproducible build notes.
