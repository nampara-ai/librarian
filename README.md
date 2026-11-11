# Librarian

Librarian is a terminal program that turns source documents into Markdown. It runs on the user's own machine. This branch, `codex/rust-foundation`, holds the program as rebuilt in Rust. The program version is 0.1.0. It is a development build and has not been released.

## Functions

| Function | Description |
| --- | --- |
| Parse | Reads a PDF and writes a Markdown file and a JSON file of typed layout blocks. Local. No model is called |
| Clean | Queues source documents, copyedits them with a language model through a configured provider, classifies them, and publishes Markdown |
| Search | Full-text search of the documents that Clean has published |

Both Parse and Clean are available from the command line and from the terminal interface. There is no graphical interface.

## Documents

| Document | Contents |
| --- | --- |
| [docs/RUST_FOUNDATION.md](docs/RUST_FOUNDATION.md) | The terminal reference: installation, Setup, every screen and key, every command and option, every configuration key, the pipelines, the limits |
| [docs/PARSING_FOUNDATION.md](docs/PARSING_FOUNDATION.md) | The PDF parser: output format, method, measurements, limits |
| [docs/THIRD_PARTY.md](docs/THIRD_PARTY.md) | Bundled engines, models and fonts, with versions and licenses |
| [AGENTS.md](AGENTS.md) | Rules for work on this branch |
| [docs/EDITORIAL_PIPELINE.md](docs/EDITORIAL_PIPELINE.md) | The owner's approved contract for the editorial pipeline, and the dated work log |
| [docs/IMPLEMENTATION_PROGRESS.md](docs/IMPLEMENTATION_PROGRESS.md) | Dated ledger of the first terminal implementation. Historical |
| [docs/TERMINAL_REBUILD.md](docs/TERMINAL_REBUILD.md) | Design notes written before the rebuild. Historical. Not a description of the program |

The repository also holds the earlier Python and Swift application, version 1.10.3, under `src/`, `apps/` and `tests/`. It is kept for reference and is not built or tested on this branch. Its documents are [docs/LEGACY_README.md](docs/LEGACY_README.md), `docs/API.md`, `docs/ARCHITECTURE.md`, `docs/DEPLOYMENT.md`, `docs/OPERATIONS.md`, `docs/OKF.md` and `docs/EDITORIAL_EVALUATION.md`. The commands and settings they name are not those of the Rust program.

## Installation

Native packages for Apple Silicon macOS, Intel macOS, Windows x64 and Linux x64 are produced by the workflow [Librarian terminal builds](https://github.com/nampara-ai/librarian/actions/workflows/rust-foundation.yml). Download the artifact for the platform, unpack it, and keep the program beside its `engines` folder. Run `./librarian`, or `.\librarian.exe` on Windows.

To build from source:

```sh
cargo build --release --locked -p librarian-cli
./target/release/librarian
```

A source build carries no engines. Setup downloads and verifies the PDF and OCR engines. LibreOffice, which is needed for office documents, must be installed separately or named in `processing.office_renderer`.

## First use

Run `librarian`. On first use, Setup asks for a workspace folder, a provider, an API key, a model and processing defaults. It then loads the engines and sends one small billed test request to the model. Home is shown when Setup has completed. Later starts go directly to Home.

To parse a PDF without Setup, use the command line:

```sh
librarian parse document.pdf -o results --create-output
```

## Home

| Key | Action |
| --- | --- |
| a | Parse PDFs now |
| n | Clean documents with AI |
| q | Start the queue |
| s | Search the library |
| f | Setup |
| h | Help |
| p, r, c | Pause, resume or run again, cancel the selected document |
| Up, Down | Move within a pane |
| Tab, Shift+Tab, Left, Right | Switch between the actions pane and the documents pane |
| Enter, Space | Run the action, or open the menu of the document |
| Esc | Close the terminal interface. A running worker continues |
| Ctrl+S | Save drafts, pause all work in the workspace, and exit |

## Commands

```sh
librarian parse document.pdf -o results --create-output
librarian parse folder -o results --create-output --recursive
librarian clean source-folder -o finished --create-output --recursive
librarian run
librarian status
librarian show 12
librarian print 12
librarian search 'a phrase'
librarian export 12 -o other-folder --create-output
librarian doctor
librarian --help
```

The complete list is in section 6 of the [terminal reference](docs/RUST_FOUNDATION.md).

## Properties

- A source file is never modified. A copy of each imported source is kept in the workspace.
- A published file is never replaced.
- An API key is stored in the system credential store, or held for the session only. It is not written to the configuration file, job records, logs or command-line arguments.
- Work is recorded in a local database as it is done. An interrupted job resumes from its last checkpoint.
- A job keeps the configuration it was queued with. Changing Setup does not alter queued jobs.
- Request, token and cost ceilings apply to each job. A job that reaches a ceiling stops and may be resumed.

## Limits

- The parser reads PDF files only, and does not read scanned pages. It reports them.
- Clean does not yet use the parser. It renders every page to an image and reads it with OCR and the model.
- The terminal interface requires Setup, including a provider and a key, before Home is shown. This applies to Parse in the terminal interface also.
- A status of `complete` records that processing reached its end. It does not state that the published text is correct.
- The developers have made no paid document runs with this build. Acceptance on the owner's documents and keys has not yet been carried out.

## Checking a change

```sh
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --locked -p librarian-core -p librarian-native --lib --test persistence
```

A change to the PDF parser must also be measured as described in section 8 of the [parsing reference](docs/PARSING_FOUNDATION.md).
