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

Each package contains the program, an `engines` folder, this document as `README.md`, `PARSING_FOUNDATION.md`, `THIRD_PARTY.md`, the license files, the role prompts in `prompts/`, `Cargo.lock`, and `build.json`. The `engines` folder holds PDFium, ONNX Runtime, the two OCR models and a private LibreOffice. The Windows package also holds the Visual C++ runtime libraries.

Unpack the package. Keep the program beside its `engines` folder. Run `./librarian`, or `.\librarian.exe` on Windows. No Python or Node installation is required.

### 2.2 Building from source

The Rust toolchain is pinned to 1.96.0 in `rust-toolchain.toml`.

```sh
cargo build --release --locked -p librarian-cli
./target/release/librarian
```

A source build carries no engines. Setup downloads PDFium, ONNX Runtime and the OCR models into the workspace and verifies their checksums. LibreOffice is not downloaded. To process office documents with a source build, install LibreOffice or set `processing.office_renderer` to the `soffice` executable.

## 3. Workspace and files

### 3.1 Location

The workspace is the folder in which the program keeps its configuration, originals, queue and search index. It is chosen in Setup.

The default workspace is the per-user local application data folder for `librarian-terminal`. On Linux this is `~/.local/share/librarian-terminal`. The command `librarian config path` prints the configuration file in use.

The file `active-profile.json` in the default workspace folder records the workspace and configuration file chosen in Setup. Every later start of the program reads it. The option `--library` selects another workspace for one invocation. The option `--config` selects another configuration file for one invocation. No environment variable selects the workspace.

### 3.2 Contents

| Path in the workspace | Purpose |
| --- | --- |
| `config.toml` | Configuration |
| `setup-complete.json` | Record that Setup verified this configuration |
| `library.sqlite3` | Queue, checkpoints, caches, catalog and search index |
| `worker.lock` | Held by the one worker that may process the queue |
| `worker.log` | Error output of a worker started from the terminal interface |
| `originals/` | One unmodified copy of every imported source, named by content hash |
| `assets/` | Extracted and rendered images, named by content hash |
| `engines/` | Installed PDF and OCR engines, each with `manifest.json` |
| `.librarian-work/job-N/pages/` | Page images and per-page OCR, readings and edits for job N |
| `.librarian-work/job-N/office/` | The PDF made from an office document, and `conversion.log` |
| `.librarian-work/job-N/editorial-report.json` | Diagnostics of the editorial run |
| `prepared/` | Destination used by the `prepare` command |
| `setup-draft.json` | Unfinished Setup answers. Holds no key |
| `import-draft.json` | Unsubmitted "Clean documents with AI" form |
| `parse-draft.json` | Last state of the "Parse PDFs" form |
| `parse-history.json` | The 20 most recent parse results |

A source file is never modified. A published file is never replaced.

## 4. Setup

Setup runs before Home is first shown, when **f** is pressed on Home, and when `librarian setup` is run. The terminal interface does not show Home until Setup has completed for the workspace. This applies to "Parse PDFs now" in the terminal interface as well. The command `librarian parse` does not require Setup.

Setup asks one question at a time. Esc returns to the previous step. An automatic step shows its progress; **x**, Enter or Space continues after it completes. A step that fails offers "Retry this step" and "Back to previous step". There is no way past a failed step.

