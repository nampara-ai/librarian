use anyhow::{Context, Result, bail};
use crossterm::event::{self, Event, KeyCode, KeyEventKind, KeyModifiers};
use librarian_native::{
    config::Config,
    pipeline,
    store::{Job, Library},
};
use ratatui::{
    Frame,
    layout::{Constraint, Direction, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, Clear, Gauge, List, ListItem, ListState, Paragraph, Wrap},
};
use std::{
    io::{self, IsTerminal, Read},
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

enum Modal {
    None,
    Wizard(Wizard),
    Search(String),
    Message(String, String),
    Browser(Browser),
}
#[derive(Clone)]
struct Wizard {
    source: String,
    destination: String,
    recursive: bool,
    create: bool,
    extract_only: bool,
    body_only: bool,
    start: bool,
    field: usize,
}
struct Browser {
    folder: PathBuf,
    entries: Vec<PathBuf>,
    selected: usize,
    wizard: Wizard,
    destination: bool,
}
struct App {
    selected: usize,
    modal: Modal,
    message: String,
    child: Option<Child>,
    tick: u64,
    modal_scroll: u16,
}

pub fn run(root: &Path, config_path: &Path) -> Result<()> {
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        bail!(
            "The dashboard requires an interactive terminal; use clean --output or status --json"
        );
    }
    let library = Library::open(root)?;
    let mut terminal = ratatui::init();
    let result = dashboard(&mut terminal, &library, config_path);
    ratatui::restore();
    result
}

