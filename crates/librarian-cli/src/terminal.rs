use crate::ui;
use anyhow::{Context, Result};
use crossterm::event::{KeyCode, KeyModifiers};
use librarian_native::{
    config::Config,
    pipeline,
    store::{Job, Library},
};
use librarian_native::{
    credentials,
    profile::{self, Location},
};
use ratatui::{
    Frame,
    layout::{Constraint, Direction, Layout},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Gauge, List, ListItem, ListState, Paragraph, Wrap},
};
use serde::{Deserialize, Serialize};
use std::{
    io::Read,
    path::{Path, PathBuf},
    process::{Child, Stdio},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::Duration,
};

const ACCENT: Color = Color::Cyan;
pub struct Progress {
    thread: thread::JoinHandle<()>,
}
impl Progress {
    pub fn start(root: PathBuf, stop: Arc<AtomicBool>) -> Result<Self> {
        let thread=thread::Builder::new().name("librarian-progress".into()).spawn(move||{
            let Ok(library)=Library::open(&root)else{return;};
            let bar=indicatif::ProgressBar::new_spinner();bar.enable_steady_tick(Duration::from_millis(100));
            if let Ok(style)=indicatif::ProgressStyle::with_template("{spinner:.cyan} {prefix:.bold} {msg}\n  [{bar:42.cyan/blue}] {pos}/{len}  {elapsed_precise}"){bar.set_style(style.progress_chars("#>-"));}
            let mut last=None;
            while !stop.load(Ordering::Relaxed){
                if let Ok(jobs)=library.jobs(1000)&& let Some(job)=jobs.iter().find(|j|j.status=="running"){
                    if last!=Some(job.id){bar.reset_elapsed();last=Some(job.id);}
                    bar.set_prefix(format!("#{}",job.id));bar.set_message(format!("{} · {} · {} calls / {} cached",Path::new(&job.source).file_name().unwrap_or_default().to_string_lossy(),job.stage,job.requests,job.cache_hits));bar.set_length(job.total.max(1) as u64);bar.set_position(job.done.max(0) as u64);
                }thread::sleep(Duration::from_millis(150));
            }bar.finish_and_clear();
        })?;
        Ok(Self { thread })
    }
    pub fn finish(self) {
        let _ = self.thread.join();
    }
}

#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
struct Import {
    source: String,
    destination: String,
    recursive: bool,
    create: bool,
    extract_only: bool,
    body_only: bool,
    start: bool,
    field: usize,
}
impl Default for Import {
    fn default() -> Self {
        Self {
            source: String::new(),
            destination: String::new(),
            recursive: false,
            create: false,
            extract_only: false,
            body_only: false,
            start: true,
            field: 0,
        }
    }
}
#[derive(Clone, Copy, PartialEq)]
enum Pane {
    Actions,
    Documents,
}

struct App {
    pane: Pane,
    action: usize,
    selected: usize,
    message: String,
    child: Option<Child>,
    tick: u64,
}

/// Home actions: shortcut, label, what it does.
const ACTIONS: [(char, &str, &str); 6] = [
    (
        'a',
        "Parse PDFs now",
        "Local and instant, no AI. Markdown plus blocks.",
    ),
    (
        'n',
        "Clean documents with AI",
        "Import, copyedit and publish Markdown.",
    ),
    // The description of q is composed from the queue when Home is drawn.
    ('q', "Start the queue", ""),
    (
        's',
        "Search the library",
        "Full-text search of finished documents.",
    ),
    (
        'f',
        "Setup",
        "Workspace, provider, models, engines, defaults.",
    ),
    ('h', "Help", "Every key, and how the two workflows fit."),
];

pub fn run(root: &Path, config_path: &Path) -> Result<()> {
    let mut location = Location {
        root: root.to_owned(),
        config: config_path.to_owned(),
    };
    ui::with_terminal(|terminal| {
        loop {
            let result = (|| -> Result<Option<Location>> {
                if !location.ready()? {
                    let Some(ready) = crate::onboarding::run(terminal, location.clone())? else {
                        return Ok(None);
                    };
                    location = ready;
                }
                let library = Library::open(&location.root)?;
                dashboard(terminal, &library, &location.config)
            })();
            match result {
                Ok(Some(next)) => location = next,
                Ok(None) => return Ok(()),
                Err(error) if error.is::<ui::SaveExit>() => {
                    let library = Library::open(&location.root)?;
                    library.control(None, "pause")?;
                    return Ok(());
                }
                Err(error) => return Err(error),
            }
        }
    })
}

