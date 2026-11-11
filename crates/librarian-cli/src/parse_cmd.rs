//! `librarian parse`: deterministic PDF parsing for one file or a folder.
//! The same executor serves the command line and the terminal interface.
use anyhow::{Context, Result, bail};
use librarian_core::layout::{self, RenderOptions};
use librarian_native::{parse, write_new_file, write_new_markdown};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    io::{self, Write},
    path::{Path, PathBuf},
    time::Instant,
};

pub struct Request<'a> {
    pub root: &'a Path,
    pub source: &'a Path,
    pub output: Option<&'a Path>,
    pub create_output: bool,
    pub recursive: bool,
    pub options: RenderOptions,
    pub words: bool,
    pub pdfium: Option<&'a Path>,
    pub json: bool,
    /// Set by the caller to stop before the next file.
    pub stop: Option<&'a std::sync::atomic::AtomicBool>,
}

/// What parsing one file produced.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Parsed {
    pub source: PathBuf,
    pub markdown: Option<PathBuf>,
    pub json: Option<PathBuf>,
    pub pages: u32,
    pub words: u32,
    pub elements: u32,
    pub roles: BTreeMap<String, u32>,
    pub routing: BTreeMap<String, u32>,
    pub hyphens_joined: u32,
    pub hyphens_kept: u32,
    pub open_ms: f64,
    pub extract_ms: f64,
    pub layout_ms: f64,
    pub total_ms: f64,
}

