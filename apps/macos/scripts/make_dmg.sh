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
# hdiutil intermittently fails on CI runners with "create failed - Resource
# busy" (a wedged diskimages-helper left over from an earlier mount). It is
# transient, so retry a few times with a short pause instead of failing a
# release build on a runner hiccup.
for attempt in 1 2 3 4; do
  if hdiutil create -volname "Librarian" -srcfolder "$STAGING" -ov -format UDZO "$OUTPUT"; then
    break
  fi
  if [[ "$attempt" -eq 4 ]]; then
    echo "hdiutil create failed after $attempt attempts" >&2
    exit 1
  fi
  echo "hdiutil create failed (attempt $attempt); retrying in $((attempt * 5))s..." >&2
  rm -f "$OUTPUT"
  sleep $((attempt * 5))
done
echo "Created $OUTPUT"
