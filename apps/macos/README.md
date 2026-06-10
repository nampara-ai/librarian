# Librarian for Mac

A native SwiftUI companion app for the Librarian backend. Drag files in, watch
them get converted, cleaned, and classified, and read or export the results —
the app is a pure consumer of the Librarian HTTP API and its run-event
lifecycle.

## Features

- **Drag and drop** files or folders anywhere in the window. Each file is
  uploaded with `POST /documents` and queued for processing with `POST /runs`.
- **Live progress** in the activity panel: per-run stage, chunk progress, and
  expandable run events straight from `GET /runs/{id}/events/records`.
- **Outputs** in the detail pane: cleaned text with its Dewey classification,
  plus copy, Save As… (Markdown), reprocess, and delete.
- **Full-text search** over the cleaned library from the sidebar search field
  (`POST /search/results`), with highlighted snippets.
- **Backend checklist**: the status pill in the toolbar shows `/ready`
  diagnostics (database, storage, migrations) and the exact command to start
  the backend when it is offline.

## Requirements

- macOS 14 or newer
- Xcode 15+ (or just the Command Line Tools with a recent Swift toolchain)
- A running Librarian backend:

```bash
pip install "nampara-librarian[all]"
librarian init
librarian api          # serves http://127.0.0.1:8080
```

## Run it

```bash
cd apps/macos
make run               # swift run, debug build
```

Or open the package in Xcode (`open Package.swift`) and press Run.

## Build a .app bundle

```bash
cd apps/macos
make app               # produces dist/Librarian.app (ad-hoc signed)
open dist
```

Drag `Librarian.app` to /Applications if you want it around.

## Configuration

Server URL and API key live in **Settings (⌘,)**. The defaults point at
`http://127.0.0.1:8080` with no API key, which matches a local
`librarian api`. If the backend uses `LIBRARIAN_API_KEY`, paste the same value
into the API key field — it is sent as `x-api-key`.
