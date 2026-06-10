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

PBS_RELEASE="20260602"
PYTHON_VERSION="3.12.13"

APP=""
WHEEL=""
ARCH="$(uname -m)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app) APP="$2"; shift 2 ;;
    --wheel) WHEEL="$2"; shift 2 ;;
    --arch) ARCH="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

[[ -d "$APP" ]] || { echo "--app must point at an existing .app bundle" >&2; exit 1; }
[[ -n "$WHEEL" ]] || { echo "--wheel is required (wheel path or PyPI requirement)" >&2; exit 1; }

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

echo "Downloading ${TARBALL}"
curl -fL --retry 3 --retry-delay 2 -o "${BACKEND_DIR}/python.tar.gz" "$URL"
tar -xzf "${BACKEND_DIR}/python.tar.gz" -C "$BACKEND_DIR"
rm "${BACKEND_DIR}/python.tar.gz"

PYBIN="${BACKEND_DIR}/python/bin/python3"
[[ -x "$PYBIN" ]] || { echo "Extracted Python interpreter not found at $PYBIN" >&2; exit 1; }

# An x86_64 interpreter runs under Rosetta 2 on Apple Silicon, so pip can
# install x86_64 wheels there too.
echo "Installing Librarian backend"
if [[ -f "$WHEEL" ]]; then
  "$PYBIN" -m pip install --quiet --no-warn-script-location "${WHEEL}[all]"
else
  "$PYBIN" -m pip install --quiet --no-warn-script-location "$WHEEL"
fi

"$PYBIN" -m librarian version >/dev/null

echo "Pruning bundle"
find "${BACKEND_DIR}/python" -type d -name "__pycache__" -prune -exec rm -rf {} +
rm -rf "${BACKEND_DIR}/python/lib/python"*/idlelib
rm -rf "${BACKEND_DIR}/python/share"

echo "Bundled backend:"
du -sh "$BACKEND_DIR"
