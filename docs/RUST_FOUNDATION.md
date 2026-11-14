# Librarian terminal reference

This document describes the Librarian terminal program as it is built from the branch `codex/rust-foundation`. It covers installation, the files the program keeps, Setup, every screen and key of the terminal interface, every command and option, every configuration key, the processing pipelines, and the known limits. The PDF parser has its own document, [PARSING_FOUNDATION.md](PARSING_FOUNDATION.md). Bundled components are listed in THIRD_PARTY.md.

The program version is 0.1.0. It is a development build. It has not been released.

## 1. What the program does

Librarian has two independent functions.

1. **Parse.** A PDF file is read locally and converted to a Markdown file and a JSON file of typed layout blocks. No provider is contacted and no model is called.
2. **Clean.** Source documents are queued, read page by page, copyedited by a language model through a configured provider, classified, and published as Markdown. Finished documents are indexed for full-text search.

Both functions are available from the command line and from the terminal interface. There is no graphical interface.

## 2. Installation

### 2.1 Native packages

Packages are built by the workflow "Librarian terminal builds" for four targets.

| Target | Platform |
| --- | --- |
| `aarch64-apple-darwin` | macOS, Apple Silicon |
| `x86_64-apple-darwin` | macOS, Intel |
| `x86_64-pc-windows-msvc` | Windows, x64 |
| `x86_64-unknown-linux-gnu` | Linux, x64, built on Ubuntu 22.04 |

Each package contains the program, an `engines` folder, this document as `README.md`, `PARSING_FOUNDATION.md`, `THIRD_PARTY.md`, `LICENSE`, `RUST-LICENSES.txt`, a `licenses` folder, the role prompts in `prompts/`, `Cargo.lock`, and `build.json`. The `engines` folder holds PDFium, ONNX Runtime, the two OCR models and a private LibreOffice. The Windows package also holds the Visual C++ runtime libraries.

Unpack the package. Keep the program beside its `engines` folder. Run `./librarian`, or `.\librarian.exe` on Windows. No Python or Node installation is required.

### 2.2 Building from source

The Rust toolchain is pinned to 1.96.0 in `rust-toolchain.toml`.

```sh
cargo build --release --locked -p librarian-cli
./target/release/librarian
```

A source build carries no engines. The PDF engine is downloaded into the workspace, and verified against its pinned checksum, the first time a PDF is parsed. ONNX Runtime and the OCR models are downloaded the first time a queue that uses the bundled OCR is started. `librarian engines install` installs all of them at once. LibreOffice is not downloaded. To process office documents with a source build, install LibreOffice or set `processing.office_renderer` to the `soffice` executable.

## 3. Workspace and files

### 3.1 Location

The workspace is the folder in which the program keeps its configuration, originals, queue and search index. It is chosen in Setup.

The default workspace is the per-user local application data folder for `librarian-terminal`. On Linux this is `~/.local/share/librarian-terminal`. The command `librarian config path` prints the configuration file in use.

The file `active-profile.json` in the default workspace folder records the workspace and configuration file chosen in Setup. Every later start of the program reads it. The option `--library FOLDER` selects another workspace for one invocation, and with it the configuration file `FOLDER/config.toml`. The option `--config` selects another configuration file for one invocation. No Librarian variable selects the workspace. The default folder is the platform's per-user data folder; on Linux it follows `XDG_DATA_HOME`.

Every command except `parse`, `convert`, `inspect`, `prompts`, `config` and `setup` creates the workspace folder, its `originals`, `assets` and `engines` folders and its database if they are absent.

### 3.2 Contents

| Path in the workspace | Purpose |
| --- | --- |
| `config.toml` | Configuration |
| `verification.json` | What has been verified for this workspace: the last successful connection check, with the fingerprint of what it covered, and the last successful load of the local engines |
| `setup-complete.json` | Written by earlier releases. Read once, to carry an earlier verification forward. No longer written |
| `pending-credential.json` | Present only while a key is being stored. Names the key's account, never the key. See section 4.3 |
| `library.sqlite3` | Queue, checkpoints, caches, catalog and search index. The files `library.sqlite3-wal` and `library.sqlite3-shm` beside it belong to the database |
| `worker.lock` | Held by the one worker that may process the queue |
| `worker.log` | Error output of a worker started from the terminal interface |
| `originals/` | One unmodified copy of every imported source, named by content hash |
| `assets/` | Extracted and rendered images, named by content hash |
| `engines/` | Installed PDF and OCR engines, each with `manifest.json` |
| `.librarian-work/job-N/pages/` | For each page of job N, numbered with six digits: the image (`.png`), a reduced copy for the model (`.model-WIDTH.png`), and `.ocr.json`, `.visual.md`, `.copyedited.md`, `.typeset.md` |
| `.librarian-work/job-N/office/` | The PDF made from an office document, and `conversion.log` |
| `.librarian-work/job-N/editorial-report.json` | Diagnostics of the editorial run |
| `prepared/` | Destination used by the `prepare` command |
| `setup-draft.json` | Answers of an unfinished run of the provider section or of the full sequence, kept in the workspace that was active when Setup began. Holds no key. Removed when the section is saved, or when it is left with nothing answered |
| `import-draft.json` | Unsubmitted "Clean documents with AI" form |
| `parse-draft.json` | Last state of the "Parse PDFs" form |
| `parse-history.json` | The 20 most recent parse results |

A source file is never modified. A published file is never replaced.

### 3.3 Environment variables

| Variable | Use |
| --- | --- |
| The name held in `provider.key_env` | The API key, when the configuration holds no credential reference |
| `LIBRARIAN_SESSION_<ACCOUNT>` | A session key, set by the terminal interface for the worker it starts |
| `VISUAL`, `EDITOR` | The editor started by `config edit` |
| `HOME`, `USERPROFILE` | The home folder that `~` stands for in a typed path |
| `ProgramFiles`, `ProgramFiles(x86)`, `PATH` | Searched for LibreOffice |

LibreOffice is started with `SAL_USE_VCLPLUGIN=svp` and, on Linux, with `LD_LIBRARY_PATH` set to its own libraries.

## 4. Setup

Setup is in parts. Each part is asked for when it is needed, and each can be opened on its own.

| Part | Asked for | Steps |
| --- | --- | --- |
| Workspace | At the first start, before Home | Welcome; choose the folder; prepare it |
| AI provider | When work that calls a model is first queued, or from the Setup menu | Provider; key; whether to remember the key; model list; model; per-document limit; connection check; save |
| Processing defaults | From the Setup menu | One screen |
| Markdown output defaults | From the Setup menu | One screen |
| Local engines | When an engine is first needed, or from the Setup menu | One automatic step |
| Every step in order | From the Setup menu, or `librarian setup` | All of the above |

The first start of the terminal interface asks for the workspace only. Home is then shown, and PDFs can be parsed at once. No provider, key or payment is involved until work that calls a model is chosen.