| Step | Title or question | What it does |
| --- | --- | --- |
| 1 | Welcome | States what Setup will do |
| 2 | Workspace | Use the shown folder, browse for one, or type a path. A folder that already holds a configuration may be loaded |
| 3 | Automatic: storage | Creates the workspace and database and confirms the folder is writable |
| 4 | Provider | Choose one of 23 presets. The endpoint, protocol and structured-output mode are filled in. "Custom compatible endpoint" asks for a URL and a protocol |
| 5 | Credential | Paste a key into a masked field, use the saved credential, import a key from an environment variable, declare that a local server needs no key, or open the provider's key page |
| 6 | Remember key | Store the key in the system credential store, or keep it for this session only |
| 7 | Automatic: model list | Retrieves the provider's live model list with the key |
| 8 | Model | Choose one model. It is used for text and for images |
| 9 | Per-document limit | A dollar ceiling when prices are known, otherwise a ceiling on generated tokens |
| 10 | Processing defaults | Copyediting, classification, visual reading, OCR engine and language, house style, pipeline, page concurrency, classification override, office renderer |
| 11 | Markdown output defaults | Front matter, title, synopsis, page markers, review notices, file name pattern |
| 12 | Automatic: engines | Loads PDFium and the OCR models. Missing engines are downloaded and verified by SHA-256 |
| 13 | Connection check | A separate process loads the saved credential and sends one small built-in image to the model |
| 14 | Activate | Shows a summary. "Save profile and open Home" writes the configuration and makes the workspace active |

Further particulars:

- **Provider list.** Presets are sorted by a published vision score dated 2026-11-06 from `arena.ai/leaderboard/chat/vision`. Gateways and local servers are listed as unranked. The score is a sorting aid and not a statement of quality.
- **Model list.** The list always comes from the provider. If the request fails, Setup reports the failure and does not substitute a stored list. Models that report no image input are omitted. A model that does not report its image capability is labelled "image support unverified" and is tested in step 13. A model with a context below 16,384 tokens or an output limit below 1,024 tokens is refused.
- **Prices.** Prices are shown only where the provider's list supplies them (OpenRouter, xAI) and for one Anthropic model with a recorded rate. A missing price is not treated as zero. Without prices no dollar ceiling can be set.
- **Connection check.** The check sends one request of at most 1,024 generated tokens. It is billed by the provider. It confirms authentication, image input and a structured reply. It does not measure document quality. It cannot be skipped.
- **What is written.** Nothing is activated before step 14. Step 14 writes `config.toml` and `setup-complete.json` in the workspace and `active-profile.json` in the default workspace folder, then deletes `setup-draft.json`.
- **Queued jobs.** Each job keeps the configuration it was queued with. Setup does not alter queued jobs.
- **Repeated runs.** Every completed run of Setup repeats step 12 and step 13, whatever was changed.

### 4.1 Credentials

A key saved in step 6 is stored in the system credential store under the service name `ai.nampara.librarian.terminal` and a new random account name. The key is read back to confirm the save. The stores are Keychain on macOS, Credential Manager on Windows and Secret Service on Linux. Existing entries are never overwritten or deleted.

A session key is held in memory. A worker started from the terminal interface receives it through an environment variable of the worker process. A workspace whose credential is a session key is not ready in a new process, so Setup runs again at the next start.

The configuration file holds a reference to the credential: the account name, the endpoint and whether it is a session key. The key itself is not written to the configuration file, job records, logs or command-line arguments. A saved credential is bound to its endpoint and is refused for any other.

For scripted use without Setup, the key may be supplied in the environment variable named by `provider.key_env`.

## 5. Terminal interface

Start the terminal interface with `librarian` or `librarian tui`. It requires an interactive terminal.

### 5.1 Home

Home has two panes. The left pane, "What do you want to do?", lists six actions. The right pane, "Documents · newest first", lists the jobs of the workspace. Below them are a detail panel for the selected document, a "Now" panel for the running job, a line of key hints, and a message line. The header shows counts of done, to-review, queued and running documents, and the workspace path. The screen refreshes ten times a second.

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

**q** starts one background worker for the workspace. If nothing is queued, no worker is started and the message line says so. If a worker is already running, none is started.

**r** resumes a paused, cancelled or budget-stopped document from its last checkpoint. For a complete, needs-review, prepared or failed document it queues a new job from the retained original, using the current configuration, and keeps the earlier result. In both cases the worker is then started.

### 5.2 Document menu

Enter or Space on a document opens its menu. The entries shown depend on the state of the job.