const HELP: &str = "TWO WAYS TO WORK

a  Parse PDFs now
   Local. No provider, no model call, no charge. Each PDF becomes a
   Markdown file and a JSON file of typed blocks: headings, paragraphs,
   lists, tables, pictures.

n  Clean documents with AI
   Import a file or folder and choose a destination. The queue copyedits
   and publishes Markdown through the provider chosen in Setup.
   q starts the queue. Files run one at a time.

HOME

Left pane: actions. Right pane: documents, newest first.
Up / Down                    Move within the pane
Tab / Shift+Tab              Switch between actions and documents
Left / Right                 Go to actions / go to documents
Enter / Space                Run the action, or open the document's menu
a n q s f h                  Run that action from either pane
p r c                        Pause, resume or run again, cancel the
                             selected document
Esc                          Close the dashboard; a running worker continues
Ctrl+S                       Save drafts, pause all work in this workspace,
                             and exit

DOCUMENT MENU

Enter on a document lists what applies in its current state: read the
result, read diagnostics, pause, resume, run again with current settings
as a new run, or cancel. Originals and earlier results are kept.

LISTS

Up / Down / Left / Right / Tab / Shift+Tab   Move
Enter / Space                                Choose
Any other character                          Filter the list
Backspace                                    Delete one filter character
PgUp / PgDn                                  Scroll the text above the list
Esc                                          Back

FORMS (Parse PDFs, Clean documents)

Up / Down / Left / Right / Tab / Shift+Tab   Move
Enter / Space      Activate a button, toggle or path field
Ctrl+B             Browse from the source or destination field
e                  Type the path of the source or destination field
PgUp / PgDn        Scroll the text above the form
Esc                Back; the form is saved

FILE BROWSER

Up / Down / Left / Right / Tab / Shift+Tab   Move
Enter       Open a folder / select a file
Space       Select the folder you are currently inside
Esc         Return through folder history, then back to the form
Backspace   Parent folder
/           Type a path
n           Create a folder (when selecting a destination or workspace)

TEXT FIELDS

Enter accepts; Esc discards the edit. Left, Right, Home and End move the
cursor. Backspace and Delete erase. Ctrl+U clears the field. Spaces are
ordinary text and Home shortcuts do not act inside a field. Paste is
accepted. Ctrl+S keeps answers already accepted; an edit not yet accepted,
or an API key not yet saved, is discarded.

READING SCREENS (help, results, diagnostics, messages)

Up / Down / Left / Right / Tab / Shift+Tab   Scroll one line
PgUp / PgDn                                  Scroll fifteen lines
Home                                         Return to the top
Enter / Space / Esc / x                      Close

AUTOMATIC STEPS

A step shows its progress. When it completes, x, Enter or Space continues.
Esc cancels the step and goes back. A step that fails cannot be passed.

COPY / PASTE

Use the terminal's own copy and paste commands (often Ctrl+Shift+C/V on
Windows and Linux; Cmd+C/V on macOS). Librarian does not bind Ctrl+C here.

RUNTIME

Setup establishes the active workspace, provider, credential, model,
engines and defaults. Workers load it automatically. Queued jobs keep the
configuration they were queued with. API keys are never written into the
configuration file.

COMMAND LINE

parse FILE|FOLDER -o FOLDER · clean SOURCE -o FOLDER · status
show ID · pages ID · print ID · export ID --output FOLDER · doctor
Run librarian --help for every command. The README beside the program is
the complete reference.";

fn pause(app: &mut App, library: &Library, id: i64) -> Result<()> {
    app.message = if library.control(Some(id), "pause")? > 0 {
        format!("Pause requested for #{id}; committed work is kept")
    } else {
        format!("#{id} is neither queued nor running; nothing to pause")
    };
    Ok(())
}