### 4.1 The Setup menu

Press **f** on Home. The menu shows three lines of state and the parts.

| Line | Possible states |
| --- | --- |
| Workspace | The folder |
| AI provider | "not connected"; the provider and model followed by "connection verified DATE", "not verified", or "the session key has not been entered in this session"; or a note that it is not used because editing, classification and visual reading are off |
| Local engines | "PDF and OCR engines installed"; "PDF engine installed; OCR engine not installed"; "not installed; installed when first needed" |

| Entry | Action |
| --- | --- |
| Connect an AI provider, or Change the AI provider, key or model | The provider part |
| Test the connection again (one billed request) | Shown when a provider is connected. See section 4.4 |
| Processing defaults | Copyediting, classification, visual reading, OCR engine, Tesseract language, house style, pipeline, pages read at once, class override, office renderer |
| Markdown output defaults | Front matter, title, synopsis, page markers, review notes, file name pattern |
| Install or check the local engines | Loads the engines the configuration uses and installs those that are missing |
| Change the workspace | The workspace part, without the welcome |
| Go through every step in order | The full sequence |

### 4.2 Navigation and saving

Esc means Back on every screen of Setup. It returns to the previous question. Esc at the first screen of a part leaves the part.

An automatic step shows its progress. After it completes, **x**, Enter or Space continues. Esc while it runs stops it and goes back. Esc after it has completed goes back and keeps the result. Going back never lands on an automatic step: a step whose result still holds is passed over in both directions, and one whose result no longer holds runs again on the way forward.

A result holds for as long as what it depends on is unchanged.

| Result | Depends on |
| --- | --- |
| Workspace prepared | The workspace folder |
| Model list | The provider, the endpoint, the protocol and the key |
| Engines loaded | The workspace folder and the OCR engine setting |
| Connection check | The fingerprint of section 4.4, and no key having been entered since |

A step that fails shows the reason and offers "Try this step again" and "Back". A failed step cannot be passed.

Nothing outside the memory of the program is changed until a part is saved. The provider part and the full sequence end with a summary and one entry, "Save". The single-screen parts end with "Save and return". Leaving a part before that leaves the configuration in force, its key, and the queued jobs exactly as they were. Each job keeps the configuration it was queued with; Setup never alters a queued job.

Ctrl+S during the provider part or the full sequence keeps the answers given so far in `setup-draft.json`. The next run of that part offers to resume them. A key is never kept in the draft; a key entered before the interruption must be entered again.

### 4.3 Keys

The key step offers the entries that apply.

| Entry | Shown when |
| --- | --- |
| Use the key entered a moment ago | A key was entered earlier in this run |
| Use the saved key | The configuration refers to a stored key, or to a session key present in this process |
| Paste a new API key | Always |
| Import the key from VARIABLE | The variable named by `provider.key_env` is set. Setup names one only for OpenAI (`OPENAI_API_KEY`) and Anthropic (`ANTHROPIC_API_KEY`) |
| This local server needs no key | The endpoint is on the local host |
| Open the provider's API-key page | The preset has one. It is opened with the system's `open`, `xdg-open` or `rundll32` command |

A pasted key is shown as dots and may not contain white space. It is held in the memory of the program under a new reference. The next step asks whether to remember it.

| Choice | Effect when the part is saved |
| --- | --- |
| Remember it in the system credential store | The key is written to the store under the service name `ai.nampara.librarian.terminal` and a new account name, and read back to confirm it. The stores are Keychain on macOS, Credential Manager on Windows and Secret Service on Linux |
| Keep it for this session only | Nothing is stored. The key stays in memory, and a worker started from the terminal interface receives it through an environment variable of the worker process. It is asked for again at the next start |
| Reconnect the previous session key, including its queued jobs | Shown when the configuration in force uses a session key. The entered key is placed under that earlier reference, so that jobs queued under it can run |

Saving is one transaction, in this order: the key is written to the store, if it is to be remembered; the configuration is written; the verification record is written; the workspace is recorded as the active one.

- If the store is not available, nothing is written and the part is not saved. The screen offers to try again, to keep the key for the session only and save, or to go back.
- If the configuration cannot be written, the key written a moment before is removed again, and the earlier configuration and its key stay in force.
- If the program stops between writing the key and writing the configuration, the file `pending-credential.json` names the account. At the next start the program removes that key, because no configuration refers to it, and removes the file. If a configuration does refer to it, only the file is removed.

A key that existed before is never changed or removed. Every entered key has a new account name, so an earlier key stays available to jobs queued with it.

The configuration file holds a reference to the key: the account name, the endpoint and whether it is a session key. The key itself is not written to the configuration file, job records, logs, drafts or command-line arguments. A saved key is bound to its endpoint and is refused for any other. For scripted use without Setup, the key may be supplied in the environment variable named by `provider.key_env`.

### 4.4 The connection check

The connection check sends one request to the provider from a separate worker process: one small built-in image and at most 1,024 generated tokens. The provider bills this request. The check confirms the key, image input and a structured reply. It does not measure the quality of document processing. The screen states the billing before the request is sent. The check is stopped after `timeout_seconds` plus 30 seconds.

A successful check is recorded in `verification.json` with a fingerprint. The fingerprint covers the protocol (`kind`), the endpoint, the account name of the key (or the name of the environment variable when no key is stored), `model`, `vision_model` and `structured_output`.

| Change | New check required |
| --- | --- |
| Output defaults, processing defaults, house style, OCR settings | No |
| Request, token and cost limits; prices; concurrency; timeouts; rate limits | No |
| Storing a session key in the credential store under the same account | No |
| Provider, endpoint or protocol | Yes |
| A key entered again or replaced | Yes |
| Model, vision model, or structured-output mode | Yes |
| A workspace that has no record of its own | Yes |

When a check is on record and still covers the configuration, the provider part shows its date and offers "Continue without a new request". It also offers "Test the connection again (one billed request)". The provider part cannot be saved without a check that covers it.

"Test the connection again" in the Setup menu sends the same request for the configuration in force. A pass is recorded. A failure withdraws the record, and work that calls a model then asks for a successful check first.

Verified provider access, loaded local engines, and the quality of a processed document are three separate things. The first two are recorded separately in `verification.json`. Nothing records the third.

### 4.5 Other particulars

