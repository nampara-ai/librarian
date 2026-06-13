#!/usr/bin/env bash
set -euo pipefail

# Bundle relocatable OCR command-line tools (tesseract + poppler) and the
# Tesseract English language data inside Librarian.app, so scanned/image PDFs
# are OCR'd with zero external dependencies — no Homebrew, no PATH setup.
#
# Usage:
#   scripts/bundle_ocr.sh --app dist/Librarian.app [--arch arm64|x86_64]
#
# Must run on a runner whose native architecture matches --arch: Homebrew
# installs binaries for the host arch, so the arm64 DMG is built on Apple
# Silicon and the x86_64 DMG on an Intel runner. Dependent dylibs are copied
# into the bundle and their load paths rewritten to @executable_path/../lib
# with dylibbundler, so nothing points back at /opt/homebrew or /usr/local.

APP=""
ARCH="$(uname -m)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app) APP="$2"; shift 2 ;;
    --arch) ARCH="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

[[ -d "$APP" ]] || { echo "--app must point at an existing .app bundle" >&2; exit 1; }
command -v brew >/dev/null || { echo "Homebrew is required to source OCR tools" >&2; exit 1; }
command -v dylibbundler >/dev/null || {
  echo "dylibbundler is required (brew install dylibbundler)" >&2
  exit 1
}

HOST_ARCH="$(uname -m)"
case "$ARCH" in
  arm64|aarch64) WANT="arm64" ;;
  x86_64|amd64) WANT="x86_64" ;;
  *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac
if [[ "$HOST_ARCH" != "$WANT" ]]; then
  echo "Refusing to bundle $WANT OCR tools on a $HOST_ARCH host: Homebrew binaries" >&2
  echo "are host-native. Build the $WANT DMG on a $WANT runner." >&2
  exit 1
fi

BREW_PREFIX="$(brew --prefix)"
OCR_DIR="$APP/Contents/Resources/ocr"
BIN_DIR="$OCR_DIR/bin"
LIB_DIR="$OCR_DIR/lib"
TESSDATA_DIR="$OCR_DIR/share/tessdata"

rm -rf "$OCR_DIR"
mkdir -p "$BIN_DIR" "$LIB_DIR" "$TESSDATA_DIR"

# tesseract performs OCR; pdftoppm/pdftocairo rasterize PDF pages for pdf2image
# and pdfinfo reports page counts (pdf2image calls it during conversion).
EXES=(tesseract pdftoppm pdftocairo pdfinfo)
for exe in "${EXES[@]}"; do
  src="$BREW_PREFIX/bin/$exe"
  [[ -x "$src" ]] || { echo "Expected OCR tool not found: $src" >&2; exit 1; }
  cp "$src" "$BIN_DIR/$exe"
done

# English language data (plus orientation/script data for rotation handling).
for data in eng.traineddata osd.traineddata; do
  src="$BREW_PREFIX/share/tessdata/$data"
  if [[ -f "$src" ]]; then
    cp "$src" "$TESSDATA_DIR/$data"
  elif [[ "$data" == "eng.traineddata" ]]; then
    echo "Required Tesseract data not found: $src" >&2
    exit 1
  fi
done

echo "Relocating dependent dylibs into the bundle"
DYLIB_ARGS=()
for exe in "${EXES[@]}"; do
  DYLIB_ARGS+=(-x "$BIN_DIR/$exe")
done
# -of overwrite, -b fix the binaries, -cd create the dest dir,
# -p set the rewritten load-path prefix relative to each executable.
dylibbundler -of -b -cd \
  "${DYLIB_ARGS[@]}" \
  -d "$LIB_DIR" \
  -p "@executable_path/../lib"

echo "Verifying no Homebrew paths leak into the bundled OCR tools"
leaked=0
while IFS= read -r -d '' macho; do
  if otool -L "$macho" | tail -n +2 | grep -E "/opt/homebrew/|/usr/local/(Cellar|opt|lib)" >/dev/null; then
    echo "Leaked Homebrew path in: $macho" >&2
    otool -L "$macho" | grep -E "/opt/homebrew/|/usr/local/" >&2 || true
    leaked=1
  fi
done < <(find "$BIN_DIR" "$LIB_DIR" -type f -print0)
if [[ "$leaked" -ne 0 ]]; then
  echo "Bundled OCR tools still reference Homebrew; relocation failed." >&2
  exit 1
fi

echo "Bundled OCR tools ($WANT):"
du -sh "$OCR_DIR"