fn cancel(app: &mut App, library: &Library, id: i64) -> Result<()> {
    app.message = if library.control(Some(id), "cancel")? > 0 {
        format!("Cancel requested for #{id}; the original is kept")
    } else {
        format!("#{id} is not queued, running or paused; nothing to cancel")
    };
    Ok(())
}

/// "running · assembling", or just "queued" when the stage says the same.
fn state(job: &Job) -> String {
    let status = job.status.replace('_', " ");
    if job.stage.is_empty() || job.stage == job.status {
        status
    } else {
        format!("{status}  ·  {}", job.stage)
    }
}

fn finished(status: &str) -> bool {
    matches!(status, "complete" | "needs_review" | "prepared" | "failed")
}

/// Resume unfinished work, or run finished work again as a new job.
fn resume(app: &mut App, library: &Library, config_path: &Path, id: i64) -> Result<()> {
    let queued = if finished(&library.job(id)?.status) {
        let new_id = pipeline::retry(library, id, &Config::load(config_path)?)?;
        format!("Queued #{new_id} from #{id} with current settings; the earlier result is kept.")
    } else if library.control(Some(id), "resume")? > 0 {
        format!("Resumed #{id}.")
    } else {
        String::new()
    };
    start_worker(app, library, config_path)?;
    if !queued.is_empty() {
        app.message = format!("{queued} {}", app.message);
    }
    Ok(())
}

fn search(terminal: &mut ratatui::DefaultTerminal, library: &Library) -> Result<()> {
    let mut query = String::new();
    while let Some(value) = ui::input(
        terminal,
        "Search",
        "Search the text of finished documents. Enter runs the search; Esc returns Home.",
        &query,
        false,
    )? {
        query = value.to_string();
        let hits = library.search(&query, 30)?;
        let body = hits
            .into_iter()
            .map(|r| {
                format!(
                    "#{}  {}\n{}\n",
                    r["job"],
                    r["title"].as_str().unwrap_or(""),
                    r["snippet"].as_str().unwrap_or("")
                )
            })
            .collect::<Vec<_>>()
            .join("\n");
        ui::notice(
            terminal,
            &format!("Search: {query}"),
            if body.is_empty() { "No matches" } else { &body },
        )?;
    }
    Ok(())
}

/// Everything that can be done with one document in its current state.
fn document_menu(
    terminal: &mut ratatui::DefaultTerminal,
    app: &mut App,
    library: &Library,
    config_path: &Path,
    job: &Job,
) -> Result<()> {
    let name = Path::new(&job.source)
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned();
    let status = job.status.as_str();
    let mut choices: Vec<(&str, String)> = Vec::new();
    if job.output.is_some() {
        choices.push(("open", "Read the result".into()));
    }
    choices.push(("log", "Read diagnostics and events".into()));
    if matches!(status, "running" | "queued") {
        choices.push(("pause", "Pause (committed work is kept)".into()));
    }
    if matches!(status, "paused" | "cancelled" | "budget_exhausted") {
        choices.push(("resume", "Resume from the last checkpoint".into()));
    }
    if finished(status) {
        choices.push((
            "resume",
            "Run again with current settings (keeps this result)".into(),
        ));
    }
    if matches!(status, "running" | "queued" | "paused") {
        choices.push(("cancel", "Cancel (the original is kept)".into()));
    }
    choices.push(("back", "Back to Home".into()));
    let labels: Vec<String> = choices.iter().map(|(_, label)| label.clone()).collect();
    let body = format!(
        "{}Status       {}\nSource       {}\nDestination  {}\nModel calls  {} ({} from cache)",
        job.error
            .as_ref()
            .map(|e| format!("Error        {e}\n"))
            .unwrap_or_default(),
        state(job),
        job.source,
        job.destination,
        job.requests,
        job.cache_hits
    );
    let Some(choice) = ui::choose(
        terminal,
        &format!("Document #{} · {name}", job.id),
        &body,
        &labels,
        0,
    )?
    else {
        return Ok(());
    };
    match choices[choice].0 {
        "open" => {
            if let Some(output) = &job.output {
                ui::notice(terminal, output, &preview(output)?)?;
            }
        }
        "log" => {
            let body = library
                .events(Some(job.id), 60)?
                .into_iter()
                .map(|(_, level, message)| format!("{level}: {message}"))
                .collect::<Vec<_>>()
                .join("\n\n");
            ui::notice(
                terminal,
                &format!("Diagnostics for #{}", job.id),
                if body.is_empty() {
                    "No events recorded yet."
                } else {
                    &body
                },
            )?;
        }
        "pause" => pause(app, library, job.id)?,
        "resume" => resume(app, library, config_path, job.id)?,
        "cancel" => cancel(app, library, job.id)?,
        _ => {}
    }
    Ok(())
}