- **Provider list.** Ten presets carry a published vision score dated 2026-11-06 from `arena.ai/leaderboard/chat/vision` and are sorted by it. The other thirteen (gateways, hosting providers, local servers and the custom endpoint) follow, labelled "unranked / catalog-dependent". The score is a sorting aid and not a statement of quality. In the full sequence the list ends with "Leave the AI provider for later (parsing needs none)".
- **Custom endpoint.** The entry asks for a URL and a protocol. Both open on the present values. Changing the URL clears the key and the model.
- **Returning to the provider in force.** Choosing another preset and then the original one restores the original settings, including its key reference and model.
- **Model list.** The list is requested from `{endpoint}/models` (xAI and Fireworks use their own list addresses), with a time limit of 45 seconds and at most 100 pages. Entries marked inactive, entries for embedding, image, audio, moderation or reranking models, and entries that produce no text are dropped. The list always comes from the provider. If the request fails, Setup reports the failure and does not substitute a stored list. Models that report no image input are omitted. A model that does not report its image capability is labelled "image support unverified" and is tested by the connection check. A model with a context below 16,384 tokens or an output limit below 1,024 tokens is refused. "Ask the provider for the list again" repeats the request.
- **Prices.** Prices are shown only where the provider's list supplies them (OpenRouter, xAI) and for one Anthropic model with a recorded rate. A missing price is not treated as zero. Without prices no dollar limit can be set, and a limit on generated tokens is offered instead.
- **Values set by the provider part.** Choosing a preset sets `catalog_id`, `base_url`, `kind`, `structured_output`, `key_env` and `prompt_cache`, and clears the prices and `max_cost`. Choosing a model sets `model` and `vision_model`, sets `context_tokens` to the reported context but at most 32,768, sets `max_output_tokens` to the reported limit within 256 to 8,192 and at most a quarter of the context, and copies any prices. Entering a key sets `credential` and sets `namespace` to the account name.
- **Changing the workspace.** A folder that already holds a configuration is opened with its own settings. A new folder starts from the settings in force; it has no verification record, so its first use of the provider asks for a connection check.
- **`librarian setup`.** The command runs the full sequence and exits when it is saved. It does not open Home.
- **Workspaces set up by earlier releases.** Their one verification stamp is honoured for as long as the configuration it covered is unchanged.

## 5. Terminal interface

Start the terminal interface with `librarian` or `librarian tui`. It requires an interactive terminal. At the first start it asks for the workspace and then shows Home.

### 5.1 Home

Home has two panes. The left pane, "What do you want to do?", lists six actions. The right pane, "Documents · newest first", lists the jobs of the workspace. Below them are a detail panel for the selected document, a "Now" panel for the running job, a line of key hints, and a message line. The header shows counts of done, to-review, queued and running documents, and the workspace path. The screen refreshes ten times a second. The 1,000 most recent jobs are listed and counted.

Each document line shows the job number, the file name, the status and, while it differs from the status, the stage (section 9.5). With no document in the workspace, the detail panel shows the state of the AI provider, the state of the local engines and the last parse. The detail panel of a document shows any error, the status and stage, the counts of pages rendered, read by OCR, read visually, copyedited, assembled and reviewed, the source, the destination, the number of model calls and of calls answered from the cache, the tokens in and out, and the cost where prices are known. The descriptions of the actions and the longer key hints are shown only when the terminal is large enough. When the terminal interface opens, the message line shows the last parse.

| Key | Action |
| --- | --- |
| Up, Down | Move within the focused pane |
| Tab, Shift+Tab | Switch panes |
| Left | Focus the actions pane |
| Right | Focus the documents pane |
| Enter, Space | Run the focused action, or open the menu of the focused document |
| a | Parse PDFs now |
| n | Clean documents with AI |
| q | Start the queue |
| s | Search the library |
| f | Setup |
| h | Help |
| p | Pause the selected document |
| r | Resume the selected document, or run it again |
| c | Cancel the selected document |
| Esc | Close the terminal interface. A running worker continues |
| Ctrl+S | Save drafts, pause all queued and running jobs in the workspace, and exit |

Letter keys are lower case. They act from either pane. The keys **p**, **r** and **c** act on the document highlighted in the right pane, whichever pane has focus. The pane cannot be switched while the document list is empty. Ctrl+C has no function in the terminal interface.

**q** starts one background worker for the workspace. If nothing is queued, no worker is started and the message line says so. If a worker is already running, none is started. Before the worker starts, the engines the queued jobs use are installed if they are missing: the PDF engine, and the OCR runtime and models when a queued job uses the bundled OCR. If they cannot be installed the worker is not started and the jobs stay queued.

**r** resumes a paused or cancelled document from its last checkpoint. For a complete, needs-review, prepared or failed document it queues a new job from the retained original, using the current configuration, and keeps the earlier result; if that configuration calls a model and the provider is not ready, the provider is asked for first. For a document stopped at a limit it opens the limits screen of section 5.3. In each case the worker is then started.

### 5.2 Document menu

Enter or Space on a document opens its menu. The entries shown depend on the state of the job.

| Entry | Shown when the job is |
| --- | --- |
| Read the result | Published |
| Read diagnostics and events | Any state |
| Pause (committed work is kept) | Queued or running |
| Raise a limit and resume | Stopped at a limit |
| Resume from the last checkpoint | Paused, cancelled or stopped at a limit. For a document stopped at a limit this opens the limits screen, which explains why it cannot resume as it is |
| Run again with current settings (keeps this result) | Complete, needs review, prepared or failed |
| Cancel (the original is kept) | Queued, running or paused |
| Back to Home | Any state |

"Read the result" shows the first 64 KiB of the published file. "Read diagnostics and events" shows the 60 most recent events of the job.

### 5.3 Limits of a document

A document that reaches its request ceiling, its output-token ceiling or its cost ceiling stops with the status `budget_exhausted`. Its work is kept. It cannot continue until the ceiling is raised, because it keeps the ceilings it was queued with and its counts are never reset.

The limits screen is opened by **r** on such a document, or from its menu. It states which ceiling was reached, and shows for requests, output tokens and estimated cost the ceiling and the amount used. An amount marked "held" is reserved for a request whose outcome the provider never reported; it counts against the ceiling.

For each ceiling that stops the document the screen offers a suggested higher value and "another value". Choosing one opens a second screen, "Authorize a higher ceiling". It names the document, the old and new ceiling, the provider and model that further requests go to, and what has been used. Its entries are "Authorize and resume" and "Back"; the selection starts on "Back". Nothing is changed until "Authorize and resume" is chosen.

On authorization the ceiling is raised for that document only, the change is recorded with the document, the document returns to the queue and the worker is started. It continues from its last checkpoint. Completed work, cached replies and the counts of requests, tokens and cost are untouched. A ceiling can only be raised. The record of each change holds the ceiling, the old and new value, what had been used at the time, when, and whether it was authorized in the terminal interface or from the command line. It is shown by `librarian budget ID`, and as an event of the document.

The command `librarian budget` does the same from the command line. See section 6.2.

### 5.4 Parse PDFs

Press **a** on Home. Parsing needs the workspace and the PDF engine only. If the PDF engine is neither supplied with the program nor installed in the workspace, it is downloaded and verified when "Parse now" is first chosen.

| Field | Meaning |
| --- | --- |
| 1 Source | A PDF file, or a folder of PDF files |
| 2 Destination | The folder for the results. It is created if it does not exist |
| Include PDFs in subfolders | Applies when the source is a folder |
| Keep headers, footers and page numbers | Keeps them in the Markdown |
| Plain text: no bold, italic or link markup | Omits inline markup and picture markers |
| Include word positions in the JSON | Adds positioned words, fonts and bookmarks |
| 3 Parse now | Runs the parser |
| Earlier results (N) | Lists up to 20 earlier results of this workspace |