| Entry | Shown when the job is |
| --- | --- |
| Read the result | Published |
| Read diagnostics and events | Any state |
| Pause (committed work is kept) | Queued or running |
| Resume from the last checkpoint | Paused, cancelled or budget-stopped |
| Run again with current settings (keeps this result) | Complete, needs review, prepared or failed |
| Cancel (the original is kept) | Queued, running or paused |
| Back to Home | Any state |

"Read the result" shows the first 64 KiB of the published file. "Read diagnostics and events" shows the 60 most recent events of the job.

### 5.3 Parse PDFs

Press **a** on Home.

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

While parsing, the screen shows the file in hand. Esc stops the run before the next file; files already written are kept and stay listed under "Earlier results". The results screen shows the number of files and pages, the elapsed time, the count of each block type, the number of pages by next step, the results folder, and any file that could not be parsed with its reason. Enter on a file shows the first 128 KiB of its Markdown. "Parse something else" returns to the form. "Back to Home" returns to Home.

The form has no control for page markers. The command `librarian parse --page-markers` provides them.

### 5.4 Clean documents with AI

Press **n** on Home.

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

Every regular file under the source is queued, whatever its type. Symbolic links are skipped. The destination and the workspace are skipped if they lie inside the source. Source and destination must differ. A file of an unsupported type fails when its job runs, and its original is kept.

### 5.5 Search

Press **s** on Home. Type the words and press Enter. Every word must occur in a document. Up to 30 results are shown, each with the job number, the title and an excerpt. Results are text only and cannot be opened from this screen.

### 5.6 Keys common to all screens

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

Symbolic links are not listed. Folders are listed first.

**Text fields.**

| Key | Action |
| --- | --- |
| Enter | Accept |
| Esc | Discard the edit |
| Left, Right, Home, End | Move the cursor |
| Backspace, Delete | Erase |
| Ctrl+U | Clear the field |

Spaces are ordinary characters. Home shortcuts do not act inside a field. Pasted text is accepted up to a field length of 16,384 bytes; a longer paste is ignored. A key field shows a dot for each character.

**Reading screens** (help, results, diagnostics, messages).

| Key | Action |
| --- | --- |
| Up, Down, Left, Right, Tab, Shift+Tab | Scroll one line |
| PgUp, PgDn | Scroll fifteen lines |
| Home | Return to the top |
| Enter, Space, Esc, x | Close |

**Automatic steps.** After completion, **x**, Enter or Space continues. Esc cancels the step and goes back. If Esc is pressed after the step has completed, its result is discarded.

**Ctrl+S** acts on every screen. Accepted answers and saved forms are kept. An edit not yet accepted, and a key not yet saved, are lost. All queued and running jobs of the workspace are set to paused. The program exits with status 0. A running worker stops at its next checkpoint.

## 6. Commands

```
librarian [--library FOLDER] [--config FILE] [--json] [COMMAND]
```

With no command the terminal interface is started. `librarian COMMAND --help` prints the options of a command.

| Global option | Meaning |
| --- | --- |
| `--library FOLDER` | Use this workspace instead of the active one |
| `--config FILE` | Use this configuration file instead of `config.toml` in the workspace |
| `--json` | Print machine-readable JSON where the command supports it |

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
| `--page-markers` | Mark each page boundary with an HTML comment |
| `--plain` | Omit bold, italic, code and link markup and picture markers |
| `--words` | Add positioned words, fonts and bookmarks to the JSON |

An existing result file is not replaced; that file is reported as a failure. One unreadable file does not stop a folder. With `--json`, one JSON line is printed per file. The exit status is 1 if any file failed. See [PARSING_FOUNDATION.md](PARSING_FOUNDATION.md).

### 6.2 Cleaning and the queue

