#!/usr/bin/env bash
set -euo pipefail

# Package Librarian.app into a drag-to-Applications DMG.
#
# Usage:
#   scripts/make_dmg.sh --app dist/Librarian.app --output dist/Librarian.dmg

APP=""
OUTPUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app) APP="$2"; shift 2 ;;
    --output) OUTPUT="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

[[ -d "$APP" ]] || { echo "--app must point at an existing .app bundle" >&2; exit 1; }
[[ -n "$OUTPUT" ]] || { echo "--output is required" >&2; exit 1; }

STAGING="$(mktemp -d)"
trap 'rm -rf "$STAGING"' EXIT

cp -R "$APP" "$STAGING/"
ln -s /Applications "$STAGING/Applications"

mkdir -p "$(dirname "$OUTPUT")"
rm -f "$OUTPUT"
hdiutil create -volname "Librarian" -srcfolder "$STAGING" -ov -format UDZO "$OUTPUT"
echo "Created $OUTPUT"
