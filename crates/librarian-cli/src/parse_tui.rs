//! Guided Parse flow: choose a PDF or folder, choose where results go, parse
//! locally, and read what came out. No provider, no model calls.
use crate::{parse_cmd, ui};
use anyhow::Result;
use librarian_core::layout::RenderOptions;
use librarian_native::profile;
use serde::{Deserialize, Serialize};
use std::{
    io::Read,
    path::{Path, PathBuf},
};

#[derive(Clone, Default, Serialize, Deserialize)]
#[serde(default)]
struct Draft {
    source: String,
    destination: String,
    recursive: bool,
    furniture: bool,
    plain: bool,
    words: bool,
    field: usize,
}

const HISTORY: usize = 20;

fn history_path(root: &Path) -> PathBuf {
    root.join("parse-history.json")
}

pub fn history(root: &Path) -> Vec<parse_cmd::Outcome> {
    std::fs::read(history_path(root))
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default()
}

/// One line describing the latest parse, for Home.
pub fn latest(root: &Path) -> Option<String> {
    let last = history(root).into_iter().next()?;
    Some(format!(
        "Last parse: {} file(s), {} pages in {:.1} s · {}",
        last.files.len(),
        last.pages(),
        last.seconds,
        last.destination
            .as_ref()
            .map(|d| d.display().to_string())
            .unwrap_or_default()
    ))
}

fn describe(outcome: &parse_cmd::Outcome) -> String {
    let total = outcome.files.len() + outcome.failures.len();
    let mut text = format!(
        "Parsed {} of {total} file(s) · {} pages · {:.1} s · no model calls\n\n",
        outcome.files.len(),
        outcome.pages(),
        outcome.seconds
    );
    if !outcome.files.is_empty() {
        text.push_str(&format!(
            "Blocks found\n  {}\n\n",
            parse_cmd::counts(&outcome.roles())
        ));
        text.push_str(&format!(
            "Pages by next step\n  {}\n\n",
            parse_cmd::routing_words(&outcome.routing())
        ));
    }
    if let Some(folder) = &outcome.destination {
        text.push_str(&format!("Results folder\n  {}\n", folder.display()));
    }
    if outcome.skipped > 0 {
        text.push_str(&format!(
            "\nStopped before {} file(s) were parsed.\n",
            outcome.skipped
        ));
    }
    if !outcome.failures.is_empty() {
        text.push_str("\nCould not parse\n");
        for (source, error) in &outcome.failures {
            text.push_str(&format!("  {} — {error}\n", source.display()));
        }
    }
    text.push_str("\nSelect a file to read its Markdown.");
    text
}

/// Where the results screen sends the user next.
enum Next {
    /// "Parse something else": the form, focused on the source.
    Again,
    /// "Back to Home".
    Home,
    /// Esc.
    Back,
}

/// Show an outcome; Enter on a file previews its Markdown.
fn results(
    terminal: &mut ratatui::DefaultTerminal,
    title: &str,
    outcome: &parse_cmd::Outcome,
) -> Result<Next> {
    let mut selected = 0;
    loop {
        let mut labels: Vec<String> = outcome
            .files
            .iter()
            .map(|file| {
                format!(
                    "{}  ·  {} page{}  ·  {:.0} ms",
                    file.markdown
                        .as_ref()
                        .unwrap_or(&file.source)
                        .file_name()
                        .unwrap_or_default()
                        .to_string_lossy(),
                    file.pages,
                    if file.pages == 1 { "" } else { "s" },
                    file.total_ms
                )
            })
            .collect();
        let files = labels.len();
        labels.push("Parse something else".into());
        labels.push("Back to Home".into());
        let Some(choice) = ui::choose(terminal, title, &describe(outcome), &labels, selected)?
        else {
            return Ok(Next::Back);
        };
        selected = choice;
        if choice == files {
            return Ok(Next::Again);
        }
        if choice > files {
            return Ok(Next::Home);
        }
        let Some(path) = &outcome.files[choice].markdown else {
            continue;
        };
        let mut bytes = Vec::new();
        std::fs::File::open(path)?
            .take(128 * 1024)
            .read_to_end(&mut bytes)?;
        let mut body = String::from_utf8_lossy(&bytes).into_owned();
        if bytes.len() == 128 * 1024 {
            body.push_str("\n\n[Preview limited to 128 KiB. Open the file for the rest.]");
        }
        ui::notice(terminal, &path.display().to_string(), &body)?;
    }
}