| Command | Action |
| --- | --- |
| `clean [SOURCE] [-o FOLDER]` | Queue the source and process the queue. On a terminal, a missing source or destination is chosen in the file browser |
| `run` | Process the queue. Work interrupted earlier is resumed |
| `status` | Print the jobs as JSON |
| `status --watch` | Open the terminal interface |
| `pause [ID]` | Pause one job, or every queued and running job |
| `resume [ID]` | Return one job, or every paused, cancelled, failed and budget-stopped job, to the queue |
| `retry ID` | Queue a new job from the retained original with the current configuration |
| `cancel [ID]` | Cancel one job, or every queued, running and paused job |
| `prepare SOURCE` | Render page images and run local OCR only. No model call, no publication. The job ends as `prepared` |

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
| `--start` | `resume`, `retry` | Start the worker afterwards and wait for it |

Only one worker may process a workspace at a time. Jobs run one at a time in order of job number. A worker on a terminal shows a progress line. On an interrupt signal the worker pauses all queued and running jobs and stops; committed work is kept.

### 6.3 Inspection

| Command | Output |
| --- | --- |
| `show ID` | The job, its metadata, its page counts and its last 100 events, as JSON |
| `pages ID` | One line per page: image path, size of native text, and whether OCR, visual reading and copyedit are done |
| `pages ID --page N` | The contents of page N: image path, native text, OCR, visual reading, copyedit |
| `print ID` | The published Markdown |
| `search QUERY [-l N]` | Documents containing every word of the query. The default limit is 20 |
| `catalog` | Complete and needs-review jobs with metadata, class and accession number, as JSON |
| `stats` | Documents, requests and tokens by status, and documents by class, as JSON |
| `editorial ID` | The request packets, replies, revisions, interventions and expert feedback of an editorial run, as JSON |
| `prompts` | The role names, versions and SHA-256 hashes |
| `prompts ROLE` | The text of `copyeditor`, `reviewer` or `protocol` |

### 6.4 Publication and feedback

| Command | Action |
| --- | --- |
| `export ID -o FOLDER [--create-output]` | Publish a complete or needs-review job again, to another folder, with the current output settings. No model call |
| `feedback ID --segment review:N --replacement FILE --reason TEXT` | Record an expert correction in the workspace database |

`feedback` stores the source evidence, the model's text, the corrected text and the reason. It does not change the published file, any prompt or any model.

### 6.5 Configuration and engines

| Command | Action |
| --- | --- |
| `setup` | Run Setup |
| `config show` | Print the configuration as TOML |
| `config path` | Print the path of the configuration file |
| `config schema` | Print the JSON Schema of the configuration |
| `config set KEY VALUE` | Set one value. The key has the form `section.field` |
| `config edit` | Open the file in the editor named by `VISUAL` or `EDITOR`, then validate it. The default editor is `vi`, or `notepad` on Windows |
| `config reset` | Copy the file to a dated backup, then write the defaults |
| `engines install` | Download, verify and install the engines into the workspace |
| `engines list` | Print the pinned engine assets for this platform |
| `doctor` | Print the workspace, configuration path, platform, provider protocol, whether a model and a credential are configured, the pinned engines and the office renderer found. No provider request is made |
| `doctor --load-engines` | In addition, load PDFium and the OCR models |

### 6.6 Direct conversion

| Command | Action |
| --- | --- |
| `convert SOURCE [-o FILE]` | Convert a `.txt`, `.md` or `.markdown` file of at most 16 MiB to normalized Markdown, without the queue. The output file must not exist |
| `inspect SOURCE` | Print Markdown syntax counts for such a file |

### 6.7 Exit status

| Status | Meaning |
| --- | --- |
| 0 | Success. Also an exit by Ctrl+S |
| 1 | An error, or a job that failed or stopped at its budget |
| 2 | A job that needs review |
| 3 | A job that was paused or cancelled |

Statuses 1 to 3 are returned by `clean`, `run`, `prepare`, `resume --start` and `retry --start` for the jobs they processed.

## 7. Configuration

The configuration file is TOML. Unknown keys are rejected. A missing file yields the defaults. `version` must be 1.

