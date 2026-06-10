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

SIGN_FLAGS=(--force --sign "$IDENTITY")
if [[ "$IDENTITY" != "-" ]]; then
  SIGN_FLAGS+=(--options runtime --timestamp --entitlements "$ENTITLEMENTS")
fi

# Sign nested Mach-O files first (the embedded Python and its extensions),
# then the app bundle itself.
if [[ -d "$APP/Contents/Resources/backend" ]]; then
  while IFS= read -r -d '' binary; do
    if file -b "$binary" | grep -q "Mach-O"; then
      codesign "${SIGN_FLAGS[@]}" "$binary"
    fi
  done < <(find "$APP/Contents/Resources/backend" -type f \( -perm -111 -o -name "*.so" -o -name "*.dylib" \) -print0)
fi

codesign "${SIGN_FLAGS[@]}" "$APP"
codesign --verify --deep --strict "$APP"
echo "Signed $APP with identity: $IDENTITY"
