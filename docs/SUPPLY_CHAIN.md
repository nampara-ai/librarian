# Supply Chain

Current release safeguards:

- GitHub Actions CI runs lint, tests, type checking, and Docker build.
- Dependabot monitors Python and GitHub Actions dependencies.
- CodeQL workflow scaffold is present. Enable GitHub code scanning in repository settings before
  running it.
- Release workflow builds wheels/sdist from tags.
- Release workflow publishes Docker images to GitHub Container Registry.
- Release workflow generates a CycloneDX SBOM artifact.

Planned hardening before a stable release:

- Artifact attestations for distributions and images.
- Signed release artifacts.
- Published package index ownership and trusted publishing.
- Container image vulnerability scanning.
- Reproducible build notes.