fn dashboard(
    terminal: &mut ratatui::DefaultTerminal,
    library: &Library,
    config_path: &Path,
) -> Result<Option<Location>> {
    let mut app = App {
        pane: Pane::Actions,
        action: 0,
        selected: 0,
        message: crate::parse_tui::latest(&library.root).unwrap_or_default(),
        child: None,
        tick: 0,
    };
    loop {
        let jobs = library.jobs(1000)?;
        app.selected = app.selected.min(jobs.len().saturating_sub(1));
        if jobs.is_empty() {
            app.pane = Pane::Actions;
        }
        app.tick += 1;
        if let Some(child) = &mut app.child
            && child.try_wait()?.is_some()
        {
            app.child = None;
        }
        terminal.draw(|frame| draw(frame, &jobs, &app, library))?;
        let Some(key) = ui::key()? else {
            continue;
        };
        if key
            .modifiers
            .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
        {
            continue;
        }
        match key.code {
            KeyCode::Tab | KeyCode::BackTab | KeyCode::Left | KeyCode::Right => {
                if !jobs.is_empty() {
                    app.pane = match (app.pane, key.code) {
                        (_, KeyCode::Left) => Pane::Actions,
                        (_, KeyCode::Right) => Pane::Documents,
                        (Pane::Actions, _) => Pane::Documents,
                        (Pane::Documents, _) => Pane::Actions,
                    };
                }
                continue;
            }
            KeyCode::Up | KeyCode::Down => {
                let delta = if key.code == KeyCode::Up { -1 } else { 1 };
                match app.pane {
                    Pane::Actions => app.action = ui::advance(app.action, delta, ACTIONS.len()),
                    Pane::Documents => app.selected = ui::advance(app.selected, delta, jobs.len()),
                }
                continue;
            }
            KeyCode::Esc => return Ok(None),
            _ => {}
        }
        let selected = jobs.get(app.selected).map(|j| j.id);
        // Enter on an action is the same as pressing its letter.
        let command = match key.code {
            code if ui::activate(code) => match app.pane {
                Pane::Actions => Some(ACTIONS[app.action].0),
                Pane::Documents => Some('\n'),
            },
            KeyCode::Char(c) => Some(c),
            _ => None,
        };
        let outcome = (|| -> Result<Option<Location>> {
            match command {
                Some('a') => {
                    if let Some(message) = crate::parse_tui::run(terminal, &library.root)? {
                        app.message = message;
                    }
                }
                Some('n') => import(terminal, &mut app, library, config_path)?,
                Some('q') => start_worker(&mut app, library, config_path)?,
                Some('s') => search(terminal, library)?,
                Some('f') => {
                    return crate::onboarding::run(
                        terminal,
                        Location {
                            root: library.root.clone(),
                            config: config_path.to_owned(),
                        },
                    );
                }
                Some('h') => ui::notice(terminal, "Librarian help", HELP)?,
                Some('p') => {
                    if let Some(id) = selected {
                        pause(&mut app, library, id)?;
                    }
                }
                Some('r') => {
                    if let Some(id) = selected {
                        resume(&mut app, library, config_path, id)?;
                    }
                }
                Some('c') => {
                    if let Some(id) = selected {
                        cancel(&mut app, library, id)?;
                    }
                }
                Some('\n') => {
                    if let Some(job) = jobs.get(app.selected) {
                        document_menu(terminal, &mut app, library, config_path, job)?;
                    }
                }
                _ => {}
            }
            Ok(None)
        })();
        match outcome {
            Ok(Some(next)) => return Ok(Some(next)),
            Ok(None) => {}
            Err(e) if e.is::<ui::SaveExit>() => return Err(e),
            Err(e) => app.message = format!("{e:#}"),
        }
    }
}

