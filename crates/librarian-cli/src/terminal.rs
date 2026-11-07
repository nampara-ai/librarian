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
struct App {
    selected: usize,
    message: String,
    child: Option<Child>,
    tick: u64,
}

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

const HELP: &str = "HOME\nn  New Import\nq  Start queue (one file at a time)\np  Pause selected job\nr  Resume selected job and start queue\nc  Cancel selected job; originals and committed work remain\ns  Search\nf  Setup\nh  Help\nEnter / Space  Preview selected job\n\nEVERY SCREEN\nArrows / Tab / Shift+Tab  Navigate lists and forms\nEnter / Space  Activate a button, toggle, or browsable field\nEsc  Back; at Home, close the dashboard (worker continues)\nCtrl+S  Save accepted answers/drafts, pause work in this workspace, and exit\nx  Continue a completed automatic setup step\n\nFILE BROWSER\nEnter  Open folder / select file\nSpace  Select the folder you are currently inside\nEsc  Return through folder history, then back to the form\nBackspace  Parent folder\n/  Type a path\nn  Create folder when selecting a destination or workspace\nCtrl+B  Additional browse shortcut on source/destination fields\ne  Type a path on source/destination fields\n\nTEXT FIELDS\nEnter accepts; Esc discards the current edit. Spaces are ordinary text.\nHome shortcuts do not fire inside fields. Ctrl+S keeps previously accepted\nanswers; an unaccepted edit or unsaved API key is discarded.\n\nCOPY / PASTE\nUse your terminal's copy/paste commands (often Ctrl+Shift+C/V on Windows\nand Linux; Cmd+C/V on macOS). Librarian does not bind Ctrl+C in the TUI.\nMouse selection and clipboard shortcuts belong to your terminal.\n\nRUNTIME\nSetup establishes the active workspace, provider, secure credential, model,\nengines and defaults. Workers load it automatically. Existing queued jobs\nkeep their saved configuration. API keys are never written into config.\n\nCLI\nconfig schema · config edit · doctor · status --json\nshow ID · print ID · export ID --output FOLDER\nSee the bundled README for the complete terminal guide.";