### 7.1 `[provider]`

| Key | Default | Meaning |
| --- | --- | --- |
| `catalog_id` | `openai` | The preset chosen in Setup |
| `kind` | `responses` | Protocol: `responses`, `chat` or `anthropic` |
| `base_url` | `https://api.openai.com/v1` | Endpoint. HTTPS is required except for a local host |
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
| `requests_per_minute` | 60 | Request rate limit |
| `tokens_per_minute` | 120000 | Token rate limit |
| `context_tokens` | 32768 | Context budget |
| `max_output_tokens` | 8192 | Output limit per request |
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
| `ocr` | `paddle` | Local OCR: `paddle`, `tesseract`, `vision` or `off` |
| `ocr_language` | `en` | Language passed to Tesseract. With `paddle` the value must be `en`, `zh`, `ja` or `auto` and has no further effect |
| `page_concurrency` | 3 | Pages read at once, 1 to 16 |
| `chunk_bytes` | 6000 | Size of a source window, 512 to 32000 |
| `evidence_rounds` | 3 | Evidence requests allowed per passage, 0 to 8 |
| `house_style` | empty | Editorial preferences added to the fixed role prompts |
| `style` | built-in | Prompt of the archival pipeline. In the editorial pipeline only a changed value is used, as house style |
| `glossary` | empty | Terms passed to the prompts |
| `classification_override` | none | A class code that replaces the model's. Editorial pipeline only |
| `office_renderer` | none | Path of the LibreOffice `soffice` executable |
| `render_width` | 2000 | Page image width in pixels, 512 to 4096 |
| `remove_page_furniture` | true | Remove repeated headers and footers from PDF text |
| `preserve_original_on_review` | true | Archival pipeline only: keep the source text when an edit raises a finding |
| `max_file_bytes` | 2147483648 | Largest accepted source |
| `max_requests` | 10000 | Requests allowed per job |
| `max_generated_tokens` | 10000000 | Output tokens allowed per job |
| `max_cost` | none | Cost allowed per job. Requires prices |

### 7.3 `[output]`

| Key | Default | Meaning |
| --- | --- | --- |
| `frontmatter` | true | Write a front matter block |
| `title` | true | Write the title as a first-level heading |
| `summary` | true | Write a "Synopsis" paragraph |
| `page_markers` | false | Write an HTML comment at source page boundaries |
| `review_notes` | true | Write a "Review notes" list |
| `filename` | `{class} - {title} - {number}.md` | File name pattern |
| `fields` | identity | Front matter key for each of `id`, `title`, `summary`, `tags`, `class_code`, `taxonomy`, `source`, `status`, `sequence` |

File name placeholders:

| Placeholder | Value |
| --- | --- |
| `{title}` | Title |
| `{class}` | Class code, or `unclassified` |
| `{number}` | Job number, six digits |
| `{sequence}` | Accession number within the class, six digits. The job number when there is none |
| `{id}` | First 12 characters of the document identifier |

Characters not allowed in file names are replaced by `_`. A name is cut to 180 bytes. If a file of that name exists, ` (2)`, ` (3)` and so on is added.

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
| OpenRouter | `https://openrouter.ai/api/v1` | chat |
| Together AI | `https://api.together.xyz/v1` | chat |
| Fireworks AI | `https://api.fireworks.ai/inference/v1` | chat |
| Groq | `https://api.groq.com/openai/v1` | chat |
| NVIDIA NIM | `https://integrate.api.nvidia.com/v1` | chat |
| Hugging Face | `https://router.huggingface.co/v1` | chat |
| Nebius Token Factory | `https://api.tokenfactory.nebius.com/v1` | chat |
| Novita AI | `https://api.novita.ai/openai` | chat |
| SiliconFlow · International | `https://api.siliconflow.com/v1` | chat |
| SambaNova | `https://api.sambanova.ai/v1` | chat |
| Ollama · local | `http://localhost:11434/v1` | chat |
| LM Studio · local | `http://localhost:1234/v1` | chat |
| Custom compatible endpoint | entered by the user | chosen by the user |