fn dashboard(
    terminal: &mut ratatui::DefaultTerminal,
    library: &Library,
    config_path: &Path,
) -> Result<()> {
    let mut app = App {
        selected: 0,
        modal: Modal::None,
        modal_scroll: 0,
        message: "n new import · s start queue · , setup · / search · ? help".into(),
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
        if !event::poll(Duration::from_millis(180))? {
            continue;
        }
        let Event::Key(key) = event::read()? else {
            continue;
        };
        if key.kind != KeyEventKind::Press {
            continue;
        }
        if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
            library.control(None, "pause")?;
            break;
        }
        let selected = jobs.get(app.selected).map(|j| j.id);
        let modal = std::mem::replace(&mut app.modal, Modal::None);
        if !matches!(modal, Modal::Message(..)) {
            app.modal_scroll = 0;
        }
        match modal{
            Modal::Wizard(mut w)=>{
                match key.code{
                    KeyCode::Esc=>{},
                    KeyCode::Tab=>{w.field=(w.field+1)%7;app.modal=Modal::Wizard(w);},
                    KeyCode::BackTab=>{w.field=(w.field+6)%7;app.modal=Modal::Wizard(w);},
                    KeyCode::Char('b') if key.modifiers.contains(KeyModifiers::CONTROL)&&w.field<2=>{
                        let destination=w.field==1;let chosen=crate::setup::expand_path(if destination{&w.destination}else{&w.source});let folder=if chosen.is_dir(){chosen}else{chosen.parent().filter(|p|p.is_dir()).map(Path::to_owned).unwrap_or(std::env::current_dir()?)};
                        open_browser(&mut app,folder,w,destination);
                    }
                    KeyCode::Enter if w.field==6=>{
                        let result=(||->Result<usize>{let mut config=Config::load(config_path)?;if w.extract_only{config.processing.edit=false;config.processing.classify=false;config.processing.vision=false;}
                            if w.body_only{config.output.frontmatter=false;config.output.title=false;config.output.summary=false;config.output.page_markers=false;config.output.review_notes=false;}
                            let ids=pipeline::queue(library,&crate::setup::expand_path(&w.source),&crate::setup::expand_path(&w.destination),&config,w.recursive,w.create)?;if w.start{start_worker(&mut app,library,config_path)?;}Ok(ids.len())})();
                        match result{Ok(n)=>app.message=format!("Queued {n} file(s). Processing is sequential."),Err(e)=>{app.message=format!("{e:#}");app.modal=Modal::Wizard(w);}}
                    }
                    KeyCode::Enter=>{w.field+=1;app.modal=Modal::Wizard(w);},
                    KeyCode::Char(' ') if w.field>=2=>{match w.field{2=>w.recursive = !w.recursive,3=>w.create = !w.create,4=>w.extract_only = !w.extract_only,5=>w.body_only = !w.body_only,6=>w.start = !w.start,_=>{}}app.modal=Modal::Wizard(w);},
                    KeyCode::Backspace if w.field<2=>{if w.field==0{w.source.pop();}else{w.destination.pop();}app.modal=Modal::Wizard(w);},
                    KeyCode::Char(c) if w.field<2=>{if w.field==0{w.source.push(c);}else{w.destination.push(c);}app.modal=Modal::Wizard(w);},
                    _=>app.modal=Modal::Wizard(w),
                }
            }
            Modal::Browser(mut b)=>{
                match key.code{
                    KeyCode::Esc=>app.modal=Modal::Wizard(b.wizard),
                    KeyCode::Down|KeyCode::Char('j')=>{b.selected=(b.selected+1).min(b.entries.len().saturating_sub(1));app.modal=Modal::Browser(b);},
                    KeyCode::Up|KeyCode::Char('k')=>{b.selected=b.selected.saturating_sub(1);app.modal=Modal::Browser(b);},
                    KeyCode::Backspace=>{let folder=b.folder.parent().map(Path::to_owned).unwrap_or_else(||b.folder.clone());open_browser(&mut app,folder,b.wizard,b.destination);},
                    KeyCode::Char(' ')=>{let chosen=b.folder.to_string_lossy().into_owned();if b.destination{b.wizard.destination=chosen;}else{b.wizard.source=chosen;}app.modal=Modal::Wizard(b.wizard);},
                    KeyCode::Enter=>{if let Some(path)=b.entries.get(b.selected).cloned(){if path.is_dir(){open_browser(&mut app,path,b.wizard,b.destination);}else if !b.destination{b.wizard.source=path.to_string_lossy().into_owned();app.modal=Modal::Wizard(b.wizard);}else{app.modal=Modal::Browser(b);}}else{app.modal=Modal::Browser(b);}},
                    _=>app.modal=Modal::Browser(b),
                }
            }
            Modal::Search(mut query)=>{match key.code{KeyCode::Esc=>{},KeyCode::Enter=>{match library.search(&query,30){Ok(results)=>{let body=results.into_iter().map(|r|format!("#{}  {}\n{}\n",r["job"],r["title"].as_str().unwrap_or(""),r["snippet"].as_str().unwrap_or(""))).collect::<Vec<_>>().join("\n");app.modal=Modal::Message(format!("Search: {query}"),if body.is_empty(){"No matches".into()}else{body});},Err(e)=>app.message=e.to_string()}},KeyCode::Backspace=>{query.pop();app.modal=Modal::Search(query);},KeyCode::Char(c)=>{query.push(c);app.modal=Modal::Search(query);},_=>app.modal=Modal::Search(query)}}
            Modal::Message(title,body)=>{if !matches!(key.code,KeyCode::Esc|KeyCode::Enter|KeyCode::Char('q')){match key.code {KeyCode::Down|KeyCode::Char('j')=>app.modal_scroll=app.modal_scroll.saturating_add(1),KeyCode::Up|KeyCode::Char('k')=>app.modal_scroll=app.modal_scroll.saturating_sub(1),KeyCode::PageDown=>app.modal_scroll=app.modal_scroll.saturating_add(20),KeyCode::PageUp=>app.modal_scroll=app.modal_scroll.saturating_sub(20),KeyCode::Home=>app.modal_scroll=0,_=>{}}app.modal=Modal::Message(title,body);}}
            Modal::None=>match key.code{
                KeyCode::Char('q')|KeyCode::Esc=>break,
                KeyCode::Down|KeyCode::Char('j')=>app.selected=(app.selected+1).min(jobs.len().saturating_sub(1)),
                KeyCode::Up|KeyCode::Char('k')=>app.selected=app.selected.saturating_sub(1),
                KeyCode::Char('n')=>app.modal=Modal::Wizard(Wizard{source:String::new(),destination:String::new(),recursive:false,create:false,extract_only:false,body_only:false,start:true,field:0}),
                KeyCode::Char('s')=>{if let Err(e)=start_worker(&mut app,library,config_path){app.message=format!("{e:#}");}},
                KeyCode::Char('p')=>{if let Some(id)=selected{library.control(Some(id),"pause")?;app.message=format!("Paused #{id}; committed work retained");}},
                KeyCode::Char('r')=>{if let Some(id)=selected{library.control(Some(id),"resume")?;if let Err(e)=start_worker(&mut app,library,config_path){app.message=e.to_string();}}},
                KeyCode::Char('c')=>{if let Some(id)=selected{library.control(Some(id),"cancel")?;app.message=format!("Cancelled #{id}; originals retained");}},
                KeyCode::Char('/')=>app.modal=Modal::Search(String::new()),
                KeyCode::Char(',')=>{
                    ratatui::restore();let result=crate::setup::configure(config_path);*terminal=ratatui::init();app.message=match result{Ok(())=>"Configuration saved. New jobs use these settings.".into(),Err(e)=>format!("{e:#}")};
                }
                KeyCode::Char('v')|KeyCode::Enter=>{if let Some(job)=jobs.get(app.selected){match preview(library,job) {Ok((title,body))=>app.modal=Modal::Message(title,body),Err(e)=>app.message=format!("{e:#}")}}},
                KeyCode::Char('?')=>app.modal=Modal::Message("Keyboard controls".into(),"n  New import: source → destination → options → start\nCtrl+B  Browse files/folders inside the import form\ns  Start queued files (one file at a time)\np  Pause selected job\nr  Resume selected job and start worker\nc  Cancel selected job\nv / Enter  Preview output or diagnostics\n/  Search document text\n,  Configure provider, style and output options\nj/k or arrows  Select job\nq  Close dashboard; background worker continues\nCtrl+C  Pause active/queued jobs and close\n\nCLI: config schema · config edit · engines install\n     show ID · print ID · export ID --output FOLDER\n\nFiles requiring review remain clearly marked.\nAPI credentials are read from configured environment variables.".into()),
                _=>{},
            },
        }
    }
    Ok(())
}

