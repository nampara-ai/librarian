//! Shared terminal controls used by onboarding, setup and the dashboard.
use anyhow::{Context, Result};
use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::{
    layout::{Constraint, Layout},
    style::{Color, Modifier, Style},
    widgets::{Block, Borders, List, ListItem, ListState, Paragraph, Wrap},
};
use std::{
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    time::Duration,
};
use zeroize::Zeroizing;

pub fn with_terminal<T>(
    work: impl FnOnce(&mut ratatui::DefaultTerminal) -> Result<T>,
) -> Result<T> {
    use std::io::IsTerminal;
    anyhow::ensure!(
        std::io::stdin().is_terminal() && std::io::stdout().is_terminal(),
        "This screen needs an interactive terminal. Supply explicit CLI arguments for scripts."
    );
    use crossterm::{
        event::{DisableBracketedPaste, EnableBracketedPaste},
        execute,
    };
    struct Restore;
    impl Drop for Restore {
        fn drop(&mut self) {
            let _ = execute!(std::io::stdout(), DisableBracketedPaste);
            ratatui::restore();
        }
    }
    let mut terminal = ratatui::init();
    let _restore = Restore;
    execute!(std::io::stdout(), EnableBracketedPaste)?;
    work(&mut terminal)
}

#[derive(Debug)]
pub struct SaveExit;
impl std::fmt::Display for SaveExit {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Progress saved; exit requested")
    }
}
impl std::error::Error for SaveExit {}
fn exit_key(k: KeyEvent) -> Result<()> {
    if k.code == KeyCode::Char('s') && k.modifiers.contains(KeyModifiers::CONTROL) {
        return Err(SaveExit.into());
    }
    Ok(())
}
pub fn movement(code: KeyCode) -> Option<isize> {
    match code {
        KeyCode::Up | KeyCode::Left | KeyCode::BackTab => Some(-1),
        KeyCode::Down | KeyCode::Right | KeyCode::Tab => Some(1),
        _ => None,
    }
}
pub fn activate(code: KeyCode) -> bool {
    matches!(code, KeyCode::Enter | KeyCode::Char(' '))
}
pub fn advance(selected: usize, delta: isize, count: usize) -> usize {
    if count == 0 {
        0
    } else {
        (selected as isize + delta).rem_euclid(count as isize) as usize
    }
}
pub fn clean(text: &str) -> String {
    text.chars().filter(|c| !c.is_control()).collect()
}
pub fn key() -> Result<Option<KeyEvent>> {
    if !event::poll(Duration::from_millis(100))? {
        return Ok(None);
    }
    match event::read()? {
        Event::Key(k) if k.kind == KeyEventKind::Press => {
            exit_key(k)?;
            Ok(Some(k))
        }
        _ => Ok(None),
    }
}
fn back(key: KeyEvent) -> bool {
    key.code == KeyCode::Esc
}