After a source is chosen in the browser, focus moves to the destination. While parsing, the screen shows the file in hand. Esc stops the run before the next file; files already written are kept and stay listed under "Earlier results", with the note "Stopped before N file(s) were parsed". The results screen shows the number of files and pages, the elapsed time, the count of each block type, the number of pages by next step, the results folder, and any file that could not be parsed with its reason. Enter on a file shows the first 128 KiB of its Markdown. "Parse something else" returns to the form. "Back to Home" returns to Home. Esc returns to Home from new results and to the form from an earlier result.

The form has no control for page markers. The command `librarian parse --page-markers` provides them.

### 5.5 Clean documents with AI

Press **n** on Home. If the jobs to be queued call a model and the provider is not ready, "Queue import" first shows why (no provider connected, a session key not yet entered, or a provider not verified since it changed) and offers "Connect the AI provider now", which runs the provider part of Setup, and "Back", which returns to the form. An import with "Extraction only" set calls no model and needs no provider.

| Field | Default | Meaning |
| --- | --- | --- |
| 1 Source | empty | A file or folder |
| 2 Destination | empty | The folder for the finished Markdown |
| Include subfolders | off | Applies when the source is a folder |
| Create the destination if it is missing | off | |
| Extraction only (no model calls) | off | Turns off copyediting, classification and visual reading for these jobs |
| Body-only Markdown (no title, summary or front matter) | off | Also omits page markers and review notes |
| Start processing as soon as it is queued | on | Starts the worker after queueing |
| 3 Queue import | | Queues one job per file |

Every regular file under the source is queued, whatever its type. Symbolic links are skipped. The destination and the workspace are skipped if they lie inside the source. A source folder and the destination must differ. The destination must be a folder that can be written to. A file of an unsupported type fails when its job runs, and its original is kept. An empty file is refused.

### 5.6 Search

Press **s** on Home. Type the words and press Enter. Every word must occur in the same indexed passage, or in the title. Each result is one passage, so a document may appear more than once. Up to 30 results are shown, each with the job number, the title and an excerpt. Results are text only and cannot be opened from this screen.

### 5.7 Keys common to all screens

**Lists.**

| Key | Action |
| --- | --- |
| Up, Down, Left, Right, Tab, Shift+Tab | Move |
| Enter, Space | Choose |
| Any other character | Add to the filter. Rows not containing the filter text are hidden |
| Backspace | Remove one filter character |
| PgUp, PgDn | Scroll the text above the list |
| Esc | Back |

**Forms** (Parse PDFs, Clean documents with AI).

| Key | Action |
| --- | --- |
| Up, Down, Left, Right, Tab, Shift+Tab | Move |
| Enter, Space | Activate a button, switch a toggle, or browse from a path field |
| Ctrl+B | Browse, when the source or destination field has focus |
| e | Type a path, when the source or destination field has focus |
| PgUp, PgDn | Scroll the text above the form |
| Esc | Back. The form is saved |

**File browser.**

| Key | Action |
| --- | --- |
| Up, Down, Left, Right, Tab, Shift+Tab | Move |
| Enter | Open the folder, or select the file |
| Space | Select the folder currently shown |
| Backspace | Go to the parent folder |
| / | Type a path. `~` stands for the home folder |
| n | Create a folder. Available when a destination or workspace is being chosen |
| Esc | Return through the folders visited, then leave the browser |

Symbolic links are not listed. Folders are listed first. When a destination or workspace is being chosen, only folders are listed. A folder that is created is entered at once. When a source is being chosen, a path typed after `/` may name a file.

**Text fields.**

| Key | Action |
| --- | --- |
| Enter | Accept |
| Esc | Discard the edit |
| Left, Right, Home, End | Move the cursor |
| Backspace, Delete | Erase |
| Ctrl+U | Clear the field |

Spaces are ordinary characters. Home shortcuts do not act inside a field. Pasted text is accepted up to a field length of 16,384 bytes; a longer paste is ignored. A key field shows dots in place of the characters. A list filter holds at most 128 bytes.

**Reading screens** (help, results, diagnostics, messages).

| Key | Action |
| --- | --- |
| Up, Down, Left, Right, Tab, Shift+Tab | Scroll one line |
| PgUp, PgDn | Scroll fifteen lines |
| Home | Return to the top |
| Enter, Space, Esc, x | Close |

**Automatic steps.** After completion, **x**, Enter or Space continues. Esc while the step runs stops it and goes back. Esc after it has completed goes back and keeps its result.

**Ctrl+S** acts on every screen. Accepted answers and saved forms are kept. An edit not yet accepted, and a key not yet saved, are lost. All queued and running jobs of the workspace are set to paused. The program exits with status 0. A running worker stops at its next checkpoint.

## 6. Commands

```
librarian [--library FOLDER] [--config FILE] [--json] [COMMAND]
```

With no command the terminal interface is started. `librarian COMMAND --help` prints the options of a command. Two items do not appear in help: the command `setup-verify`, which the connection check runs in a separate process, and the option `parse --pdfium FILE`, which names the PDFium library to load and is used by the measurement scripts.

| Global option | Meaning |
| --- | --- |
| `--library FOLDER` | Use this workspace instead of the active one |
| `--config FILE` | Use this configuration file instead of `config.toml` in the workspace |
| `--json` | Print JSON in place of text. It changes the output of `clean`, `run`, `search`, `prompts ROLE` and `parse -o`, and makes `status --watch` print instead of opening the terminal interface. Most other commands print JSON in any case |
| `-V`, `--version` | Print the program version |
| `-h`, `--help` | Print help |

### 6.1 Parsing

| Command | Action |
| --- | --- |
| `parse SOURCE` | Parse one PDF and print its Markdown. A JSON summary is printed on the error stream |
| `parse SOURCE -o FOLDER` | Parse a PDF, or every PDF in a folder, and write `NAME.md` and `NAME.layout.json` |

| Option | Meaning |
| --- | --- |
| `-o`, `--output FOLDER` | Folder for the results. Required when the source is a folder |
| `--create-output` | Create the folder if it does not exist |
| `-r`, `--recursive` | Include PDFs in subfolders |
| `--furniture` | Keep running headers, footers and page numbers in the Markdown |
| `--page-markers` | Write `<!-- page N -->` before the first element of each page. No marker is written where a paragraph runs on across the boundary |
| `--plain` | Omit bold, italic, code and link markup and picture markers |
| `--words` | Add positioned words, fonts and bookmarks to the JSON |

