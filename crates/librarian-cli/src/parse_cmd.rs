//! `librarian parse`: deterministic PDF parsing for one file or a folder.
use anyhow::{Context, Result, bail};
use librarian_core::layout::{self, RenderOptions};
use librarian_native::{parse, write_new_file, write_new_markdown};
use std::{
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
}

fn collect(folder: &Path, recursive: bool, found: &mut Vec<PathBuf>) -> Result<()> {
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

fn summarize(source: &Path, parsed: &parse::Parsed, total_ms: f64) -> serde_json::Value {
    let stats = &parsed.document.stats;
    serde_json::json!({
        "source": source.display().to_string(),
        "pages": stats.pages,
        "words": stats.words,
        "elements": stats.elements,
        "roles": stats.roles,
        "routing": stats.tiers,
        "hyphens": {"joined": stats.hyphens_removed, "kept": stats.hyphens_kept},
        "model_calls": 0,
        "timing_ms": {
            "open": parsed.timing.open_ms,
            "extract": parsed.timing.extract_ms,
            "layout": parsed.timing.layout_ms,
            "total": total_ms,
        },
    })
}

fn counts(value: &serde_json::Value) -> String {
    value
        .as_object()
        .into_iter()
        .flatten()
        .map(|(k, v)| format!("{v} {}", k.replace('_', " ")))
        .collect::<Vec<_>>()
        .join(", ")
}

/// Write `<stem>.md` and `<stem>.layout.json` without replacing anything.
fn publish(
    folder: &Path,
    source: &Path,
    parsed: parse::Parsed,
    markdown: &str,
    summary: &serde_json::Value,
    words: bool,
) -> Result<(PathBuf, PathBuf)> {
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
    let mut body = serde_json::json!({ "summary": summary });
    if words {
        body["pages"] = serde_json::to_value(&parsed.input.pages)?;
        body["fonts"] = serde_json::to_value(&parsed.input.fonts)?;
        body["outline"] = serde_json::to_value(&parsed.input.outline)?;
        body["document"] = serde_json::to_value(&parsed.document)?;
    } else {
        body["document"] = serde_json::to_value(parsed.document.without_word_refs())?;
    }
    write_new_file(&json_path, &serde_json::to_vec_pretty(&body)?)?;
    write_new_markdown(&md_path, markdown)?;
    Ok((md_path, json_path))
}

pub fn run(request: Request<'_>) -> Result<u8> {
    let engine = parse::pdfium(request.root, request.pdfium)?;
    if let Some(folder) = request.output
        && !folder.is_dir()
    {
        if request.create_output {
            std::fs::create_dir_all(folder)?;
        } else {
            bail!(
                "Output folder {} does not exist; pass --create-output to create it",
                folder.display()
            );
        }
    }
    if !request.source.is_dir() {
        let started = Instant::now();
        let parsed = parse::parse(&engine, request.source)?;
        let markdown = layout::markdown(&parsed.document, request.options);
        let summary = summarize(
            request.source,
            &parsed,
            started.elapsed().as_secs_f64() * 1e3,
        );
        let Some(folder) = request.output else {
            io::stdout().lock().write_all(markdown.as_bytes())?;
            eprintln!("{}", serde_json::to_string(&summary)?);
            return Ok(0);
        };
        let (md, json) = publish(
            folder,
            request.source,
            parsed,
            &markdown,
            &summary,
            request.words,
        )?;
        if request.json {
            println!("{}", serde_json::to_string(&summary)?);
        } else {
            println!(
                "Parsed {} pages ({} words) in {:.0} ms with no model calls",
                summary["pages"], summary["words"], summary["timing_ms"]["total"]
            );
            println!("Elements: {}", counts(&summary["roles"]));
            println!("Page routing: {}", counts(&summary["routing"]));
            println!("Wrote {}", md.display());
            println!("Wrote {}", json.display());
        }
        return Ok(0);
    }

    // A folder: every PDF is parsed; one bad file does not stop the rest.
    let folder = request
        .output
        .context("Parsing a folder needs an output folder: pass --output")?;
    let mut sources = Vec::new();
    collect(request.source, request.recursive, &mut sources)?;
    if sources.is_empty() {
        bail!("No PDF files found in {}", request.source.display());
    }
    let started = Instant::now();
    let (mut done, mut pages) = (0usize, 0u64);
    let mut failures: Vec<(PathBuf, String)> = Vec::new();
    for source in &sources {
        let begun = Instant::now();
        let result = parse::parse(&engine, source).and_then(|parsed| {
            let markdown = layout::markdown(&parsed.document, request.options);
            let summary = summarize(source, &parsed, begun.elapsed().as_secs_f64() * 1e3);
            let count = parsed.document.stats.pages as u64;
            publish(folder, source, parsed, &markdown, &summary, request.words)?;
            Ok((count, summary))
        });
        match result {
            Ok((count, summary)) => {
                done += 1;
                pages += count;
                if request.json {
                    println!("{}", serde_json::to_string(&summary)?);
                }
            }
            Err(error) => {
                if request.json {
                    println!(
                        "{}",
                        serde_json::json!({"source": source.display().to_string(), "error": format!("{error:#}")})
                    );
                } else {
                    eprintln!("Failed {}: {error:#}", source.display());
                }
                failures.push((source.clone(), format!("{error:#}")));
            }
        }
    }
    if !request.json {
        println!(
            "Parsed {done} of {} files ({pages} pages) in {:.1} s with no model calls",
            sources.len(),
            started.elapsed().as_secs_f64()
        );
        println!("Wrote results to {}", folder.display());
        if !failures.is_empty() {
            println!(
                "{} file(s) could not be parsed; see messages above",
                failures.len()
            );
        }
    }
    Ok(u8::from(!failures.is_empty()))
}