The presets record each provider's published endpoint. The developers have not exercised them with live keys. Setup verifies the chosen provider, key and model in step 13.

**Requests.** A request that receives HTTP 429 or a 5xx status, or that fails in transport, is retried up to `attempts` times with increasing delay. Any other HTTP error fails at once. Requests are spaced to `requests_per_minute` and held to `tokens_per_minute`. A reply cut off at the output limit causes the source to be divided and the work continued from the source, not from generated text.

**Budgets.** Before each request a reservation is recorded against `max_requests`, `max_generated_tokens` and `max_cost`. A request that would exceed a ceiling is not sent, and the job stops with status `budget_exhausted`. Reservations survive a restart. Cost is computed only when prices are configured and the provider reports token counts.

**Caches.** A completed reply is stored in the workspace database under a hash of the request. An identical later request is answered from the store without a provider call. This is separate from the provider's own prompt caching, which is selected by `prompt_cache`.

**Images.** A page image of at most 3 MiB and at most 7,680 pixels on its longer side is sent unchanged. A larger image is converted to JPEG and reduced until it is within 3 MiB. The full page image remains in the workspace.

## 9. Processing

### 9.1 Input formats

| Extensions | Handling |
| --- | --- |
| `pdf` | PDFium. Encrypted files are refused |
| `docx`, `pptx`, `ppt`, `doc`, `odt`, `odp`, `rtf`, `xlsx`, `ods` | Converted to PDF by LibreOffice, then handled as PDF. Editorial pipeline only, except `docx` |
| `txt`, `text`, `log` | UTF-8 text |
| `md`, `markdown` | Markdown, at most 64 MiB |
| `csv`, `tsv` | Tables, in batches of records |
| `srt`, `vtt` | One unit per subtitle cue |
| `html`, `htm`, `xhtml` | Headings, paragraphs, lists, tables, images, figures and mathematics, at most 32 MiB |
| `png`, `jpg`, `jpeg`, `bmp`, `webp` | One page image |
| `tif`, `tiff`, `gif` | One page image per frame |

Any other extension is refused when the job runs, and the original is kept. Text must be UTF-8. An office conversion is stopped after ten minutes. In the archival pipeline `docx` is read directly from its XML, and the other office formats are refused.

### 9.2 Engines

| Engine | Version | Use |
| --- | --- | --- |
| PDFium | Chromium build 8066 | PDF text, geometry and page rendering |
| ONNX Runtime | 1.22.0, CPU | Runs the OCR models |
| PP-OCRv5 mobile detector and recognizer | PaddleOCR | Local OCR |
| LibreOffice | 26.8.2 | Office documents to PDF. In packages only |
| Tesseract | external | Optional OCR. Not supplied |

Engines are pinned by URL and SHA-256. An engine is looked for in the workspace `engines` folder and then in the `engines` folder beside the program. Each use verifies the engine file against its recorded checksum. Engines exist for the four package targets only.

LibreOffice is looked for in this order: `processing.office_renderer`, the workspace, the folder beside the program, the usual installation paths, and the search path. It runs without a display, with a private profile and with document macros disabled.

### 9.3 The editorial pipeline

This is the default. It is used when `pipeline` is `editorial` and at least one of `edit`, `vision` or `prepare_only` is set.

1. The source is copied to `originals/`.
2. Pages are prepared. A PDF page is rendered to an image. An office document is first converted to PDF. Text that has no page image is printed to page images in the bundled Noto Sans font, and every byte of the text is checked to belong to exactly one page.
3. Local OCR is run on each page image. A failed OCR is recorded and does not stop the job.
4. If `classify` is set, a first title, synopsis and class are requested from the first page.
5. Three roles then work at the same time.
   - The **Copyeditor** edits the native text of each page. It is skipped when `edit` is off or the page has no native text.
   - The **visual reader** reads each page image together with its OCR. With `vision` off, the native text or the OCR text stands in for the reading.
   - The **Typesetter** merges the copyedit and the reading of each page, in page order, into one Markdown text, and carries an unfinished paragraph to the next page.