impl Parsed {
    pub fn summary(&self) -> serde_json::Value {
        serde_json::json!({
            "source": self.source.display().to_string(),
            "pages": self.pages,
            "words": self.words,
            "elements": self.elements,
            "roles": self.roles,
            "routing": self.routing,
            "hyphens": {"joined": self.hyphens_joined, "kept": self.hyphens_kept},
            "model_calls": 0,
            "timing_ms": {
                "open": self.open_ms,
                "extract": self.extract_ms,
                "layout": self.layout_ms,
                "total": self.total_ms,
            },
        })
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Outcome {
    pub files: Vec<Parsed>,
    pub failures: Vec<(PathBuf, String)>,
    pub seconds: f64,
    pub destination: Option<PathBuf>,
    /// Files found but not parsed because the run was stopped.
    #[serde(default)]
    pub skipped: usize,
}

impl Outcome {
    pub fn pages(&self) -> u64 {
        self.files.iter().map(|f| f.pages as u64).sum()
    }
    fn totals(&self, pick: impl Fn(&Parsed) -> &BTreeMap<String, u32>) -> BTreeMap<String, u32> {
        let mut totals = BTreeMap::new();
        for file in &self.files {
            for (key, count) in pick(file) {
                *totals.entry(key.clone()).or_insert(0) += count;
            }
        }
        totals
    }
    pub fn roles(&self) -> BTreeMap<String, u32> {
        self.totals(|f| &f.roles)
    }
    pub fn routing(&self) -> BTreeMap<String, u32> {
        self.totals(|f| &f.routing)
    }
}

pub fn collect(folder: &Path, recursive: bool, found: &mut Vec<PathBuf>) -> Result<()> {
    let mut entries: Vec<PathBuf> = std::fs::read_dir(folder)
        .with_context(|| format!("Cannot read {}", folder.display()))?
        .filter_map(|e| e.ok().map(|e| e.path()))
        .collect();
    entries.sort();
    for path in entries {
        if path.is_dir() {
            if recursive {
                collect(&path, recursive, found)?;
            }
        } else if parse::supported(&path) {
            found.push(path);
        }
    }
    Ok(())
}

pub fn counts(values: &BTreeMap<String, u32>) -> String {
    values
        .iter()
        .map(|(k, v)| {
            let name = match (k.as_str(), *v) {
                ("code", 1) => "code block".to_owned(),
                ("code", _) => "code blocks".to_owned(),
                (_, 1) => k.replace('_', " "),
                _ => format!("{}s", k.replace('_', " ")),
            };
            format!("{v} {name}")
        })
        .collect::<Vec<_>>()
        .join(", ")
}

/// Plain words for what each page still needs after parsing.
pub fn routing_words(routing: &BTreeMap<String, u32>) -> String {
    let get = |key: &str| routing.get(key).copied().unwrap_or(0);
    let mut parts = vec![format!("{} ready as parsed", get("deterministic"))];
    for (key, label) in [
        ("vision", "with figures worth describing"),
        ("review", "with a table to confirm"),
        ("ocr", "without readable text (need OCR)"),
    ] {
        if get(key) > 0 {
            parts.push(format!("{} {label}", get(key)));
        }
    }
    parts.join(" · ")
}

/// Parse one file; write `<stem>.md` and `<stem>.layout.json` when a folder
/// is given. Existing files are never replaced.
fn one(
    engine: &parse::Pdfium,
    source: &Path,
    folder: Option<&Path>,
    options: RenderOptions,
    words: bool,
) -> Result<(Parsed, String)> {
    let started = Instant::now();
    let parsed = parse::parse(engine, source)?;
    let markdown = layout::markdown(&parsed.document, options);
    let stats = &parsed.document.stats;
    let mut result = Parsed {
        source: source.to_owned(),
        markdown: None,
        json: None,
        pages: stats.pages,
        words: stats.words,
        elements: stats.elements,
        roles: stats.roles.clone(),
        routing: stats.tiers.clone(),
        hyphens_joined: stats.hyphens_removed,
        hyphens_kept: stats.hyphens_kept,
        open_ms: parsed.timing.open_ms,
        extract_ms: parsed.timing.extract_ms,
        layout_ms: parsed.timing.layout_ms,
        total_ms: started.elapsed().as_secs_f64() * 1e3,
    };
    let Some(folder) = folder else {
        return Ok((result, markdown));
    };
    let stem = source
        .file_stem()
        .and_then(|s| s.to_str())
        .context("Source file has no usable name")?;
    let md_path = folder.join(format!("{stem}.md"));
    let json_path = folder.join(format!("{stem}.layout.json"));
    for path in [&md_path, &json_path] {
        if path.exists() {
            bail!(
                "{} already exists; existing outputs are never replaced",
                path.display()
            );
        }
    }
    let mut body = serde_json::json!({ "summary": result.summary() });
    if words {
        body["pages"] = serde_json::to_value(&parsed.input.pages)?;
        body["fonts"] = serde_json::to_value(&parsed.input.fonts)?;
        body["outline"] = serde_json::to_value(&parsed.input.outline)?;
        body["document"] = serde_json::to_value(&parsed.document)?;
    } else {
        body["document"] = serde_json::to_value(parsed.document.without_word_refs())?;
    }
    write_new_file(&json_path, &serde_json::to_vec_pretty(&body)?)?;
    write_new_markdown(&md_path, &markdown)?;
    result.total_ms = started.elapsed().as_secs_f64() * 1e3;
    result.markdown = Some(md_path);
    result.json = Some(json_path);
    Ok((result, markdown))
}

/// Parse a file or every PDF in a folder. One bad file does not stop the rest.
/// `progress` receives a line before each file. A stop request takes effect
/// between files; the file in hand is finished and its outputs are kept.
pub fn execute(
    request: &Request<'_>,
    mut progress: impl FnMut(&str),
    mut each: impl FnMut(&Result<Parsed, (PathBuf, String)>),
) -> Result<Outcome> {
    let engine = parse::pdfium(request.root, request.pdfium)?;
    if let Some(folder) = request.output
        && !folder.is_dir()
    {
        if request.create_output {
            std::fs::create_dir_all(folder)?;
        } else {
            bail!(
                "Output folder {} does not exist; choose to create it",
                folder.display()
            );
        }
    }
    let mut sources = Vec::new();
    if request.source.is_dir() {
        if request.output.is_none() {
            bail!("Parsing a folder needs an output folder");
        }
        collect(request.source, request.recursive, &mut sources)?;
        if sources.is_empty() {
            bail!("No PDF files found in {}", request.source.display());
        }
    } else {
        sources.push(request.source.to_owned());
    }
    let started = Instant::now();
    let mut outcome = Outcome {
        destination: request.output.map(Path::to_owned),
        ..Default::default()
    };
    let total = sources.len();
    for (index, source) in sources.iter().enumerate() {
        if request
            .stop
            .is_some_and(|stop| stop.load(std::sync::atomic::Ordering::Relaxed))
        {
            outcome.skipped = total - index;
            break;
        }
        let name = source
            .file_name()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned();
        progress(&format!("Parsing {} of {total}: {name}", index + 1));
        match one(
            &engine,
            source,
            request.output,
            request.options,
            request.words,
        ) {
            Ok((parsed, _)) => {
                each(&Ok(parsed.clone()));
                outcome.files.push(parsed);
            }
            Err(error) => {
                let failure = (source.clone(), format!("{error:#}"));
                each(&Err(failure.clone()));
                outcome.failures.push(failure);
            }
        }
    }
    outcome.seconds = started.elapsed().as_secs_f64();
    Ok(outcome)
}

pub fn run(request: Request<'_>) -> Result<u8> {
    // A single file without an output folder prints its Markdown.
    if !request.source.is_dir() && request.output.is_none() {
        let engine = parse::pdfium(request.root, request.pdfium)?;
        let (parsed, markdown) = one(&engine, request.source, None, request.options, false)?;
        io::stdout().lock().write_all(markdown.as_bytes())?;
        eprintln!("{}", serde_json::to_string(&parsed.summary())?);
        return Ok(0);
    }
    let json = request.json;
    let folder_input = request.source.is_dir();
    let outcome = execute(
        &request,
        |_| {},
        |result| match result {
            Ok(parsed) if json => {
                println!("{}", parsed.summary());
            }
            Err((source, error)) if json => {
                println!(
                    "{}",
                    serde_json::json!({"source": source.display().to_string(), "error": error})
                );
            }
            Err((source, error)) => eprintln!("Failed {}: {error}", source.display()),
            Ok(_) => {}
        },
    )?;
    if !folder_input && let Some((_, error)) = outcome.failures.first() {
        bail!("{error}");
    }
    if !json {
        if folder_input {
            println!(
                "Parsed {} of {} files ({} pages) in {:.1} s with no model calls",
                outcome.files.len(),
                outcome.files.len() + outcome.failures.len(),
                outcome.pages(),
                outcome.seconds
            );
            if let Some(folder) = &outcome.destination {
                println!("Wrote results to {}", folder.display());
            }
            if !outcome.failures.is_empty() {
                println!(
                    "{} file(s) could not be parsed; see messages above",
                    outcome.failures.len()
                );
            }
        } else if let Some(file) = outcome.files.first() {
            println!(
                "Parsed {} pages ({} words) in {:.0} ms with no model calls",
                file.pages, file.words, file.total_ms
            );
            println!("Elements: {}", counts(&file.roles));
            println!("Pages: {}", routing_words(&file.routing));
            for path in [&file.markdown, &file.json].into_iter().flatten() {
                println!("Wrote {}", path.display());
            }
        }
    }
    Ok(u8::from(!outcome.failures.is_empty()))
}