Only files with the extension `.pdf`, in any letter case, are read. A folder holding no PDF is an error. An existing result file is not replaced; that file is reported as a failure. All results of a recursive run go into the one folder, so two PDFs of the same name in different subfolders collide, and the second is reported as a failure. One unreadable file does not stop a folder. With `-o` and `--json`, one JSON line is printed per file. Without `-o`, `--json` and `--words` have no effect. The exit status is 1 if any file failed. With a source build, run `librarian engines install` once before the first parse. See [PARSING_FOUNDATION.md](PARSING_FOUNDATION.md).

### 6.2 Cleaning and the queue

| Command | Action |
| --- | --- |
| `clean [SOURCE] [-o FOLDER]` | Queue the source, then process the whole queue. On a terminal, a missing source or destination is chosen in the file browser, and a destination that does not exist is created after a question |
| `run` | Process the queue. Work interrupted earlier is resumed |
| `status` | Print the 1,000 most recent jobs as JSON |
| `status --watch` | Open the terminal interface |
| `tui` | Open the terminal interface. The same as giving no command |
| `pause [ID]` | Pause one job, or every queued and running job |
| `resume [ID]` | Return one job, or every paused, cancelled and failed job, to the queue. A job stopped at a ceiling is returned only if its ceilings now allow another request. Otherwise it is left as it is and listed under `blocked`, with the reason and the `budget` command that raises the ceiling |
| `budget ID` | Print the ceilings of the job, what it has used, which ceiling stops it, and the record of every change to its ceilings, as JSON |
| `budget ID --max-requests N` | Raise a ceiling of that job. Also `--max-generated-tokens N` and `--max-cost DOLLARS`. Naming the new ceiling is the authorization. A ceiling can only be raised; the job must not be running; a cost ceiling can be raised only where one exists |
| `retry ID` | Queue a new job from the retained original with the current configuration |
| `cancel [ID]` | Cancel one job, or every queued, running and paused job |
| `prepare SOURCE` | Render page images and run local OCR only, for a file or for each file directly in a folder. No model call, no publication. The job ends as `prepared` |

| Option | Command | Meaning |
| --- | --- | --- |
| `-o`, `--output FOLDER` | `clean` | Destination folder |
| `--recursive` | `clean` | Include files in subfolders |
| `--create-output` | `clean` | Create the destination if it does not exist |
| `--extract-only` | `clean` | No copyediting, classification or image requests |
| `--no-vision` | `clean` | No image requests |
| `--body-only` | `clean` | Omit front matter, title, synopsis, page markers and review notes |
| `--queue-only` | `clean` | Queue without starting the worker |
| `--quiet` | `run` | No progress display and no result lines |
| `--start` | `resume`, `retry`, `budget` | Start the worker afterwards and wait for it |
| `--resume` | `budget` | Return the job to the queue after raising its ceiling |

`pause` and `cancel` print the number of jobs changed; `resume` prints the number resumed and the jobs left blocked. A job not in a state the command can change is left as it is, and the exit status is 0. `clean`, `run` and `prepare` do not require Setup and do not check that the provider is verified; they require a usable configuration and, for model work, a key.

Raising a ceiling changes that one job. Its completed work, its cached replies and its counts of requests, tokens and cost are untouched, and the change is recorded with the consumption at the time and the words "from the command line". `resume ID` for a job that a ceiling still stops prints the reason on the error stream and exits with status 1.

Only one worker may process a workspace at a time. Jobs run one at a time in order of job number. A worker on a terminal shows a progress line. On an interrupt signal the worker pauses all queued and running jobs and stops; committed work is kept.

### 6.3 Inspection

| Command | Output |
| --- | --- |
| `show ID` | The job, its metadata, its page counts and its last 100 events, as JSON |
| `pages ID` | A JSON array with one object per page: image path, size of the native text in bytes, and whether OCR, visual reading and copyedit are done |
| `pages ID --page N` | The contents of page N: image path, native text, OCR, visual reading, copyedit |
| `print ID` | The published Markdown |
| `search QUERY [-l N]` | Passages containing every word of the query. The default limit is 20 and the largest is 1,000 |
| `catalog` | Complete and needs-review jobs with metadata, class and accession number, as JSON |
| `stats` | Documents, requests and tokens by status, and documents by class, as JSON |
| `editorial ID` | The role contract, the reviewed segments with their `review:N` identifiers, the request packets, replies, revisions, interventions and expert feedback of an editorial run, as JSON |
| `prompts` | The role names, versions and SHA-256 hashes |
| `prompts ROLE` | The text of `copyeditor`, `reviewer` or `protocol`. The version name is also accepted |

### 6.4 Publication and feedback

| Command | Action |
| --- | --- |
| `export ID -o FOLDER [--create-output]` | Publish a complete or needs-review job again as a new job, to another folder, with the current output settings. No model call. Not possible while a worker is running |
| `feedback ID --segment review:N --replacement FILE --reason TEXT` | Record an expert correction in the workspace database |

`feedback` stores the source evidence, the model's text, the corrected text and the reason. It does not change the published file, any prompt or any model.

### 6.5 Configuration and engines

| Command | Action |
| --- | --- |
| `setup` | Go through every Setup step in order. See section 4 |
| `config show` | Print the configuration as TOML |
| `config path` | Print the path of the configuration file |
| `config schema` | Print the JSON Schema of the configuration |
| `config set KEY VALUE` | Set one value. The key has the form `section.field`, or `taxonomy.CODE`, `output.fields.NAME` or `processing.glossary.TERM`. A value that is valid JSON is read as JSON; other text is a string |
| `config edit` | Open the file in the editor named by `VISUAL` or `EDITOR`, then validate it. The file is created if absent. The default editor is `vi`, or `notepad` on Windows |
| `config reset` | Copy an existing file to `config.toml.backup-SECONDS`, then write the defaults |
| `engines install` | Download, verify and install the engines into the workspace |
| `engines list` | Print the pinned engine assets for this platform |
| `doctor` | Print the workspace, configuration path, platform, provider protocol, whether a model and a credential are configured, the pinned engine assets and the office renderer found. It does not check that the engines are installed. No provider request is made |
| `doctor --load-engines` | In addition, load PDFium and the OCR models |

### 6.6 Direct conversion

| Command | Action |
| --- | --- |
| `convert SOURCE [-o FILE]` | Convert a `.txt`, `.md` or `.markdown` file of at most 16 MiB to normalized Markdown, without the queue. The output file must not exist and its name must end in `.md` |
| `inspect SOURCE` | Print Markdown syntax counts for such a file |

### 6.7 Exit status

| Status | Meaning |
| --- | --- |
| 0 | Success. Also an exit by Ctrl+S, and output written to a closed pipe |
| 1 | An error; a job that failed or stopped at a ceiling; `parse` with a file that failed; `resume ID` for a job that a ceiling still stops |
| 2 | A job that needs review. Also an error in the command line |
| 3 | A job that was paused or cancelled |

