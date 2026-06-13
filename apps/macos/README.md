# Librarian for Mac

Handbrake for documents. Drop files into one window, pick a destination
folder and format, and cleaned files appear there automatically — converted,
cleaned, and classified by the engine bundled inside the app. Download, drag
to Applications, double-click, done.

## How it's organized

- **The window is the queue.** Every dropped file shows its stage (Waiting →
  Sending → Converting → Cleaning → Classifying → Saved) and ends with
  **Show in Finder** or a plain-words failure with **Retry**. The cleaned
  file is saved to your destination folder automatically under a library
  filename — the Dewey code plus an AI-generated title, like
  `636.1 Saddle Fit and Groundwork Notes.md` (your original filename if no
  title was produced). Name collisions get " (2)" appended, never
  overwritten. Markdown output opens with a short synopsis, the
  classification, and topic tags above the cleaned text.
- **Destination strip** at the top: Save to (any folder; default
  `~/Documents/Librarian`) and Format (Markdown, Plain Text, JSON).
- **Settings (⌘, or the gear)**: one pane. Provider (Anthropic / OpenAI /
  OpenAI-compatible / Ollama / None), model, API key with inline validation.
  Keys are stored in the macOS Keychain — never on disk — and handed to the
  engine through its environment. With Provider = None, files are converted
  and organized without AI cleaning, so the app works with zero setup.
  Advanced (collapsed): keep originals alongside outputs, or connect to a
  remote Librarian server.
- **Tools menu** (menu bar): Convert File, Convert Folder, Normalize
  Transcript, Find in Transcript — the CLI's file utilities.
- **Help → Diagnostics…**: engine capability checks (`doctor`), readiness,
  migrations, and log access.

## Install (for users)

1. Download the DMG for your Mac from the assets of the
   [latest release](https://github.com/nampara-ai/librarian/releases/latest):
   - **Librarian-AppleSilicon.dmg** for M1/M2/M3/M4 Macs
   - **Librarian-Intel.dmg** for Intel Macs

   (Apple menu → About This Mac shows which chip you have. DMGs are attached
   to releases from v1.1.0 onward.)
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

Scanned and image-based PDFs are OCR'd with a self-contained copy of Tesseract
and Poppler (plus English language data) bundled under
`Librarian.app/Contents/Resources/ocr`, with dependent libraries relocated into
the bundle. The app puts these on the engine's `PATH` and sets `TESSDATA_PREFIX`
at launch — so OCR works with no Homebrew install and no `PATH` setup. (macOS GUI
apps inherit only a bare system `PATH`, which is why a system-installed OCR tool
would otherwise be invisible to the engine.) Each DMG is built on a
native-architecture runner so the bundled OCR binaries match the target Mac.

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

Open **Settings (⌘,)** — or click **Set up AI cleaning…** in the empty main
window — pick a provider (**Anthropic**, **OpenAI**, **DeepSeek**,
**Ollama**, **LM Studio**, or **Custom**), paste your API key (local servers
take an address instead, pre-filled with the standard port), and press
**Connect**. The app queries the provider's live model list and shows a
model dropdown with what your account or server actually offers — picking a
model applies it immediately and pins it to every cleaning call ("Cleaning
with claude-sonnet-4-6"). A bad key fails with a clear message within a few
seconds. Until a provider is connected, files are still converted and
organized, just without AI cleaning.

API keys are stored in the macOS Keychain and passed to the engine through
its process environment — they are never written to disk. Non-secret
settings (provider, model, base URL) live in
`~/Library/Application Support/Librarian/.env`, which you can also edit by
hand; a key found in a legacy `.env` is migrated into the Keychain
automatically.

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