pub fn choose(
    terminal: &mut ratatui::DefaultTerminal,
    title: &str,
    description: &str,
    labels: &[String],
    initial: usize,
) -> Result<Option<usize>> {
    choose_impl(terminal, title, description, labels, initial, false)
}
pub fn path_form(
    terminal: &mut ratatui::DefaultTerminal,
    title: &str,
    description: &str,
    labels: &[String],
    initial: usize,
) -> Result<Option<usize>> {
    choose_impl(terminal, title, description, labels, initial, true)
}
fn choose_impl(
    terminal: &mut ratatui::DefaultTerminal,
    title: &str,
    description: &str,
    labels: &[String],
    initial: usize,
    paths: bool,
) -> Result<Option<usize>> {
    let mut selected = initial.min(labels.len().saturating_sub(1));
    let mut query = String::new();
    let mut description_scroll = 0u16;
    loop {
        let visible = labels
            .iter()
            .enumerate()
            .filter(|(_, s)| s.to_lowercase().contains(&query.to_lowercase()))
            .map(|(i, _)| i)
            .collect::<Vec<_>>();
        if !visible.contains(&selected) {
            selected = visible.first().copied().unwrap_or(0);
        }
        terminal.draw(|f| {
            let areas=Layout::vertical([Constraint::Length(3),Constraint::Length((description.lines().map(|line|(line.chars().count().max(1) as u16).div_ceil(f.area().width.max(1))).sum::<u16>()+1).min(f.area().height.saturating_sub(10))),Constraint::Min(4),Constraint::Length(3)]).split(f.area());
            f.render_widget(Paragraph::new(format!("  LIBRARIAN · {title}")).style(Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)).block(Block::default().borders(Borders::BOTTOM)),areas[0]);
            f.render_widget(Paragraph::new(description).wrap(Wrap{trim:false}).scroll((description_scroll,0)),areas[1]);
            let items=visible.iter().map(|i|ListItem::new(clean(&labels[*i]))).collect::<Vec<_>>();
            let mut state=ListState::default();state.select(visible.iter().position(|i|*i==selected));
            f.render_stateful_widget(List::new(items).highlight_symbol("› ").highlight_style(Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)).block(Block::default().borders(Borders::ALL).title(if query.is_empty(){" Choose ".into()}else{format!(" Filter: {query} ")})),areas[2],&mut state);
            f.render_widget(Paragraph::new(if paths{"↑/↓ or Tab/Shift+Tab move · Enter/Space activate · Esc back\nCtrl+B browse · e type focused path · Ctrl+S save & exit"}else{"↑/↓ or Tab/Shift+Tab move · Enter/Space choose · Esc back\nType to filter · PgUp/PgDn scroll details · Ctrl+S save & exit"}),areas[3]);
        })?;
        let Some(k) = key()? else {
            continue;
        };
        if back(k) {
            return Ok(None);
        }
        if k.code == KeyCode::PageDown {
            description_scroll = description_scroll.saturating_add(5);
            continue;
        }
        if k.code == KeyCode::PageUp {
            description_scroll = description_scroll.saturating_sub(5);
            continue;
        }
        if paths && selected < 2 {
            if k.code == KeyCode::Char('b') && k.modifiers.contains(KeyModifiers::CONTROL) {
                return Ok(Some(selected));
            }
            if k.code == KeyCode::Char('e') && k.modifiers.is_empty() {
                return Ok(Some(selected + labels.len()));
            }
        }
        if k.modifiers
            .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
        {
            continue;
        }
        if let Some(delta) = movement(k.code) {
            if !visible.is_empty() {
                let i = visible.iter().position(|i| *i == selected).unwrap_or(0);
                selected = visible[advance(i, delta, visible.len())];
            }
        } else if activate(k.code) && !visible.is_empty() {
            return Ok(Some(selected));
        } else if k.code == KeyCode::Backspace {
            query.pop();
        } else if let KeyCode::Char(c) = k.code
            && !paths
            && !k
                .modifiers
                .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
            && query.len() < 128
        {
            query.push(c);
        }
    }
}

// Keep the insertion point visible even for long paths and editorial prompts.
// The underlying value is unchanged; this only chooses the visible window.
fn input_window(text: &str, cursor: usize, width: usize) -> String {
    let mut start = cursor;
    let mut used = 0;
    let left_budget = width.saturating_sub(4);
    for (i, c) in text[..cursor].char_indices().rev() {
        let cells = ratatui::text::Span::raw(c.to_string()).width();
        if used + cells > left_budget {
            break;
        }
        used += cells;
        start = i;
    }
    let mut result = if start > 0 {
        "…".to_owned()
    } else {
        String::new()
    };
    result.extend(
        text[start..cursor]
            .chars()
            .map(|c| if c.is_control() { ' ' } else { c }),
    );
    result.push('▏');
    used += 1 + usize::from(start > 0);
    for c in text[cursor..].chars() {
        let c = if c.is_control() { ' ' } else { c };
        let cells = ratatui::text::Span::raw(c.to_string()).width();
        if used + cells > width {
            break;
        }
        result.push(c);
        used += cells;
    }
    result
}