Job statuses are returned by `clean`, `run`, `prepare`, `resume --start` and `retry --start`. The jobs considered are: for `clean` and `prepare`, those queued by the command; for `run`, those queued or running when it started; for `resume --start`, all queued jobs; for `retry --start`, the new job. If any of them failed or stopped at its budget the status is 1. Otherwise it is 2 or 3 according to the last such job in the list.

## 7. Configuration

The configuration file is TOML. Unknown keys are rejected. A missing file yields the defaults. `version` must be 1.

### 7.1 `[provider]`

| Key | Default | Meaning |
| --- | --- | --- |
| `catalog_id` | `openai` | The preset chosen in Setup |
| `kind` | `responses` | Protocol: `responses`, `chat` or `anthropic` |
| `base_url` | `https://api.openai.com/v1` | Endpoint. HTTPS is required except for a local host. It may hold no user name, password or query |
| `structured_output` | `schema` | How structured replies are requested: `schema`, `json` or `prompt` |
| `model` | empty | Model for text |
| `vision_model` | empty | Model for image requests. When empty, `model` is used |
| `credential` | none | Reference to the stored key |
| `key_env` | `OPENAI_API_KEY` | Environment variable read when no credential is stored |
| `namespace` | `default` | Separates caches and rate limits of different uses |
| `prompt_cache` | `implicit` | Provider prompt caching: `off`, `implicit`, `explicit` (Responses only) or `ephemeral` (Anthropic only) |
| `cache_ttl_hours` | 168 | Lifetime of locally cached replies. 0 disables reading the cache |
| `timeout_seconds` | 180 | Request timeout, 1 to 3600 |
| `attempts` | 4 | Attempts per request, 1 to 8 |
| `concurrency` | 3 | Requests in flight, 1 to 16 |
| `requests_per_minute` | 60 | Request rate limit, at least 1 |
| `tokens_per_minute` | 120000 | Token rate limit, at least 1024 |
| `context_tokens` | 32768 | Context budget. At least `max_output_tokens` + `chunk_bytes` + the length of `style` + 4096 |
| `max_output_tokens` | 8192 | Output limit per request, at least 256 |
| `input_price_per_million`, `cached_input_price_per_million`, `output_price_per_million` | none | Prices for cost accounting |
| `vision_input_price_per_million`, `vision_cached_input_price_per_million`, `vision_output_price_per_million` | none | Prices of a separate vision model |

### 7.2 `[processing]`

| Key | Default | Meaning |
| --- | --- | --- |
| `pipeline` | `editorial` | `editorial` or `archival`. See section 9 |
| `edit` | true | Copyedit with the model |
| `classify` | true | Title, synopsis, tags and class from the model |
| `vision` | true | Send page images to the model |
| `prepare_only` | false | Page images and OCR only |
| `ocr` | `paddle` | `paddle` or `tesseract` (local OCR), `vision` (no local OCR; in the archival pipeline the model reads the page image, in the editorial pipeline the same as `off`), or `off` |
| `ocr_language` | `en` | Read only when `ocr` is `tesseract`: the Tesseract language, or several joined by `+`, for example `eng+deu`. It must be one for which Tesseract has data on the computer; this is checked when Tesseract is chosen in Setup and when it is used. The values `en`, `zh` and `ja` of earlier files are taken as `eng`, `chi_sim` and `jpn`. With any other OCR engine the key has no effect and every value is accepted. The bundled engine recognizes English, Chinese and Japanese without a setting |
| `page_concurrency` | 3 | Pages read at once, 1 to 16 |
| `chunk_bytes` | 6000 | Size of a source window, 512 to 32000 |
| `evidence_rounds` | 3 | Evidence requests allowed per passage, 0 to 8 |
| `house_style` | empty | Editorial preferences added to the fixed role prompts |
| `style` | built-in | Prompt of the archival pipeline. In the editorial pipeline only a changed value is used, as house style |
| `glossary` | empty | Terms passed to the prompts |
| `classification_override` | none | A class code that replaces the model's. Editorial pipeline only |
| `office_renderer` | none | Path of the LibreOffice `soffice` executable |
| `render_width` | 2000 | Page image width in pixels, 512 to 4096 |
| `remove_page_furniture` | true | Remove repeated headers and footers from the text of a PDF of three or more pages |
| `preserve_original_on_review` | true | Archival pipeline only: keep the source text when an edit raises a finding |
| `max_file_bytes` | 2147483648 | Largest accepted source |
| `max_requests` | 10000 | Requests allowed per job |
| `max_generated_tokens` | 10000000 | Output tokens allowed per job |
| `max_cost` | none | Cost allowed per job. Requires input and output prices, and vision prices when a separate vision model is set |

### 7.3 `[output]`

| Key | Default | Meaning |
| --- | --- | --- |
| `frontmatter` | true | Write a front matter block |
| `title` | true | Write the title as a first-level heading |
| `summary` | true | Write a "Synopsis" paragraph |
| `page_markers` | false | Write an HTML comment at source page boundaries |
| `review_notes` | true | Write a "Review notes" list |
| `filename` | `{class} - {title} - {number}.md` | File name pattern |
| `fields` | identity for eight fields | Front matter key for each of `id`, `title`, `summary`, `tags`, `class_code`, `taxonomy`, `source`, `status`. `sequence` may be added. A field left out is not written. Keys must be unique and consist of ASCII letters, digits, `_` or `-` |

File name placeholders:

| Placeholder | Value |
| --- | --- |
| `{title}` | Title |
| `{class}` | Class code. `unclassified` when there is none, which occurs only in the archival pipeline; the editorial pipeline assigns `000` |
| `{number}` | Job number, six digits |
| `{sequence}` | Accession number within the class, six digits. The job number when there is none |
| `{id}` | First 12 characters of the document identifier |

Any other placeholder is rejected. Characters not allowed in file names are replaced by `_`. Leading and trailing spaces and dots are removed. A name is cut to 180 bytes. An empty name becomes `untitled`. A name reserved by Windows is given a leading `_`. `.md` is added when absent. If a file of that name exists, ` (2)`, ` (3)` and so on is added.

### 7.4 `[taxonomy]`

A table of class code to class name, given to the classification prompt. The default is ten classes, `000` to `900`, in the manner of the Dewey Decimal Classification.

## 8. Providers