fn dashboard(
    terminal: &mut ratatui::DefaultTerminal,
    library: &Library,
    config_path: &Path,
) -> Result<Option<Location>> {
    let mut app = App {
        selected: 0,
        message: "Arrows/Tab select · Enter/Space preview · Esc close · Ctrl+S save, pause & exit"
            .into(),
        child: None,
        tick: 0,
    };
    loop {
        let jobs = library.jobs(1000)?;
        app.selected = app.selected.min(jobs.len().saturating_sub(1));
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
        if let Some(delta) = ui::movement(key.code) {
            app.selected = ui::advance(app.selected, delta, jobs.len());
            continue;
        }
        let selected = jobs.get(app.selected).map(|j| j.id);
        let outcome = (|| -> Result<Option<Location>> {
            match key.code {
                KeyCode::Char('n') => import(terminal, &mut app, library, config_path)?,
                KeyCode::Char('q') => start_worker(&mut app, library, config_path)?,
                KeyCode::Char('p') => {
                    if let Some(id) = selected {
                        library.control(Some(id), "pause")?;
                        app.message = format!("Pause requested for #{id}; committed work retained");
                    }
                }
                KeyCode::Char('r') => {
                    if let Some(id) = selected {
                        library.control(Some(id), "resume")?;
                        start_worker(&mut app, library, config_path)?;
                    }
                }
                KeyCode::Char('c') => {
                    if let Some(id) = selected {
                        library.control(Some(id), "cancel")?;
                        app.message = format!("Cancel requested for #{id}; originals retained");
                    }
                }
                KeyCode::Char('s') => {
                    let mut query = String::new();
                    while let Some(value) = ui::input(
                        terminal,
                        "Search",
                        "Search accepted document text. Enter runs the search; Esc returns Home.",
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
                }
                KeyCode::Char('f') => {
                    return crate::onboarding::run(
                        terminal,
                        Location {
                            root: library.root.clone(),
                            config: config_path.to_owned(),
                        },
                    );
                }
                KeyCode::Char('h') => ui::notice(terminal, "Librarian controls", HELP)?,
                code if ui::activate(code) => {
                    if let Some(job) = jobs.get(app.selected) {
                        let (title, body) = preview(library, job)?;
                        ui::notice(terminal, &title, &body)?;
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
        if key.code == KeyCode::Esc {
            return Ok(None);
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
                "Source       {}",
                if w.source.is_empty() {
                    "Select a file or folder"
                } else {
                    &w.source
                }
            ),
            format!(
                "Destination  {}",
                if w.destination.is_empty() {
                    "Select or create an output folder"
                } else {
                    &w.destination
                }
            ),
            format!("{} Include subfolders", check(w.recursive)),
            format!("{} Create destination if missing", check(w.create)),
            format!("{} Extraction only (no model calls)", check(w.extract_only)),
            format!("{} Body-only Markdown", check(w.body_only)),
            format!("{} Start processing after queueing", check(w.start)),
            "Queue import".into(),
        ];
        // This form shares navigation with setup; browse fields also expose an
        // explicit typed-path option, so paths containing spaces work normally.
        let Some(index) = ui::path_form(
            t,
            "New Import",
            "Choose source and destination before queueing.
Enter/Space opens a path or toggles an option; Queue import submits.
Esc returns Home with this draft saved.",
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
                        app.message = format!("Queued {n} file(s). Processing is sequential.");
                        if w.start {
                            start_worker(app, library, config_path)?;
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

fn preview(library: &Library, job: &Job) -> Result<(String, String)> {
    if let Some(output) = &job.output {
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
        Ok((output.clone(), body))
    } else {
        Ok((
            format!("Job #{}", job.id),
            library
                .events(Some(job.id), 30)?
                .into_iter()
                .map(|(_, level, message)| format!("{level}: {message}"))
                .collect::<Vec<_>>()
                .join("\n\n"),
        ))
    }
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
    for json in providers {
        let config: Config = serde_json::from_str(&json)?;
        credentials::attach_session(&mut command, &config.provider)?;
    }
    let child = command.spawn().context("Could not start worker")?;
    app.child = Some(child);
    app.message = "Worker started. Files run one at a time.".into();
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
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Min(8),
            Constraint::Length(4),
            Constraint::Length(4),
            Constraint::Length(2),
        ])
        .split(area);
    let completed = jobs.iter().filter(|j| j.status == "complete").count();
    let review = jobs.iter().filter(|j| j.status == "needs_review").count();
    let queued = jobs.iter().filter(|j| j.status == "queued").count();
    frame.render_widget(
        Paragraph::new(Line::from(vec![
            Span::styled(
                "  LIBRARIAN  ",
                Style::default().fg(ACCENT).add_modifier(Modifier::BOLD),
            ),
            Span::raw(format!(
                "{completed} complete  ·  {review} review  ·  {queued} queued"
            )),
        ]))
        .block(Block::default().borders(Borders::BOTTOM)),
        rows[0],
    );
    let columns = Layout::default()
        .direction(Direction::Horizontal)
        .constraints([Constraint::Percentage(52), Constraint::Percentage(48)])
        .split(rows[1]);
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
                    format!("       {}  ·  {}", job.status, job.stage),
                    Style::default().fg(status_color(&job.status)),
                )),
            ])
        })
        .collect::<Vec<_>>();
    let mut state = ListState::default();
    if !jobs.is_empty() {
        state.select(Some(app.selected));
    }
    frame.render_stateful_widget(
        List::new(items)
            .block(panel(" Queue · newest first "))
            .highlight_style(Style::default().bg(Color::Rgb(25, 45, 55)))
            .highlight_symbol("›"),
        columns[0],
        &mut state,
    );
    let detail = if let Some(job) = jobs.get(app.selected) {
        format!(
            "SOURCE\n{}\n\nDESTINATION\n{}\n\nSTAGE  {}\nSTATUS  {}\n\nREQUESTS  {}   CACHE HITS  {}\nTOKENS  {} in / {} out\nCACHED INPUT  {}\nCOST  {}\n\n{}",
            job.source,
            job.destination,
            job.stage,
            job.status,
            job.requests,
            job.cache_hits,
            job.input_tokens,
            job.output_tokens,
            job.cached_tokens,
            job.cost
                .map(|n| format!("${n:.5} (configured rates)"))
                .unwrap_or_else(|| "rates not configured".into()),
            job.output
                .as_ref()
                .or(job.error.as_ref())
                .cloned()
                .unwrap_or_default()
        )
    } else {
        format!(
            "Your library is ready.\n\nPress n to select a file or folder and an output destination.\n\nPress f to configure your provider, OCR and Markdown preferences.\n\nLibrary:\n{}",
            library.root.display()
        )
    };
    frame.render_widget(
        Paragraph::new(detail)
            .wrap(Wrap { trim: false })
            .block(panel(" Selected document ")),
        columns[1],
    );
    if let Some(job) = jobs.iter().find(|j| j.status == "running") {
        let ratio = if job.total > 0 {
            (job.done as f64 / job.total as f64).clamp(0.0, 1.0)
        } else {
            0.0
        };
        let spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"][(app.tick % 10) as usize];
        frame.render_widget(
            Gauge::default()
                .block(panel(" Live progress "))
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
            rows[2],
        );
    } else {
        frame.render_widget(
            Paragraph::new("  No active job · q starts queued work").block(panel(" Progress ")),
            rows[2],
        );
    }
    frame.render_widget(Paragraph::new("  n New Import   q Start queue   p Pause   r Resume   c Cancel   s Search   f Setup   h Help").wrap(Wrap{trim:true}).block(panel(" Controls ")),rows[3]);
    frame.render_widget(
        Paragraph::new(app.message.as_str())
            .style(Style::default().fg(Color::DarkGray))
            .wrap(Wrap { trim: true }),
        rows[4],
    );
}
