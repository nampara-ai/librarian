#!/usr/bin/env bash
set -euo pipefail

# Bundle relocatable OCR command-line tools (tesseract + poppler) and the
# Tesseract English language data inside Librarian.app, so scanned/image PDFs
# are OCR'd with zero external dependencies — no Homebrew, no PATH setup.
#
# Usage:
#   scripts/bundle_ocr.sh --app dist/Librarian.app --arch arm64|x86_64 \
#     [--brew-prefix /opt/homebrew]
#
# Both DMGs build on Apple Silicon runners (Intel runners are scarce). The
# arm64 binaries come from the native Homebrew at /opt/homebrew; the x86_64
# binaries come from an x86_64 Homebrew installed under Rosetta at /usr/local,
# so --brew-prefix selects which one. Dependent dylibs are copied into the
# bundle and their load paths rewritten to @executable_path/../lib with
# dylibbundler, so nothing points back at Homebrew.

APP=""
ARCH="$(uname -m)"
BREW_PREFIX=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app) APP="$2"; shift 2 ;;
    --arch) ARCH="$2"; shift 2 ;;
    --brew-prefix) BREW_PREFIX="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

[[ -d "$APP" ]] || { echo "--app must point at an existing .app bundle" >&2; exit 1; }

HOST_ARCH="$(uname -m)"
case "$ARCH" in
  arm64|aarch64) WANT="arm64" ;;
  x86_64|amd64) WANT="x86_64" ;;
  *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

# x86_64 binaries on an Apple Silicon host run (and are tooled) under Rosetta.
RUN=()
if [[ "$WANT" == "x86_64" && "$HOST_ARCH" == "arm64" ]]; then
  RUN=(arch -x86_64)
fi

[[ -n "$BREW_PREFIX" ]] || BREW_PREFIX="$("${RUN[@]}" brew --prefix)"
DYLIBBUNDLER="$BREW_PREFIX/bin/dylibbundler"
[[ -x "$DYLIBBUNDLER" ]] || {
  echo "dylibbundler not found at $DYLIBBUNDLER (brew install dylibbundler)" >&2
  exit 1
}

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
  # Each binary must be the architecture we are packaging for.
  archs="$(lipo -archs "$BIN_DIR/$exe" 2>/dev/null || echo "")"
  case " $archs " in
    *" $WANT "*) ;;
    *)
      echo "Bundled $exe is '$archs', expected $WANT (wrong --brew-prefix?)" >&2
      exit 1
      ;;
  esac
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
# Homebrew binaries reference their dependencies via @rpath. dylibbundler
# stops and prompts on stdin for a search path whenever it cannot resolve an
# @rpath dependency, which hangs forever in CI. Hand it every Homebrew library
# location up front (the linked lib dir plus each keg's opt/<name>/lib) so it
# never needs to ask, and close stdin so it fails fast instead of hanging if a
# stray dependency still slips through.
SEARCH_ARGS=(-s "$BREW_PREFIX/lib")
for opt_lib in "$BREW_PREFIX"/opt/*/lib; do
  [[ -d "$opt_lib" ]] && SEARCH_ARGS+=(-s "$opt_lib")
done
# -of overwrite, -b fix the binaries, -cd create the dest dir,
# -p set the rewritten load-path prefix relative to each executable.
"${RUN[@]}" "$DYLIBBUNDLER" -of -b -cd \
  "${DYLIB_ARGS[@]}" \
  "${SEARCH_ARGS[@]}" \
  -d "$LIB_DIR" \
  -p "@executable_path/../lib" </dev/null

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