| Preset | Endpoint | Protocol |
| --- | --- | --- |
| Anthropic | `https://api.anthropic.com/v1` | anthropic |
| Alibaba Qwen · International | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | chat |
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai` | chat |
| OpenAI | `https://api.openai.com/v1` | responses |
| xAI / Grok | `https://api.x.ai/v1` | chat |
| Z.ai / GLM | `https://api.z.ai/api/paas/v4` | chat |
| Moonshot / Kimi | `https://api.moonshot.ai/v1` | chat |
| DeepSeek | `https://api.deepseek.com` | chat |
| MiniMax | `https://api.minimax.io/v1` | chat |
| Mistral | `https://api.mistral.ai/v1` | chat |
| OpenRouter · model gateway | `https://openrouter.ai/api/v1` | chat |
| Together AI | `https://api.together.xyz/v1` | chat |
| Fireworks AI | `https://api.fireworks.ai/inference/v1` | chat |
| Groq | `https://api.groq.com/openai/v1` | chat |
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | chat |
| Hugging Face · inference gateway | `https://router.huggingface.co/v1` | chat |
| Nebius Token Factory | `https://api.tokenfactory.nebius.com/v1` | chat |
| Novita AI | `https://api.novita.ai/openai` | chat |
| SiliconFlow · International | `https://api.siliconflow.com/v1` | chat |
| SambaNova | `https://api.sambanova.ai/v1` | chat |
| Ollama · local | `http://localhost:11434/v1` | chat |
| LM Studio · local | `http://localhost:1234/v1` | chat |
| Custom compatible endpoint | entered by the user | chosen by the user |

Structured replies are requested in `json` mode for Qwen, Z.ai, Moonshot, DeepSeek, MiniMax, Groq, NVIDIA NIM, Novita AI, SiliconFlow and SambaNova, and in `schema` mode for the others. Every reply is also checked locally against the expected schema.

The presets record each provider's published endpoint. The developers have not exercised them with live keys. The connection check of section 4.4 verifies the chosen provider, key and model.

**Requests.** A request that receives HTTP 429 or a 5xx status, or that fails in transport, is tried at most `attempts` times in all, with increasing delay or the delay the provider states. Each try counts as one request against `max_requests`. A stated delay longer than `timeout_seconds` fails the request. Any other HTTP error fails at once. Redirects are not followed. The connection time limit is 20 seconds. A reply may be at most 16 MiB. A request estimated to exceed `context_tokens`, or the whole of `tokens_per_minute`, is not sent; for an Anthropic endpoint the provider's token-count service is consulted before such a request is refused. Requests are spaced to `requests_per_minute` and held to `tokens_per_minute`. A reply cut off at the output limit causes the source to be divided and the work continued from the source, not from generated text.

**Budgets.** Before each request a reservation is recorded against `max_requests`, `max_generated_tokens` and `max_cost`. A request that would exceed a ceiling is not sent, and the job stops with status `budget_exhausted` and a message that names the ceiling. Reservations survive a restart. A reservation for a request whose outcome was never reported stays held and counts against the ceiling. Cost is computed only when prices are configured and the provider reports token counts. A stopped job continues when its ceiling is raised (sections 5.3 and 6.2); nothing is reset.

**Caches.** A completed reply is stored in the workspace database under a hash of the request. An identical later request is answered from the store without a provider call. This is separate from the provider's own prompt caching, which is selected by `prompt_cache`.

**Images.** A page image of at most 3 MiB and at most 7,680 pixels on its longer side is sent unchanged. A larger image is converted to JPEG and reduced until it is within 3 MiB. The full page image remains in the workspace.

## 9. Processing

### 9.1 Input formats

| Extensions | Handling |
| --- | --- |
| `pdf` | PDFium. Encrypted files are refused |
| `docx`, `pptx`, `ppt`, `doc`, `odt`, `odp`, `rtf`, `xlsx`, `ods` | When `processing.pipeline` is `editorial`: converted to PDF by LibreOffice, then handled as PDF |
| `txt`, `text`, `log` | UTF-8 text |
| `md`, `markdown` | Markdown, at most 64 MiB |
| `csv`, `tsv` | Tables, in batches of records |
| `srt`, `vtt` | One unit per subtitle cue |
| `html`, `htm`, `xhtml` | Headings, paragraphs, lists, tables, images, figures and mathematics, at most 32 MiB |
| `png`, `jpg`, `jpeg`, `bmp`, `webp` | One page image |
| `tif`, `tiff`, `gif` | One page image per frame |

Any other extension is refused when the job runs, and the original is kept. Text must be UTF-8. An office conversion is stopped after ten minutes. The handling of office formats follows the setting `processing.pipeline`, including for extraction-only jobs. When it is `archival`, `docx` is read directly from its XML and the other office formats are refused.

Further limits: an image may be at most 16,000 pixels on a side; a rendered page image is at most twice `render_width` in height; a CSV or TSV record may have at most 1,000 columns and 8 MiB; a subtitle line may be at most 1 MiB and a cue 8 MiB; a part of a DOCX file may be at most 64 MiB; Tesseract is allowed 120 seconds a page.

### 9.2 Engines

| Engine | Version | Use |
| --- | --- | --- |
| PDFium | Chromium build 8066 | PDF text, geometry and page rendering |
| ONNX Runtime | 1.22.0, CPU | Runs the OCR models |
| PP-OCRv5 mobile detector and recognizer | PaddleOCR | Local OCR |
| LibreOffice | 26.8.2 | Office documents to PDF. In packages only |
| Tesseract | external | Optional OCR. Not supplied |

Engines are pinned by URL and SHA-256. An engine is looked for in the workspace `engines` folder and then in the `engines` folder beside the program. Each use verifies the engine file against its recorded checksum. Engines exist for the four package targets only. An engine archive may be at most 512 MiB and its download is allowed 600 seconds.

LibreOffice is looked for in this order: `processing.office_renderer`, the workspace, the folder beside the program, the usual installation paths, and the search path. If `processing.office_renderer` is set and does not name a file, no other place is tried. It runs without a display, with a private profile and with document macros disabled.

### 9.3 The editorial pipeline

This is the default. It is used when `pipeline` is `editorial` and at least one of `edit`, `vision` or `prepare_only` is set.

1. When the job first runs, the source is copied to `originals/`. A file changed between queueing and that moment is read as changed.
2. Pages are prepared. A PDF page is rendered to an image. An office document is first converted to PDF. Text that has no page image is printed to page images in the bundled Noto Sans font, and every byte of the text is checked to belong to exactly one page.
3. Local OCR is run on each page image. A failed OCR is recorded and does not stop the job.
4. If `classify` is set, a first title, synopsis and class are requested from the first page.
5. Three roles then work at the same time.
   - The **Copyeditor** edits the native text of each page. It is skipped when `edit` is off or the page has no native text.
   - The **visual reader** reads each page image together with its OCR. With `vision` off, the native text or the OCR text stands in for the reading.
   - The **Typesetter** merges the copyedit and the reading of each page, in page order, into one Markdown text, and carries an unfinished paragraph to the next page.
6. The **Reviewer** reads the assembled passages in order. It may revise an earlier passage; such a revision must quote the text to be replaced, give its current hash, cite evidence and state a reason. No other role may revise an earlier passage.

   Each of the three roles may ask for source text or an earlier passage, and the Typesetter and the Reviewer for one page image, up to `evidence_rounds` times for each unit of work.
7. Final title, synopsis, tags and class are requested. `classification_override` is applied. An accession number is assigned within the class.
8. The Markdown is published and indexed.

