#!/usr/bin/env bash
set -euo pipefail

# Bundle a relocatable Python runtime plus the Librarian backend inside
# Librarian.app so the app is fully self-contained.
#
# Usage:
#   scripts/bundle_backend.sh --app dist/Librarian.app --wheel ../../dist/nampara_librarian-*.whl [--arch arm64|x86_64]
#
# The wheel argument may also be a PyPI requirement such as
# "nampara-librarian[all]==1.0.0".
#
# A --constraints file (e.g. the release constraints.txt exported from uv.lock)
# is applied to pip so bundled dependency versions are pinned/reproducible.

PBS_RELEASE="20260602"
PYTHON_VERSION="3.12.13"

# Expected SHA256 of the python-build-standalone tarball, per architecture, for
# the pinned PBS_RELEASE + PYTHON_VERSION above. This is a supply-chain guard:
# the download is verified against these digests and the build fails hard on any
# mismatch (or if the digest is left unset).
#
# TODO(security): fill in the real digests before shipping. Obtain them from the
# .sha256 sidecar files the PBS release publishes, e.g.:
#   curl -fsSL "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${TARBALL}.sha256"
# (that file contains the hex digest for the matching TARBALL). Paste the 64-hex
# value for each arch below. The literal placeholder string will never match a
# real digest, so an unfilled value fails the build rather than skipping the
# check.
PBS_SHA256_aarch64="REPLACE_WITH_REAL_SHA256_FOR_aarch64"
PBS_SHA256_x86_64="REPLACE_WITH_REAL_SHA256_FOR_x86_64"

APP=""
WHEEL=""
ARCH="$(uname -m)"
CONSTRAINTS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app) APP="$2"; shift 2 ;;
    --wheel) WHEEL="$2"; shift 2 ;;
    --arch) ARCH="$2"; shift 2 ;;
    --constraints) CONSTRAINTS="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

[[ -d "$APP" ]] || { echo "--app must point at an existing .app bundle" >&2; exit 1; }
[[ -n "$WHEEL" ]] || { echo "--wheel is required (wheel path or PyPI requirement)" >&2; exit 1; }
if [[ -n "$CONSTRAINTS" ]]; then
  [[ -f "$CONSTRAINTS" ]] || { echo "--constraints file not found: $CONSTRAINTS" >&2; exit 1; }
fi

case "$ARCH" in
  arm64|aarch64) PBS_ARCH="aarch64" ;;
  x86_64|amd64) PBS_ARCH="x86_64" ;;
  *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

BACKEND_DIR="$APP/Contents/Resources/backend"
rm -rf "$BACKEND_DIR"
mkdir -p "$BACKEND_DIR"

TARBALL="cpython-${PYTHON_VERSION}+${PBS_RELEASE}-${PBS_ARCH}-apple-darwin-install_only_stripped.tar.gz"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/${TARBALL}"

# Select the expected digest for the target arch (indirect expansion).
EXPECTED_SHA_VAR="PBS_SHA256_${PBS_ARCH}"
EXPECTED_SHA="${!EXPECTED_SHA_VAR:-}"
if [[ ! "$EXPECTED_SHA" =~ ^[0-9a-fA-F]{64}$ ]]; then
  echo "No valid pinned SHA256 for ${PBS_ARCH} (got '${EXPECTED_SHA}')." >&2
  echo "Refusing to bundle an unverified Python runtime; fill in ${EXPECTED_SHA_VAR}." >&2
  exit 1
fi

echo "Downloading ${TARBALL}"
curl -fL --retry 3 --retry-delay 2 -o "${BACKEND_DIR}/python.tar.gz" "$URL"

echo "Verifying tarball SHA256"
# Verify against the pinned digest before extracting. shasum -c fails non-zero
# on mismatch, and `set -e` aborts the build — nothing is extracted on failure.
printf '%s  %s\n' "$EXPECTED_SHA" "${BACKEND_DIR}/python.tar.gz" \
  | shasum -a 256 -c - \
  || { echo "SHA256 mismatch for ${TARBALL}; aborting (possible tampering)." >&2; exit 1; }

tar -xzf "${BACKEND_DIR}/python.tar.gz" -C "$BACKEND_DIR"
rm "${BACKEND_DIR}/python.tar.gz"

PYBIN="${BACKEND_DIR}/python/bin/python3"
[[ -x "$PYBIN" ]] || { echo "Extracted Python interpreter not found at $PYBIN" >&2; exit 1; }

# An x86_64 interpreter runs under Rosetta 2 on Apple Silicon, so pip can
# install x86_64 wheels there too. When cross-bundling x86_64 on an Apple
# Silicon host, force prebuilt wheels: otherwise pip may source-build a
# dependency (e.g. cryptography), and the native arm64 toolchain cross-compiles
# it to x86_64 and fails (openssl-sys cannot find an x86_64 OpenSSL).
PIP_BINARY=()
if [[ "$PBS_ARCH" == "x86_64" && "$(uname -m)" == "arm64" ]]; then
  PIP_BINARY+=(--only-binary=:all:)
fi
# Apply the exported dependency constraints (exact pins from uv.lock) when
# provided, so the bundled runtime's dependencies are reproducible and pinned
# instead of resolving to whatever is latest on PyPI at build time.
PIP_CONSTRAINTS=()
if [[ -n "$CONSTRAINTS" ]]; then
  echo "Applying dependency constraints: $CONSTRAINTS"
  PIP_CONSTRAINTS+=(-c "$CONSTRAINTS")
fi
echo "Installing Librarian backend"
if [[ -f "$WHEEL" ]]; then
  "$PYBIN" -m pip install --quiet --no-warn-script-location \
    ${PIP_BINARY[@]+"${PIP_BINARY[@]}"} \
    ${PIP_CONSTRAINTS[@]+"${PIP_CONSTRAINTS[@]}"} "${WHEEL}[all]"
else
  "$PYBIN" -m pip install --quiet --no-warn-script-location \
    ${PIP_BINARY[@]+"${PIP_BINARY[@]}"} \
    ${PIP_CONSTRAINTS[@]+"${PIP_CONSTRAINTS[@]}"} "$WHEEL"
fi

"$PYBIN" -m librarian version >/dev/null

echo "Pruning bundle"
find "${BACKEND_DIR}/python" -type d -name "__pycache__" -prune -exec rm -rf {} +
rm -rf "${BACKEND_DIR}/python/lib/python"*/idlelib
rm -rf "${BACKEND_DIR}/python/share"

echo "Bundled backend:"
du -sh "$BACKEND_DIR"