pub fn input(
    terminal: &mut ratatui::DefaultTerminal,
    title: &str,
    description: &str,
    initial: &str,
    secret: bool,
) -> Result<Option<Zeroizing<String>>> {
    let mut text = Zeroizing::new(initial.to_owned());
    let mut cursor = text.len();
    loop {
        terminal.draw(|f| {
            let areas=Layout::vertical([Constraint::Length(3),Constraint::Min(3),Constraint::Length(4),Constraint::Length(3)]).split(f.area());
            f.render_widget(Paragraph::new(format!("  LIBRARIAN · {title}")).style(Style::default().fg(Color::Cyan)),areas[0]);
            f.render_widget(Paragraph::new(description).wrap(Wrap{trim:false}),areas[1]);
            let display=if secret {format!("{}▏{}","•".repeat(text[..cursor].chars().count().min(areas[2].width.saturating_sub(4) as usize)),"•".repeat(text[cursor..].chars().count().min(20)))}else{input_window(&text,cursor,areas[2].width.saturating_sub(2) as usize)};
            f.render_widget(Paragraph::new(display).wrap(Wrap{trim:false}).block(Block::default().borders(Borders::ALL).title(if secret{" API key · hidden "}else{" Value "})),areas[2]);
            f.render_widget(Paragraph::new("Enter accepts · Esc back · Left/Right moves cursor\nPaste supported · Ctrl+S saves accepted answers and exits (unaccepted text is discarded)."),areas[3]);
        })?;
        if !event::poll(Duration::from_millis(100))? {
            continue;
        }
        match event::read()? {
            Event::Paste(value) => {
                let value = Zeroizing::new(value);
                let value = value.trim_end_matches(['\n', '\r']);
                if text.len() + value.len() <= 16384 {
                    text.insert_str(cursor, value);
                    cursor += value.len();
                }
            }
            Event::Key(k) if k.kind == KeyEventKind::Press => {
                exit_key(k)?;
                if back(k) {
                    return Ok(None);
                }
                match k.code {
                    KeyCode::Enter => return Ok(Some(text)),
                    KeyCode::Left => {
                        cursor = text[..cursor]
                            .char_indices()
                            .next_back()
                            .map(|(i, _)| i)
                            .unwrap_or(0);
                    }
                    KeyCode::Right => {
                        if let Some(c) = text[cursor..].chars().next() {
                            cursor += c.len_utf8();
                        }
                    }
                    KeyCode::Home => cursor = 0,
                    KeyCode::End => cursor = text.len(),
                    KeyCode::Backspace if cursor > 0 => {
                        let start = text[..cursor].char_indices().next_back().unwrap().0;
                        text.replace_range(start..cursor, "");
                        cursor = start;
                    }
                    KeyCode::Delete if cursor < text.len() => {
                        let end = cursor + text[cursor..].chars().next().unwrap().len_utf8();
                        text.replace_range(cursor..end, "");
                    }
                    KeyCode::Char('u') if k.modifiers.contains(KeyModifiers::CONTROL) => {
                        text.clear();
                        cursor = 0;
                    }
                    KeyCode::Char(c)
                        if !k
                            .modifiers
                            .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
                            && text.len() < 16384
                            && !c.is_control() =>
                    {
                        text.insert(cursor, c);
                        cursor += c.len_utf8();
                    }
                    _ => {}
                }
            }
            _ => {}
        }
    }
}

pub fn notice(terminal: &mut ratatui::DefaultTerminal, title: &str, body: &str) -> Result<()> {
    let mut scroll = 0u16;
    loop {
        terminal.draw(|f| {
            let areas =
                Layout::vertical([Constraint::Min(3), Constraint::Length(2)]).split(f.area());
            f.render_widget(
                Paragraph::new(body)
                    .wrap(Wrap { trim: false })
                    .scroll((scroll, 0))
                    .block(Block::default().borders(Borders::ALL).title(title)),
                areas[0],
            );
            f.render_widget(
                Paragraph::new(
                    "Arrows/Tab scroll · Enter/Space close · Esc back · Ctrl+S save & exit",
                ),
                areas[1],
            );
        })?;
        if let Some(k) = key()? {
            if back(k) || activate(k.code) || k.code == KeyCode::Char('x') {
                return Ok(());
            }
            if k.code == KeyCode::PageDown {
                scroll = scroll.saturating_add(15);
            }
            if k.code == KeyCode::PageUp {
                scroll = scroll.saturating_sub(15);
            }
            if k.code == KeyCode::Home {
                scroll = 0;
            }
            if let Some(delta) = movement(k.code) {
                scroll = scroll.saturating_add_signed(delta as i16);
            }
        }
    }
}