fn browser(folder: PathBuf, wizard: Wizard, destination: bool) -> Result<Browser> {
    let mut entries = std::fs::read_dir(&folder)?
        .filter_map(|e| e.ok())
        .filter(|e| {
            e.file_type()
                .is_ok_and(|k| !k.is_symlink() && (!destination || k.is_dir()))
        })
        .map(|e| e.path())
        .collect::<Vec<_>>();
    entries.sort_by_key(|p| (!p.is_dir(), p.file_name().map(|s| s.to_os_string())));
    Ok(Browser {
        folder,
        entries,
        selected: 0,
        wizard,
        destination,
    })
}
fn open_browser(app: &mut App, folder: PathBuf, wizard: Wizard, destination: bool) {
    match browser(folder, wizard.clone(), destination) {
        Ok(browser) => app.modal = Modal::Browser(browser),
        Err(error) => {
            app.message = format!("Cannot open folder: {error}");
            app.modal = Modal::Wizard(wizard);
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
    let child = std::process::Command::new(std::env::current_exe()?)
        .arg("--library")
        .arg(&library.root)
        .arg("--config")
        .arg(config)
        .args(["run", "--quiet"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(log)
        .spawn()
        .context("Could not start worker")?;
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
fn centered(area: Rect, width: u16, height: u16) -> Rect {
    let width = width.min(area.width.saturating_sub(2));
    let height = height.min(area.height.saturating_sub(2));
    Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    )
}
fn draw(frame: &mut Frame<'_>, jobs: &[Job], app: &App, library: &Library) {
    let area = frame.area();
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Min(8),
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
            "Your library is ready.\n\nPress n to select a file or folder and an output destination.\n\nPress , to configure your provider, OCR and Markdown preferences.\n\nLibrary:\n{}",
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
        frame.render_widget(Paragraph::new("  n New import   s Start queue   p Pause   r Resume   c Cancel   / Search   , Setup   ? Help").wrap(Wrap{trim:true}).block(panel(" Controls ")),rows[2]);
    }
    frame.render_widget(
        Paragraph::new(app.message.as_str())
            .style(Style::default().fg(Color::DarkGray))
            .wrap(Wrap { trim: true }),
        rows[3],
    );
    match &app.modal {
        Modal::None => {}
        Modal::Wizard(w) => {
            let popup = centered(area, 88, 23);
            frame.render_widget(Clear, popup);
            let check = |b| if b { "[x]" } else { "[ ]" };
            let fields = [
                format!("Source       {}", w.source),
                format!("Destination  {}", w.destination),
                format!("{} Include subfolders", check(w.recursive)),
                format!("{} Create destination if missing", check(w.create)),
                format!("{} Extraction only (no model calls)", check(w.extract_only)),
                format!("{} Body-only Markdown", check(w.body_only)),
                format!("{} Start processing after queueing", check(w.start)),
            ];
            let mut lines = vec![
                Line::from(""),
                Line::from("  Select the source and destination before processing."),
                Line::from(""),
            ];
            for (i, line) in fields.iter().enumerate() {
                lines.push(Line::from(Span::styled(
                    format!(" {} {line}", if i == w.field { "›" } else { " " }),
                    if i == w.field {
                        Style::default().fg(ACCENT).add_modifier(Modifier::BOLD)
                    } else {
                        Style::default()
                    },
                )));
                lines.push(Line::from(""));
            }
            lines.push(Line::from("  Tab next · Shift+Tab previous · Space toggle"));
            lines.push(Line::from(
                "  Ctrl+B browse · Enter on last field queues · Esc cancel",
            ));
            frame.render_widget(
                Paragraph::new(lines)
                    .block(panel(" New import "))
                    .wrap(Wrap { trim: false }),
                popup,
            );
        }
        Modal::Browser(b) => {
            let popup = centered(area, 90, 28);
            frame.render_widget(Clear, popup);
            let parts = Layout::vertical([
                Constraint::Length(3),
                Constraint::Min(3),
                Constraint::Length(2),
            ])
            .split(popup);
            frame.render_widget(
                Paragraph::new(b.folder.to_string_lossy().into_owned())
                    .block(panel(" Select file or folder ")),
                parts[0],
            );
            let items = b
                .entries
                .iter()
                .map(|p| {
                    ListItem::new(format!(
                        "{} {}",
                        if p.is_dir() { "▸" } else { " " },
                        p.file_name().unwrap_or_default().to_string_lossy()
                    ))
                })
                .collect::<Vec<_>>();
            let mut state = ListState::default();
            state.select(Some(b.selected));
            frame.render_stateful_widget(
                List::new(items)
                    .highlight_style(Style::default().fg(ACCENT))
                    .highlight_symbol("› "),
                parts[1],
                &mut state,
            );
            frame.render_widget(
                Paragraph::new(
                    "Enter open/select file · Space select this folder · Backspace up · Esc back",
                ),
                parts[2],
            );
        }
        Modal::Search(query) => {
            let popup = centered(area, 80, 5);
            frame.render_widget(Clear, popup);
            frame.render_widget(
                Paragraph::new(format!("\n  {query}▏"))
                    .block(panel(" Search · Enter to search · Esc cancel ")),
                popup,
            );
        }
        Modal::Message(title, body) => {
            let popup = centered(area, 100, 30);
            frame.render_widget(Clear, popup);
            frame.render_widget(
                Paragraph::new(body.as_str())
                    .block(panel(title))
                    .scroll((app.modal_scroll, 0))
                    .wrap(Wrap { trim: false }),
                popup,
            );
        }
    }
}
