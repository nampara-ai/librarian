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
#   - For a Developer ID identity every Mach-O gets the hardened runtime
#     (--options runtime) for notarization; the OCR tools are signed WITHOUT
#     the broad entitlements. Ad-hoc builds get no hardened runtime (see below).
#
# Hardened runtime turns on library validation, which refuses to load any
# native library not signed by the process's own Team ID. The embedded CPython
# loads dozens of third-party C-extension .so files (pydantic_core, Pillow,
# numpy, PDFium, ...) that carry their upstream signatures, so a hardened
# process MUST also carry com.apple.security.cs.disable-library-validation or
# it cannot import them and the backend dies on startup ("Engine didn't start").
#
# For a real Developer ID identity we apply BOTH (hardened runtime for
# notarization + the entitlement so libraries still load). For an ad-hoc build
# ("-", what CI ships when no signing cert is configured) we deliberately do
# NOT enable the hardened runtime: without it, library validation is not
# enforced and the third-party extensions load normally. Enabling hardened
# runtime on an ad-hoc build without the entitlement is exactly the regression
# that broke the engine — do not "consistency-fix" this back.

# Base flags for every binary. Hardened runtime + entitlements are added below
# only for a real Developer ID identity.
BASE_FLAGS=(--force --sign "$IDENTITY")
# Flags for the Python interpreter and the app bundle (the processes that load
# the extensions): base + the broad entitlements when hardened.
PY_FLAGS=(--force --sign "$IDENTITY")
if [[ "$IDENTITY" != "-" ]]; then
  BASE_FLAGS+=(--options runtime --timestamp)
  PY_FLAGS+=(--options runtime --timestamp --entitlements "$ENTITLEMENTS")
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