pub fn browse(
    terminal: &mut ratatui::DefaultTerminal,
    start: &Path,
    directories_only: bool,
) -> Result<Option<PathBuf>> {
    let mut folder = if start.is_dir() {
        start.to_owned()
    } else {
        start
            .parent()
            .filter(|p| p.is_dir())
            .unwrap_or(Path::new("."))
            .to_owned()
    }
    .canonicalize()?;
    let mut history = Vec::new();
    let mut selected = 0usize;
    let mut message = String::new();
    loop {
        let mut entries = std::fs::read_dir(&folder)
            .with_context(|| format!("Cannot read {}", folder.display()))?
            .filter_map(Result::ok)
            .filter(|e| {
                e.file_type()
                    .is_ok_and(|t| !t.is_symlink() && (!directories_only || t.is_dir()))
            })
            .map(|e| e.path())
            .collect::<Vec<_>>();
        entries.sort_by_key(|p| (!p.is_dir(), p.file_name().map(|s| s.to_os_string())));
        selected = selected.min(entries.len().saturating_sub(1));
        terminal.draw(|f|{
            let areas=Layout::vertical([Constraint::Length(4),Constraint::Min(4),Constraint::Length(4)]).split(f.area());
            f.render_widget(Paragraph::new(clean(&folder.to_string_lossy())).wrap(Wrap{trim:false}).block(Block::default().borders(Borders::ALL).title(" Browse · Space selects this folder ")),areas[0]);
            let items=entries.iter().map(|p|ListItem::new(format!("{} {}",if p.is_dir(){"▸"}else{" "},clean(&p.file_name().unwrap_or_default().to_string_lossy())))).collect::<Vec<_>>();
            let mut state=ListState::default();state.select((!entries.is_empty()).then_some(selected));
            f.render_stateful_widget(List::new(items).highlight_symbol("› ").highlight_style(Style::default().fg(Color::Cyan)),areas[1],&mut state);
            f.render_widget(Paragraph::new(format!("↑/↓ or Tab/Shift+Tab move · Enter opens a folder/selects a file\nSpace selects this folder · Esc back · Backspace parent · / type path · Ctrl+S exit{}\n{message}",if directories_only{" · n New folder"}else{""})),areas[2]);
        })?;
        let Some(k) = key()? else {
            continue;
        };
        if back(k) {
            if let Some((previous, index)) = history.pop() {
                folder = previous;
                selected = index;
                continue;
            }
            return Ok(None);
        }
        if let Some(delta) = movement(k.code) {
            selected = advance(selected, delta, entries.len());
            continue;
        }
        if k.modifiers
            .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT)
        {
            continue;
        }
        let target = match k.code {
            KeyCode::Char(' ') => return Ok(Some(folder)),
            KeyCode::Enter => entries.get(selected).cloned(),
            KeyCode::Backspace => folder.parent().map(Path::to_owned),
            KeyCode::Char('/') => input(
                terminal,
                "Go to folder",
                "Enter a folder path. ~ expands to your home folder.",
                &folder.to_string_lossy(),
                false,
            )?
            .map(|p| crate::setup::expand_path(&p)),
            KeyCode::Char('n') if directories_only => {
                if let Some(name) = input(
                    terminal,
                    "Create folder",
                    "Enter the new folder name.",
                    "",
                    false,
                )? {
                    if name.is_empty()
                        || name.contains(['/', '\\'])
                        || matches!(name.as_str(), "." | "..")
                    {
                        message = "Use a single folder name".into();
                    } else {
                        let path = folder.join(name.as_str());
                        match std::fs::create_dir(&path) {
                            Ok(()) => {
                                history.push((folder, selected));
                                folder = path;
                                selected = 0;
                                message.clear();
                            }
                            Err(e) => message = e.to_string(),
                        }
                    }
                }
                None
            }
            _ => None,
        };
        if let Some(path) = target {
            if path.is_dir() {
                if std::fs::read_dir(&path).is_ok() {
                    history.push((folder, selected));
                    folder = path;
                    selected = 0;
                    message.clear();
                } else {
                    message = "Cannot read that folder".into();
                }
            } else if path.is_file() && !directories_only {
                return Ok(Some(path));
            } else {
                message = "Choose an accessible folder".into();
            }
        }
    }
}

pub fn task<T: Send + 'static>(
    terminal: &mut ratatui::DefaultTerminal,
    title: &str,
    description: &str,
    work: impl FnOnce(mpsc::Sender<String>, Arc<AtomicBool>) -> Result<T> + Send + 'static,
) -> Result<Option<T>> {
    let (messages, updates) = mpsc::channel();
    let (send, receive) = mpsc::channel();
    let cancel = Arc::new(AtomicBool::new(false));
    let flag = cancel.clone();
    std::thread::Builder::new()
        .name("librarian-setup".into())
        .spawn(move || {
            let _ = send.send(work(messages, flag));
        })?;
    let mut message = "Working…".to_string();
    let mut completed = None;
    let mut tick = 0usize;
    loop {
        while let Ok(update) = updates.try_recv() {
            message = update;
        }
        if completed.is_none() {
            match receive.try_recv() {
                Ok(result) => {
                    completed = Some(result?);
                    message =
                        format!("✓ {message}\n\nComplete. Press x, Enter or Space to continue.");
                }
                Err(mpsc::TryRecvError::Disconnected) => {
                    anyhow::bail!("Setup operation ended without a result")
                }
                Err(_) => {}
            }
        }
        terminal.draw(|f|{
            let spinner=["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"][tick%10];
            f.render_widget(Paragraph::new(format!("  LIBRARIAN · {title}\n\n{description}\n\n{spinner} {message}\n\nEsc back · Ctrl+S save & exit")).wrap(Wrap{trim:false}).block(Block::default().borders(Borders::ALL)),f.area());
        })?;
        tick += 1;
        let next = key();
        if next.is_err() {
            cancel.store(true, Ordering::Relaxed);
            if completed.is_none() {
                let _ = receive.recv_timeout(Duration::from_secs(2));
            }
        }
        if let Some(k) = next? {
            if back(k) {
                cancel.store(true, Ordering::Relaxed);
                if completed.is_none() {
                    let _ = receive.recv_timeout(Duration::from_secs(2));
                }
                return Ok(None);
            }
            if (activate(k.code) || k.code == KeyCode::Char('x')) && completed.is_some() {
                return Ok(completed);
            }
        }
    }
}
