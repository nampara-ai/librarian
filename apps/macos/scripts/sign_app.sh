#!/usr/bin/env bash
set -euo pipefail

# Sign Librarian.app including every Mach-O inside the bundled backend.
#
# Usage:
#   scripts/sign_app.sh --app dist/Librarian.app [--identity "Developer ID Application: ..."]
#
# Without --identity the bundle is ad-hoc signed, which is enough to run
# locally; distribution-quality signing and notarization require a
# Developer ID identity.

APP=""
IDENTITY="-"
ENTITLEMENTS="$(dirname "$0")/../Support/entitlements.plist"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app) APP="$2"; shift 2 ;;
    --identity) IDENTITY="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

[[ -d "$APP" ]] || { echo "--app must point at an existing .app bundle" >&2; exit 1; }

# Entitlement scoping (least privilege):
#   - The broad entitlements in Support/entitlements.plist
#     (disable-library-validation + allow-unsigned-executable-memory) are needed
#     ONLY by the embedded CPython interpreter: it loads unsigned/third-party
#     C-extension .so files (library validation) and some deps JIT/allocate
#     executable memory. Applying them to every nested binary — including the
#     bundled OCR tools (tesseract/pdftoppm/...) — needlessly widens the attack
#     surface.
#   - Every Mach-O still gets the hardened runtime (--options runtime) for
#     notarization; the OCR tools are signed WITHOUT the broad entitlements.
#
# Always apply --options runtime so the hardened runtime is consistent across
# ad-hoc and Developer ID signing. Entitlements/timestamp only apply for a real
# Developer ID identity (ad-hoc "-" signing cannot carry them meaningfully).

# Base flags for every binary: hardened runtime, no broad entitlements.
BASE_FLAGS=(--force --sign "$IDENTITY" --options runtime)
# Flags for the Python interpreter: base + the broad entitlements.
PY_FLAGS=(--force --sign "$IDENTITY" --options runtime)
if [[ "$IDENTITY" != "-" ]]; then
  BASE_FLAGS+=(--timestamp)
  PY_FLAGS+=(--timestamp --entitlements "$ENTITLEMENTS")
fi

# The interpreter binaries that legitimately need the broad entitlements. These
# live under Contents/Resources/backend/python/bin (python3, python3.x). Match
# by realpath so the symlink and the real binary both get the entitlements.
is_python_interpreter() {
  local path="$1"
  case "$path" in
    */Resources/backend/python/bin/python*) return 0 ;;
    *) return 1 ;;
  esac
}

# Sign nested Mach-O files first (the embedded Python and its extensions, plus
# the bundled OCR tools and their relocated dylibs), then the app bundle
# itself. Notarization rejects any unsigned nested Mach-O.
for nested in backend ocr; do
  dir="$APP/Contents/Resources/$nested"
  [[ -d "$dir" ]] || continue
  while IFS= read -r -d '' binary; do
    if file -b "$binary" | grep -q "Mach-O"; then
      if is_python_interpreter "$binary"; then
        codesign "${PY_FLAGS[@]}" "$binary"
      else
        codesign "${BASE_FLAGS[@]}" "$binary"
      fi
    fi
  done < <(find "$dir" -type f \( -perm -111 -o -name "*.so" -o -name "*.dylib" \) -print0)
done

# Sign the app bundle itself with the interpreter entitlements: the app's main
# executable launches the embedded interpreter, and the top-level entitlements
# govern the process. Nested OCR tools were already signed WITHOUT them above.
codesign "${PY_FLAGS[@]}" "$APP"
codesign --verify --deep --strict "$APP"
echo "Signed $APP with identity: $IDENTITY"