The Copyeditor and Reviewer prompts are fixed texts, `copyeditor-v2.txt` and `reviewer-v2.txt`. They are in the `prompts` folder of a package and in `crates/librarian-core/prompts/` of the repository. Their versions and hashes are recorded with each run. A run cannot be resumed by a build whose role prompts or protocol differ from those recorded for the run; it must be retried as a new job. A change of house style applies to new jobs only. A job begun under the earlier `editorial-v1` runner resumes with that runner's roles.

Code, mathematics, tables, HTML, link destinations, reference definitions, front matter and recognizable literals (addresses, paths, variables, option names, identifiers, version numbers) are replaced by markers before a text is sent for editing and are restored afterwards. A reply that loses or duplicates a marker is rejected.

The model must list every assigned unit of source text exactly once, as represented, merged or unresolved, with a reason, before a passage is accepted. This accounting detects a unit that was skipped or processed twice. It does not prove that the meaning was preserved. That requires a human reader.

A job of this pipeline is published with status `complete`. Unresolved findings of the model are written to `editorial-report.json` and are not placed in the published file.

### 9.4 The archival pipeline

This pipeline is kept so that older jobs can resume. It is also the path taken when `edit`, `vision` and `prepare_only` are all off, as with `clean --extract-only`.

It extracts blocks, interprets page images by OCR and, where OCR is uncertain, by the model, copyedits blocks in batches, classifies the document against the taxonomy, and publishes. An edit that changes numbers or quotations, changes the length greatly, or alters protected content raises a finding. With `preserve_original_on_review` the source text of that block is kept. A document with any finding is published with status `needs_review`. This pipeline does not apply `classification_override` and does not assign accession numbers.

### 9.5 Job states

| Status | Meaning |
| --- | --- |
| `queued` | Waiting for the worker |
| `running` | Being processed |
| `paused` | Stopped at a checkpoint by request |
| `cancelled` | Stopped by request. May be resumed |
| `failed` | Stopped by an error. The message is kept with the job |
| `budget_exhausted` | Stopped at a request, token or cost ceiling. Continues from its checkpoint once the ceiling is raised |
| `prepared` | Page images and OCR are done. Nothing was published |
| `complete` | Published |
| `needs_review` | Published with findings |

While a job runs, its stage is shown beside its status.

| Stage | Pipeline |
| --- | --- |
| `recovering` | Either. A job returned to the queue after an interruption |
| `snapshotting`, `extracting`, `extracting and rendering PDF`, `laying out office pages` | Either |
| `preparing page images + OCR`, `quick look`, `reading pages + copyediting`, `assembling`, `reviewing with evidence`, `cataloging` | Editorial |
| `interpreting`, `cleaning`, `classifying` | Archival |
| `exporting` | Either |

A job found in state `running` when a worker starts is returned to the queue. Work is recorded in the database page by page and passage by passage. A resumed job does not repeat recorded work.

### 9.6 Published files

A published file has, in this order and each subject to its setting: a front matter block; the title; a synopsis; the body; a list of review notes. Images referred to by the body are copied to an `assets` folder in the destination. The file is written under a temporary name and renamed. An existing file is never replaced.

### 9.7 Search

Finished documents are indexed in the workspace database with SQLite FTS5. The index holds one entry for each published passage. A query is split into words; every word must occur in the same passage or in the title; each word is matched literally. Diacritics are ignored. An empty query returns nothing.

## 10. The WebAssembly package

The crate `librarian-wasm` exports one function, `execute_json`. It gives access to the document model, the editorial prompts and reply schema, literal protection and restoration, revision checks, text planning and file naming. It performs no file access, OCR or provider request. It is built for Node and for the web by the workflow. No program in this repository uses it.

## 11. Limits and known conditions

- **Credential stores.** Storing a key in Keychain, Credential Manager or Secret Service has not been exercised by the automated tests, which use a stand-in store to test the order of operations, the rollback and the recovery. On a computer with no credential store the key can be kept for the session only.
- **Session keys.** A session key is asked for again at each start. Until it is entered, work that calls a model is not queued from the terminal interface.
- **Workers started from the command line.** `clean`, `run` and `retry --start` do not check that the provider is verified. A job whose provider or key is missing fails when it first calls the model, with a message that says so.
- **OCR models.** The bundled OCR models recognize English, Chinese and Japanese. Other languages need Tesseract or the model's own reading of the page.
- **Suggested ceilings.** The limits screen suggests double the present ceiling. The suggestion is arithmetic and not an estimate of what the document needs.
- **Print layout.** Text printed to page images is set in one font without complex-script shaping. The native text is kept separately and is not affected.
- **Scanned PDFs in `parse`.** The parser reports pages that need OCR and does not read them.
- **Quality.** A status of `complete` records that the pipeline ran to its end. It is not a statement that the document is correct. The developers have made no paid document runs; the two document runs on record were made by the owner with an earlier version.
- **Platforms.** The four packages are built and their engines are loaded by the workflow. Operation on other systems, and on every configuration of the four, has not been established.

## 12. Building and checking

```sh
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --locked -p librarian-core -p librarian-native --lib --test persistence
cargo build --release --locked -p librarian-cli --target aarch64-apple-darwin
python3 tools/package.py aarch64-apple-darwin --check-engines
```

`tools/package.py` assembles a package from a built program. It downloads the pinned engines and LibreOffice, verifies their checksums, and copies the licenses. With `--check-engines` it loads PDFium and the OCR models through `doctor --load-engines`, then converts a two-page DOCX and a two-slide PPTX with `prepare` and checks the page order, the text and the OCR.

The package is written to `dist/` as `librarian-TARGET.tar.gz`, or `.zip` on Windows, with a `.sha256` file. The options `--binary` and `--output` name another program and another folder; `--fetch-common` only downloads and verifies the OCR models.

The workflow "Librarian terminal builds" starts on every pull request, on each push that changes the Rust sources, the three packaging tools or the packaged documents, and on request. On a pull request a first job finds which files changed. The build jobs run only when the Rust sources, the packaging tools or the packaged documents changed. A lint of the Python scripts under `tools/` runs only when those scripts or `pyproject.toml` changed. A job that does not apply is reported as skipped. The last job, "Rust required", always runs; it fails if a job that had to run did not succeed. It is the check to require on `main`.

For each of the four targets the build jobs run the linter with warnings as errors, a release build, the tests, and the package check. The format check runs on Linux. The workflow also builds the WebAssembly package.

The legacy Python application is checked by the workflow "CI" in the same manner: its jobs run when its files change, and "Legacy required" reports the result.

There are 18 automated tests. Five concern the PDF parser. The others concern text ownership across chunk boundaries, protection of literal content, budget reservations across concurrent database connections and a reopened database, continuation of a job after its ceiling is raised, the activation transaction and its recovery from interruption, what a connection check covers, the Tesseract language, the image size limit, page coverage of printed text, pause and resume, atomic application of revisions to earlier passages, and recovery of publication after an interruption. They do not establish the quality of any model's editing.
