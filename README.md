# Librarian

Librarian is a terminal program that turns source documents into Markdown. It runs on the user's own machine. This branch, `codex/rust-foundation`, holds the program as rebuilt in Rust. The program version is 0.1.0. It is a development build and has not been released.

## Functions

| Function | Description |
| --- | --- |
| Parse | Reads a PDF and writes a Markdown file and a JSON file of typed layout blocks. Local. No model is called. Scanned pages are read on request, by a local OCR engine |
| Clean | Queues source documents, copyedits them with a language model through a configured provider, classifies them, and publishes Markdown. Three pipelines exist. The fast pipeline, which a new configuration uses, reads a PDF with the parser, sends each passage of text to the model in one request, and accounts for every source word of the reply. The editorial pipeline renders every page to an image and reads it with OCR and the model. The archival pipeline is kept so that older jobs can resume |
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

The repository also holds the earlier Python and Swift application, version 1.10.3, under `src/`, `apps/` and `tests/`. It is kept for reference. The workflow "CI" checks it when its files change, and the workflow "Mac App" builds the Swift application when a pull request changes it. Its documents are [docs/LEGACY_README.md](docs/LEGACY_README.md), `docs/API.md`, `docs/ARCHITECTURE.md`, `docs/DEPLOYMENT.md`, `docs/OPERATIONS.md`, `docs/OKF.md` and `docs/EDITORIAL_EVALUATION.md`. The commands and settings they name are not those of the Rust program.

## Installation

Native packages for Apple Silicon macOS, Intel macOS, Windows x64 and Linux x64 are produced by the workflow [Librarian terminal builds](https://github.com/nampara-ai/librarian/actions/workflows/rust-foundation.yml). Download the artifact for the platform, unpack it, and keep the program beside its `engines` folder. Run `./librarian`, or `.\librarian.exe` on Windows.

To build from source:

```sh
cargo build --release --locked -p librarian-cli
./target/release/librarian
```

A source build carries no engines. The terminal interface downloads and verifies an engine when it is first needed; from the command line, `librarian engines install` does. LibreOffice, which is needed for office documents, must be installed separately or named in `processing.office_renderer`.

## First use

Run `librarian`. The first start asks for a workspace folder and then shows Home. PDFs can be parsed at once: press **a**. Parsing needs no account, no key and no payment. If the program was built from source, the PDF engine is downloaded and verified the first time it is needed.

Cleaning documents with AI needs a provider, a key and a model. Librarian asks for them when such work is first queued or started, or from Setup (**f**). Connecting a provider includes one test request that the provider bills, unless a recorded check still covers the provider, endpoint, key and model. The request sends a small built-in image. With the fast pipeline and image reading off it sends a built-in sentence and no image, and a model that accepts no images can then be used. The request is not repeated for other changes, with one exception: after a check that sent no image, turning image reading on or choosing another pipeline requires a new check.

From the command line, without the terminal interface:

```sh
librarian parse document.pdf -o results --create-output
```

With a source build, run `librarian engines install` once before the first `parse` from the command line.

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
librarian parse scan.pdf -o results --ocr
librarian clean source-folder -o finished --create-output --recursive
librarian run
librarian status
librarian show 12
librarian print 12
librarian changes 12
librarian search 'a phrase'
librarian export 12 -o other-folder --create-output
librarian budget 12 --max-requests 20000 --resume --start
librarian doctor
librarian --help
```

The complete list is in section 6 of the [terminal reference](docs/RUST_FOUNDATION.md).

## Properties

- A source file is never modified. When a job first runs, a copy of its source is stored in the workspace.
- A published file is never replaced.
- An API key is stored in the system credential store, or held for the session only. It is not written to the configuration file, job records, logs or command-line arguments. It is stored only when Setup is saved; leaving Setup earlier leaves the configuration and the stored keys unchanged.
- Work is recorded in a local database as it is done. An interrupted job resumes from its last checkpoint.
- A job keeps the configuration it was queued with. Changing Setup does not alter queued jobs.
- Request, token and cost ceilings apply to each job. A job that reaches a ceiling stops and keeps its work. Its ceiling can then be raised, with explicit authorization, and the job continues from its checkpoint. What was used is not reset, and each change is recorded.

## Limits

- The parser reads PDF files only. It reads scanned pages only on request: with `parse --ocr`, with the box "Read scanned pages with local OCR" of the Parse form, and in the fast pipeline of Clean when `processing.ocr` is `paddle` or `tesseract`. The reading is done by a local OCR engine, which must be installed: the bundled engine, or Tesseract. Otherwise scanned pages are reported and not read.
- In the fast pipeline no model reads a page of a PDF, and pictures in a PDF are not described. Of the office formats it reads only `docx`. The editorial pipeline reads the others.
- The editorial and archival pipelines do not use the parser.
- A status of `complete` records that processing reached its end. It does not state that the published text is correct.
- The developers' paid runs with this build were made on synthetic damage of public-domain text and on one scanned test document. They are recorded in section 9.9 of the terminal reference. No run was made on the owner's documents. Acceptance on the owner's documents and keys has not yet been carried out.

## Checking a change

```sh
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --locked -p librarian-core -p librarian-native --lib --test persistence
```

A change to the PDF parser must also be measured as described in section 8 of the [parsing reference](docs/PARSING_FOUNDATION.md).