/// Returns a status line for Home when something was parsed.
pub fn run(terminal: &mut ratatui::DefaultTerminal, root: &Path) -> Result<Option<String>> {
    let draft_path = root.join("parse-draft.json");
    let mut draft: Draft = std::fs::read(&draft_path)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default();
    let mut message = None;
    loop {
        profile::write_json(&draft_path, &draft)?;
        let check = |on: bool| if on { "[x]" } else { "[ ]" };
        let past = history(root);
        let fields = vec![
            format!(
                "1  Source        {}",
                if draft.source.is_empty() {
                    "Select a PDF or a folder of PDFs"
                } else {
                    &draft.source
                }
            ),
            format!(
                "2  Destination   {}",
                if draft.destination.is_empty() {
                    "Select or create a folder for the results"
                } else {
                    &draft.destination
                }
            ),
            format!("   {} Include PDFs in subfolders", check(draft.recursive)),
            format!(
                "   {} Keep headers, footers and page numbers",
                check(draft.furniture)
            ),
            format!(
                "   {} Plain text: no bold, italic or link markup",
                check(draft.plain)
            ),
            format!(
                "   {} Include word positions in the JSON",
                check(draft.words)
            ),
            "3  Parse now".into(),
            format!("   Earlier results ({})", past.len()),
        ];
        let Some(index) = ui::path_form(
            terminal,
            "Parse PDFs",
            "Fast and local. Nothing leaves this computer and no AI model is called.\n\
             Each PDF becomes Markdown plus a JSON file of typed blocks: headings,\n\
             paragraphs, lists, tables, pictures, each with its page position.\n\
             Existing files are never replaced. Esc returns Home with this form saved.",
            &fields,
            draft.field,
        )?
        else {
            return Ok(message);
        };
        draft.field = index % fields.len();
        if index >= fields.len() {
            // `e` on a path field: type the path.
            let destination = draft.field == 1;
            let current = if destination {
                &draft.destination
            } else {
                &draft.source
            };
            if let Some(path) = ui::input(
                terminal,
                if destination {
                    "Destination folder"
                } else {
                    "Source PDF or folder"
                },
                "Enter a path. ~ expands to your home folder. Spaces are preserved.",
                current,
                false,
            )? {
                if destination {
                    draft.destination = path.to_string();
                } else {
                    draft.source = path.to_string();
                }
            }
            continue;
        }
        match index {
            0 | 1 => {
                let destination = index == 1;
                let current = if destination {
                    &draft.destination
                } else {
                    &draft.source
                };
                let start = if current.is_empty() {
                    std::env::current_dir()?
                } else {
                    crate::setup::expand_path(current)
                };
                if let Some(chosen) = ui::browse(terminal, &start, destination)? {
                    let chosen = chosen.to_string_lossy().into_owned();
                    if destination {
                        draft.destination = chosen;
                    } else {
                        draft.source = chosen;
                        draft.field = 1;
                    }
                }
            }
            2 => draft.recursive = !draft.recursive,
            3 => draft.furniture = !draft.furniture,
            4 => draft.plain = !draft.plain,
            5 => draft.words = !draft.words,
            6 => {
                if draft.source.is_empty() || draft.destination.is_empty() {
                    ui::notice(
                        terminal,
                        "Two things are still needed",
                        "Choose a source (step 1) and a destination (step 2) first.",
                    )?;
                    draft.field = usize::from(!draft.source.is_empty());
                    continue;
                }
                let source = crate::setup::expand_path(&draft.source);
                let destination = crate::setup::expand_path(&draft.destination);
                let mut options = if draft.plain {
                    RenderOptions::plain()
                } else {
                    RenderOptions::default()
                };
                options.furniture = draft.furniture;
                let (recursive, words, library) = (draft.recursive, draft.words, root.to_owned());
                let record = root.to_owned();
                let outcome = ui::task(
                    terminal,
                    "Parsing",
                    "Reading the PDFs locally. No model is called.",
                    move |updates, cancel| {
                        let request = parse_cmd::Request {
                            root: &library,
                            source: &source,
                            output: Some(&destination),
                            create_output: true,
                            recursive,
                            options,
                            words,
                            pdfium: None,
                            json: false,
                            stop: Some(cancel.as_ref()),
                        };
                        let outcome = parse_cmd::execute(
                            &request,
                            |line| {
                                let _ = updates.send(line.to_owned());
                            },
                            |_| {},
                        )?;
                        // Recorded here, by the worker, so that files already
                        // written stay listed even if this screen is left.
                        let mut past = history(&record);
                        past.insert(0, outcome.clone());
                        past.truncate(HISTORY);
                        profile::write_json(&history_path(&record), &past)?;
                        Ok(outcome)
                    },
                );
                let outcome = match outcome {
                    Ok(Some(outcome)) => outcome,
                    Ok(None) => continue,
                    Err(error) if error.is::<ui::SaveExit>() => return Err(error),
                    Err(error) => {
                        ui::notice(terminal, "Parse needs attention", &format!("{error:#}"))?;
                        continue;
                    }
                };
                message = Some(format!(
                    "Parsed {} file(s), {} pages in {:.1} s. Results in {}",
                    outcome.files.len(),
                    outcome.pages(),
                    outcome.seconds,
                    draft.destination
                ));
                match results(terminal, "Parse results", &outcome)? {
                    Next::Again => draft.field = 0,
                    Next::Home | Next::Back => return Ok(message),
                }
            }
            7 => {
                if past.is_empty() {
                    ui::notice(
                        terminal,
                        "No earlier results",
                        "Nothing has been parsed in this workspace yet.",
                    )?;
                    continue;
                }
                let labels: Vec<String> = past
                    .iter()
                    .map(|o| {
                        format!(
                            "{} file(s) · {} pages · {}",
                            o.files.len(),
                            o.pages(),
                            o.destination
                                .as_ref()
                                .map(|d| d.display().to_string())
                                .unwrap_or_default()
                        )
                    })
                    .collect();
                if let Some(choice) = ui::choose(
                    terminal,
                    "Earlier results",
                    "Most recent first. Enter opens a result.",
                    &labels,
                    0,
                )? {
                    match results(terminal, "Earlier result", &past[choice])? {
                        Next::Again => draft.field = 0,
                        Next::Home => return Ok(message),
                        Next::Back => {}
                    }
                }
            }
            _ => {}
        }
    }
}