fn import(
    t: &mut ratatui::DefaultTerminal,
    app: &mut App,
    library: &Library,
    config_path: &Path,
) -> Result<()> {
    let path = library.root.join("import-draft.json");
    let mut w = std::fs::read(&path)
        .ok()
        .and_then(|bytes| serde_json::from_slice::<Import>(&bytes).ok())
        .unwrap_or_default();
    loop {
        profile::write_json(&path, &w)?;
        let check = |b| if b { "[x]" } else { "[ ]" };
        let fields = vec![
            format!(
                "1  Source        {}",
                if w.source.is_empty() {
                    "Select a file or folder"
                } else {
                    &w.source
                }
            ),
            format!(
                "2  Destination   {}",
                if w.destination.is_empty() {
                    "Select or create an output folder"
                } else {
                    &w.destination
                }
            ),
            format!("   {} Include subfolders", check(w.recursive)),
            format!(
                "   {} Create the destination if it is missing",
                check(w.create)
            ),
            format!(
                "   {} Extraction only (no model calls)",
                check(w.extract_only)
            ),
            format!(
                "   {} Body-only Markdown (no title, summary or front matter)",
                check(w.body_only)
            ),
            format!(
                "   {} Start processing as soon as it is queued",
                check(w.start)
            ),
            "3  Queue import".into(),
        ];
        // This form shares navigation with setup; browse fields also expose an
        // explicit typed-path option, so paths containing spaces work normally.
        let Some(index) = ui::path_form(
            t,
            "Clean documents with AI",
            "Choose what to import and where the finished Markdown goes, then queue it.\n\
             The queue copyedits with the provider chosen in Setup; originals are kept.\n\
             Esc returns Home with this form saved.",
            &fields,
            w.field,
        )?
        else {
            return Ok(());
        };
        w.field = index % fields.len();
        if index >= fields.len() {
            let destination = w.field == 1;
            let value = if destination {
                &w.destination
            } else {
                &w.source
            };
            if let Some(path) = ui::input(
                t,
                if destination {
                    "Destination folder"
                } else {
                    "Source file or folder"
                },
                "Enter a path. ~ expands to your home folder. Spaces are preserved.",
                value,
                false,
            )? {
                if destination {
                    w.destination = path.to_string();
                } else {
                    w.source = path.to_string();
                }
            }
            continue;
        }
        match index {
            0 | 1 => {
                let destination = index == 1;
                let value = if destination {
                    &w.destination
                } else {
                    &w.source
                };
                let start = if value.is_empty() {
                    std::env::current_dir()?
                } else {
                    crate::setup::expand_path(value)
                };
                if let Some(chosen) = ui::browse(t, &start, destination)? {
                    if destination {
                        w.destination = chosen.to_string_lossy().into_owned();
                    } else {
                        w.source = chosen.to_string_lossy().into_owned();
                    }
                }
            }
            2 => w.recursive = !w.recursive,
            3 => w.create = !w.create,
            4 => w.extract_only = !w.extract_only,
            5 => w.body_only = !w.body_only,
            6 => w.start = !w.start,
            7 => {
                let result = (|| -> Result<usize> {
                    anyhow::ensure!(
                        !w.source.is_empty() && !w.destination.is_empty(),
                        "Select both source and destination first"
                    );
                    let mut config = Config::load(config_path)?;
                    if w.extract_only {
                        config.processing.edit = false;
                        config.processing.classify = false;
                        config.processing.vision = false;
                    }
                    if w.body_only {
                        config.output.frontmatter = false;
                        config.output.title = false;
                        config.output.summary = false;
                        config.output.page_markers = false;
                        config.output.review_notes = false;
                    }
                    let ids = pipeline::queue(
                        library,
                        &crate::setup::expand_path(&w.source),
                        &crate::setup::expand_path(&w.destination),
                        &config,
                        w.recursive,
                        w.create,
                    )?;
                    Ok(ids.len())
                })();
                match result {
                    Ok(n) => {
                        // Queue creation succeeded. Do not leave a submit-ready
                        // draft behind if starting the worker subsequently fails.
                        std::fs::remove_file(&path)?;
                        let queued = format!("Queued {n} file(s).");
                        if w.start {
                            start_worker(app, library, config_path)?;
                            app.message = format!("{queued} {}", app.message);
                        } else {
                            app.message = format!("{queued} Press q to start the queue.");
                        }
                        return Ok(());
                    }
                    Err(e) => ui::notice(t, "Import needs attention", &format!("{e:#}"))?,
                }
            }
            _ => {}
        }
    }
}

