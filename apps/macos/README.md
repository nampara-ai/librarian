# Librarian for Mac

A native SwiftUI app for the Librarian engine. Drag files in, watch them get
converted, cleaned, and classified with live progress, then read, search, and
export the results. Release builds ship the entire backend inside the app —
download, drag to Applications, double-click, done.

## Install (for users)

1. Download the DMG for your Mac from the
   [latest release](https://github.com/nampara-ai/librarian/releases/latest):
   - **Librarian-AppleSilicon.dmg** for M1/M2/M3/M4 Macs
   - **Librarian-Intel.dmg** for Intel Macs

   (Apple menu → About This Mac shows which chip you have.)
2. Open the DMG and drag **Librarian** into **Applications**.
3. Launch Librarian and drop a file anywhere in the window.

macOS 14 (Sonoma) or newer is required.

**First launch on an unsigned build:** macOS Gatekeeper will warn about an
unidentified developer. Right-click the app → **Open** → **Open**, or allow it
under **System Settings → Privacy & Security → Open Anyway**. Notarized
releases (built with a Developer ID, see below) open without any warning.

## How it works

The app bundles a relocatable Python runtime plus the `nampara-librarian`
backend in `Librarian.app/Contents/Resources/backend`. On launch it starts
`python -m librarian api` on a local port (127.0.0.1 only), waits for
`/health`, and talks to it over the same public HTTP API any other client
would use. Quitting the app stops the backend.

Each launch generates a random API key and passes it to the backend through
`LIBRARIAN_API_KEY`, so other local processes cannot read or modify your
corpus over localhost — only the app holds the credential for its own
backend instance.

Your data lives in `~/Library/Application Support/Librarian`:

| Path | Contents |
| --- | --- |
| `librarian.sqlite` | document database and search index |
| `uploads/` | original files you dropped in |
| `converted/` | converted Markdown/text outputs |
| `backend.log` | backend log (first stop for troubleshooting) |
| `.env` (optional) | backend configuration, see below |

### Connecting an LLM provider

Out of the box the app uses the built-in mock cleaner, which works offline.
For real LLM cleaning and classification, create
`~/Library/Application Support/Librarian/.env`:

```bash
LIBRARIAN_LLM_PROVIDER=openai-compatible
LIBRARIAN_LLM_MODEL=gpt-4.1-mini
OPENAI_API_KEY=sk-...
```

Restart the backend (quit and reopen the app, or use the status pill →
Restart) to apply changes. Any `LIBRARIAN_*` setting from
[docs/DEPLOYMENT.md](../../docs/DEPLOYMENT.md) works here.

### OCR for scanned documents

Embedded-text PDFs, DOCX, Markdown, text, and transcripts work out of the box.
OCR for scanned PDFs and images additionally needs the Tesseract and Poppler
command-line tools:

```bash
brew install tesseract poppler
```

### Using a remote backend instead

Settings (⌘,) → turn off "Run the built-in backend automatically" and enter
the URL and API key of any Librarian API server. Developer builds without a
bundled backend fall back to this mode automatically (default
`http://127.0.0.1:8080`).

## Develop (for contributors)

Requirements: Xcode 15+ or a recent Swift toolchain.

```bash
# Backend for the app to talk to (the dev build has no bundled backend):
pip install -e "../..[all]" && librarian api

# App:
cd apps/macos
make run          # debug build via swift run
```

Or `open Package.swift` in Xcode and press Run.

## Build the distributable yourself

```bash
# 1. Build the backend wheel (repo root)
python -m pip install build && python -m build --wheel

# 2. Build the self-contained app + DMG (Apple Silicon example)
cd apps/macos
make bundled-app        # app + downloads relocatable Python + installs wheel
make dmg                # dist/Librarian.dmg
```

`make bundled-app` accepts `WHEEL=...` (wheel path or PyPI requirement),
`BUNDLE_ARCH=arm64|x86_64`, and `IDENTITY="Developer ID Application: ..."` for
real signing. The bundled Python version is pinned in
`scripts/bundle_backend.sh`.

## Release pipeline (CI)

`.github/workflows/macapp.yml` builds both DMGs on a macOS runner for every
`v*` tag (and on demand via workflow dispatch) and attaches them to the GitHub
release as `Librarian-AppleSilicon.dmg` and `Librarian-Intel.dmg` — which is
what the stable `releases/latest/download/...` links point at.

Builds are ad-hoc signed by default. To ship notarized builds that open with
zero Gatekeeper friction, add these repository secrets (requires an Apple
Developer account):

| Secret | Value |
| --- | --- |
| `MACOS_SIGNING_CERT_P12_BASE64` | base64 of your Developer ID Application certificate (.p12) |
| `MACOS_SIGNING_CERT_PASSWORD` | password for the .p12 |
| `MACOS_SIGNING_IDENTITY` | e.g. `Developer ID Application: Your Name (TEAMID)` |
| `MACOS_NOTARIZATION_APPLE_ID` | Apple ID email |
| `MACOS_NOTARIZATION_TEAM_ID` | 10-character team ID |
| `MACOS_NOTARIZATION_APP_PASSWORD` | app-specific password for notarytool |

The workflow detects the secrets and switches to Developer ID signing plus
`notarytool` submission and stapling automatically — no workflow changes
needed.

A download landing page lives in [`site/`](../../site/index.html) and deploys
via GitHub Pages (`.github/workflows/pages.yml`; enable Pages with source
"GitHub Actions" in the repository settings).