6. The **Reviewer** reads the assembled passages in order. It may ask for source text, an earlier passage or one page image, up to `evidence_rounds` times per passage. It may revise an earlier passage; such a revision must quote the text to be replaced and its current hash.
7. Final title, synopsis, tags and class are requested. `classification_override` is applied. An accession number is assigned within the class.
8. The Markdown is published and indexed.

The Copyeditor and Reviewer prompts are fixed texts, `copyeditor-v2.txt` and `reviewer-v2.txt`. They are in the `prompts` folder of a package and in `crates/librarian-core/prompts/` of the repository. Their versions and hashes are recorded with each run. A run cannot be resumed after the prompts or the house style have changed; it must be retried as a new job.

Code, mathematics, tables, links, front matter and recognizable identifiers are replaced by markers before a text is sent for editing and are restored afterwards. A reply that loses or duplicates a marker is rejected.

Every unit of source text must be reported by the model as represented or merged before a passage is accepted. This accounting detects a unit that was skipped or processed twice. It does not prove that the meaning was preserved. That requires a human reader.

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
| `budget_exhausted` | Stopped at a request, token or cost ceiling |
| `prepared` | Page images and OCR are done. Nothing was published |
| `complete` | Published |
| `needs_review` | Published with findings |

A job found in state `running` when a worker starts is returned to the queue. Work is recorded in the database page by page and passage by passage. A resumed job does not repeat recorded work.

### 9.6 Published files

A published file has, in this order and each subject to its setting: a front matter block; the title; a synopsis; the body; a list of review notes. Images referred to by the body are copied to an `assets` folder in the destination. The file is written under a temporary name and renamed. An existing file is never replaced.

### 9.7 Search

Finished documents are indexed in the workspace database with SQLite FTS5. A query is split into words; every word must occur; each word is matched literally. Diacritics are ignored.

## 10. The WebAssembly package

The crate `librarian-wasm` exports one function, `execute_json`. It gives access to the document model, the editorial prompts and reply schema, literal protection and restoration, revision checks, text planning and file naming. It performs no file access, OCR or provider request. It is built for Node and for the web by the workflow. No program in this repository uses it.

## 11. Limits and known conditions

- **Setup before Home.** The terminal interface requires a completed Setup, including a provider, a key, a model and the billed connection check, before any screen other than Setup is shown. Parsing from the command line has no such requirement.
- **Setup repeats its checks.** Each completed Setup repeats the engine step and the billed connection check.
- **Session keys.** A workspace with a session key runs Setup again at each start.
- **Discarded result.** If Esc is pressed on an automatic step after it has completed, the result is discarded. In step 6 of Setup this leaves a stored key that no configuration refers to.
- **Setup draft.** Leaving Setup at its first step leaves `setup-draft.json`. The next Setup offers to resume it.
- **Custom endpoint.** Choosing "Custom compatible endpoint" again resets the URL to its placeholder and clears the credential and model.
- **Page concurrency in Setup.** A value outside 1 to 16 is ignored without a message.
- **OCR language.** The bundled OCR models recognize English, Chinese and Japanese. `ocr_language` selects nothing for them.
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

The workflow "Librarian terminal builds" runs on each push that changes the Rust sources, the tools or the packaged documents. For each of the four targets it runs the linter with warnings as errors, a release build, the tests, and the package check. The format check runs on Linux. The workflow also builds the WebAssembly package.

There are 14 automated tests. Five concern the PDF parser. The others concern text ownership across chunk boundaries, protection of literal content, budget reservations across processes, the image size limit, page coverage of printed text, pause and resume, and recovery of publication after an interruption. They do not establish the quality of any model's editing.