/// The first 64 KiB of a published result.
fn preview(output: &str) -> Result<String> {
    let mut bytes = Vec::new();
    std::fs::File::open(output)?
        .take(64 * 1024)
        .read_to_end(&mut bytes)?;
    let mut body = String::from_utf8_lossy(&bytes).into_owned();
    if bytes.len() == 64 * 1024 {
        body.push_str(
            "\n\n[Preview limited to 64 KiB. Use librarian print for the full document.]",
        );
    }
    Ok(body)
}
fn start_worker(app: &mut App, library: &Library, config: &Path) -> Result<()> {
    if app
        .child
        .as_mut()
        .is_some_and(|c| c.try_wait().ok().flatten().is_none())
    {
        app.message = "Worker is already running".into();
        return Ok(());
    }
    if library.worker_running()? {
        app.message = "A worker is already processing this library".into();
        return Ok(());
    }
    // A worker with nothing to do would start and stop at once.
    if !library
        .jobs(100_000)?
        .iter()
        .any(|j| matches!(j.status.as_str(), "queued" | "running"))
    {
        app.message = "Nothing is queued. Import documents first.".into();
        return Ok(());
    }
    let log = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(library.root.join("worker.log"))?;
    let mut command = std::process::Command::new(std::env::current_exe()?);
    command
        .arg("--library")
        .arg(&library.root)
        .arg("--config")
        .arg(config)
        .args(["run", "--quiet"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(log);
    // Jobs retain their provider snapshots. Pass each required session key to
    // this child privately, including jobs queued under an earlier profile.
    let providers = {
        let mut statement = library
            .db
            .prepare("SELECT DISTINCT config FROM jobs WHERE status IN ('queued','running')")?;
        statement
            .query_map([], |row| row.get::<_, String>(0))?
            .collect::<std::result::Result<Vec<_>, _>>()?
    };
    let mut missing = 0usize;
    for json in providers {
        let config: Config = serde_json::from_str(&json)?;
        if (config.processing.edit || config.processing.classify || config.processing.vision)
            && credentials::attach_session(&mut command, &config.provider).is_err()
        {
            missing += 1;
        }
    }
    let child = command.spawn().context("Could not start worker")?;
    app.child = Some(child);
    app.message = if missing == 0 {
        "Worker started. Files run one at a time.".into()
    } else {
        format!(
            "Worker started; {missing} saved profile(s) need a session key. Affected jobs will report a credential error; other files can proceed."
        )
    };
    Ok(())
}

fn status_color(status: &str) -> Color {
    match status {
        "running" => ACCENT,
        "complete" => Color::Green,
        "needs_review" | "paused" | "budget_exhausted" => Color::Yellow,
        "failed" => Color::Red,
        _ => Color::DarkGray,
    }
}
fn panel(title: &str) -> Block<'_> {
    Block::default()
        .title(title)
        .borders(Borders::ALL)
        .border_style(Style::default().fg(Color::DarkGray))
}
fn draw(frame: &mut Frame<'_>, jobs: &[Job], app: &App, library: &Library) {
    let area = frame.area();
    let roomy = area.height >= 30;
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(2),
            Constraint::Min(8),
            Constraint::Length(if roomy { 8 } else { 5 }),
            Constraint::Length(3),
            Constraint::Length(2),
        ])
        .split(area);
    let count = |status: &str| jobs.iter().filter(|j| j.status == status).count();
    let (queued, running) = (count("queued"), count("running"));
    let focused = |pane: Pane| {
        Style::default().fg(if app.pane == pane {
            ACCENT
        } else {
            Color::DarkGray
        })
    };

    // Header: product, counts, workspace.
    let summary = if jobs.is_empty() {
        "no documents yet".to_owned()
    } else {
        format!(
            "{} done · {} to review · {queued} queued · {running} running",
            count("complete"),
            count("needs_review")
        )
    };
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(
                "  LIBRARIAN  ",
                Style::default().fg(ACCENT).add_modifier(Modifier::BOLD),
            ),
            Span::raw(summary),
            Span::styled(
                format!("   workspace {}", library.root.display()),
                Style::default().fg(Color::DarkGray),
            ),
        ]))
        .block(Block::default().borders(Borders::BOTTOM)),
        rows[0],
    );

    let columns = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([
            Constraint::Length(58.min(area.width / 2)),
            Constraint::Min(20),
        ])
        .split(rows[1]);

    // Left: what you can do. Every action shows its key and its meaning.
    let describe = columns[0].width >= 54 && rows[1].height >= 14;
    let actions = ACTIONS
        .iter()
        .map(|(key, label, about)| {
            let about = if *key == 'q' {
                match (running, queued) {
                    (0, 0) => "Nothing is queued. Import documents first.".to_owned(),
                    (0, n) => format!("{n} queued and waiting. Starts one at a time."),
                    (_, n) => format!("Running now; {n} more queued."),
                }
            } else {
                (*about).to_owned()
            };
            let mut lines = vec![Line::from(vec![
                Span::styled(
                    format!(" {key}  "),
                    Style::default().fg(ACCENT).add_modifier(Modifier::BOLD),
                ),
                Span::styled(*label, Style::default().add_modifier(Modifier::BOLD)),
            ])];
            // Descriptions need room; a narrow terminal shows the labels only.
            if describe {
                lines.push(Line::from(Span::styled(
                    format!("    {about}"),
                    Style::default().fg(Color::DarkGray),
                )));
            }
            ListItem::new(lines)
        })
        .collect::<Vec<_>>();
    let mut action_state = ListState::default();
    action_state.select(Some(app.action));
    let highlight = |pane: Pane| {
        if app.pane == pane {
            Style::default().bg(Color::Rgb(25, 45, 55))
        } else {
            Style::default()
        }
    };
    frame.render_stateful_widget(
        List::new(actions)
            .block(
                Block::default()
                    .title(" What do you want to do? ")
                    .borders(Borders::ALL)
                    .border_style(focused(Pane::Actions)),
            )
            .highlight_style(highlight(Pane::Actions))
            .highlight_symbol(if app.pane == Pane::Actions {
                "›"
            } else {
                " "
            }),
        columns[0],
        &mut action_state,
    );

    // Right: documents, or how to get the first one.
    let documents = Block::default()
        .title(" Documents · newest first ")
        .borders(Borders::ALL)
        .border_style(focused(Pane::Documents));
    if jobs.is_empty() {
        frame.render_widget(
            Paragraph::new(
                "Nothing here yet.\n\n\
                 1. Press a to parse a PDF. It is local and takes seconds:\n   \
                 you get Markdown and a JSON file of typed blocks.\n\n\
                 2. Press n to clean documents with AI. Imported files\n   \
                 appear here with live progress and results.\n\n\
                 Press h for every key.",
            )
            .wrap(Wrap { trim: false })
            .block(documents),
            columns[1],
        );
    } else {
        let items = jobs
            .iter()
            .map(|job| {
                let name = Path::new(&job.source)
                    .file_name()
                    .unwrap_or_default()
                    .to_string_lossy();
                ListItem::new(vec![
                    Line::from(vec![
                        Span::styled(
                            format!(" #{:<4} ", job.id),
                            Style::default().fg(Color::DarkGray),
                        ),
                        Span::raw(name.into_owned()),
                    ]),
                    Line::from(Span::styled(
                        format!("       {}", state(job)),
                        Style::default().fg(status_color(&job.status)),
                    )),
                ])
            })
            .collect::<Vec<_>>();
        let mut state = ListState::default();
        state.select(Some(app.selected));
        frame.render_stateful_widget(
            List::new(items)
                .block(documents)
                .highlight_style(highlight(Pane::Documents))
                .highlight_symbol(if app.pane == Pane::Documents {
                    "›"
                } else {
                    " "
                }),
            columns[1],
            &mut state,
        );
    }

    // Detail: the selected document, or the workspace when there is none.
    let (title, detail) = match jobs.get(app.selected) {
        Some(job) => {
            let editorial =
                librarian_native::editorial::progress(library, job.id).unwrap_or_default();
            let stages = if editorial["pages"]["rendered"].as_i64().unwrap_or(0) > 0 {
                format!(
                    "Pages {} · OCR {} · vision {} · copyedit {} · assembled {} · polished {}\n",
                    editorial["pages"]["rendered"],
                    editorial["pages"]["ocr"],
                    editorial["pages"]["visual"],
                    editorial["pages"]["copyedited"],
                    editorial["assembled"],
                    editorial["reviewed"]
                )
            } else {
                String::new()
            };
            (
                format!(
                    " #{} · {} ",
                    job.id,
                    Path::new(&job.source)
                        .file_name()
                        .unwrap_or_default()
                        .to_string_lossy()
                ),
                format!(
                    "{}{}\n{}From  {}\nTo    {}\nModel calls {} ({} cached) · tokens {} in / {} out · cost {}",
                    job.error
                        .as_ref()
                        .map(|e| format!("Error: {e}\n"))
                        .unwrap_or_default(),
                    state(job),
                    stages,
                    job.source,
                    job.output.as_deref().unwrap_or(&job.destination),
                    job.requests,
                    job.cache_hits,
                    job.input_tokens,
                    job.output_tokens,
                    job.cost
                        .map(|n| format!("${n:.5}"))
                        .unwrap_or_else(|| "n/a".into()),
                ),
            )
        }
        None => (
            " This workspace ".to_owned(),
            format!(
                "{}\n{}",
                library.root.display(),
                crate::parse_tui::latest(&library.root)
                    .unwrap_or_else(|| "Nothing has been parsed here yet.".into())
            ),
        ),
    };
    frame.render_widget(
        Paragraph::new(detail)
            .wrap(Wrap { trim: false })
            .block(panel(&title)),
        rows[2],
    );

    // Now: what is running.
    if let Some(job) = jobs.iter().find(|j| j.status == "running") {
        let ratio = if job.total > 0 {
            (job.done as f64 / job.total as f64).clamp(0.0, 1.0)
        } else {
            0.0
        };
        let spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"][(app.tick % 10) as usize];
        frame.render_widget(
            Gauge::default()
                .block(panel(" Now "))
                .gauge_style(Style::default().fg(ACCENT))
                .ratio(ratio)
                .label(format!(
                    "{spinner} #{} · {} · {}/{}",
                    job.id,
                    job.stage,
                    job.done,
                    if job.total > 0 {
                        job.total.to_string()
                    } else {
                        "?".into()
                    }
                )),
            rows[3],
        );
    } else {
        let idle = if queued > 0 {
            format!("  Nothing is running. {queued} queued: press q to start.")
        } else {
            "  Nothing is running.".to_owned()
        };
        frame.render_widget(Paragraph::new(idle).block(panel(" Now ")), rows[3]);
    }

    // Footer: the keys that apply right now, then the latest message.
    let wide = area.width >= 104;
    let hints = match (app.pane, wide) {
        (Pane::Actions, _) if jobs.is_empty() => {
            "↑↓ move · Enter run · letter = shortcut · h help · Esc close"
        }
        (Pane::Actions, true) => {
            "↑↓ move · Enter run · Tab documents · letter = shortcut · Esc close · Ctrl+S save, pause & exit"
        }
        (Pane::Actions, false) => "↑↓ move · Enter run · Tab documents · h help · Esc close",
        (Pane::Documents, true) => {
            "↑↓ move · Enter document menu · p pause · r resume/retry · c cancel · Tab actions · Esc close"
        }
        (Pane::Documents, false) => {
            "↑↓ move · Enter menu · p pause · r resume · c cancel · Tab actions"
        }
    };
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(Span::raw(format!("  {hints}"))),
            Line::from(Span::styled(
                format!("  {}", app.message),
                Style::default().fg(Color::DarkGray),
            )),
        ]),
        rows[4],
    );
}
