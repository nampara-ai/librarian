use super::*;
use pulldown_cmark::{CodeBlockKind, Event, Parser, Tag};

/// Syntax diagnostics are evidence for the editor, not an archival rejection
/// gate. No untrusted code, shell command, TeX or URL is executed here.
pub fn inspect(text: &str) -> Vec<contract::Finding> {
    let mut findings = Vec::new();
    let mut add = |kind: &str, description: String| {
        findings.push(contract::Finding {
            kind: kind.into(),
            description,
            evidence_ids: vec![],
        })
    };
    let mut fence: Option<(char, usize)> = None;
    for line in text.lines() {
        let line = line.trim_start();
        let ch = line.chars().next().unwrap_or(' ');
        let n = line.chars().take_while(|c| *c == ch).count();
        if matches!(ch, '`' | '~') && n >= 3 {
            match fence {
                None => fence = Some((ch, n)),
                Some((c, k)) if c == ch && n >= k && line[n..].trim().is_empty() => fence = None,
                _ => {}
            }
        }
    }
    if fence.is_some() {
        add("markdown_fence","An opening code fence has no closing fence in this passage; check continuation context.".into());
    }
    let mut language = String::new();
    let mut body = String::new();
    for event in Parser::new_ext(text, librarian_core::markdown_options()) {
        match event {
            Event::Start(Tag::CodeBlock(kind)) => {
                language = match kind {
                    CodeBlockKind::Fenced(l) => l
                        .split_whitespace()
                        .next()
                        .unwrap_or("")
                        .to_ascii_lowercase(),
                    _ => String::new(),
                };
                body.clear();
            }
            Event::Text(t) if !language.is_empty() => body.push_str(&t),
            Event::End(pulldown_cmark::TagEnd::CodeBlock) => {
                let result: Result<()> = match language.as_str() {
                    "json" => serde_json::from_str::<Value>(&body)
                        .map(|_| ())
                        .map_err(Into::into),
                    "yaml" | "yml" => serde_yaml::from_str::<serde_yaml::Value>(&body)
                        .map(|_| ())
                        .map_err(Into::into),
                    "toml" => toml::from_str::<toml::Value>(&body)
                        .map(|_| ())
                        .map_err(Into::into),
                    "xml" => roxmltree::Document::parse(&body)
                        .map(|_| ())
                        .map_err(Into::into),
                    "csv" | "tsv" => {
                        let mut reader = csv::ReaderBuilder::new()
                            .has_headers(false)
                            .flexible(false)
                            .delimiter(if language == "tsv" { b'\t' } else { b',' })
                            .from_reader(body.as_bytes());
                        reader
                            .records()
                            .try_for_each(|r| r.map(|_| ()).map_err(Into::into))
                    }
                    _ => Ok(()),
                };
                if let Err(error) = result {
                    add(
                        "structured_syntax",
                        format!("{language} parse error: {error}"),
                    );
                }
                // No claim of compiling arbitrary Mermaid or TeX: these checks
                // cover delimiters only. Actual semantics remain evidence-based.
                if matches!(language.as_str(), "mermaid" | "tex" | "latex" | "katex")
                    && !balanced(&body)
                {
                    add(
                        "technical_delimiters",
                        format!("Unbalanced delimiters in {language}; confirm against the source."),
                    );
                }
                language.clear();
                body.clear();
            }
            Event::InlineMath(t) | Event::DisplayMath(t) if !balanced(&t) => {
                add(
                    "math_delimiters",
                    "Unbalanced mathematical braces or delimiters; confirm against the source."
                        .into(),
                );
            }
            _ => {}
        }
    }
    findings
}

fn balanced(text: &str) -> bool {
    let mut stack = Vec::new();
    let mut escaped = false;
    let mut quote = None;
    for c in text.chars() {
        if escaped {
            escaped = false;
            continue;
        }
        if c == '\\' {
            escaped = true;
            continue;
        }
        if c == '"' {
            quote = if quote.is_some() { None } else { Some(c) };
            continue;
        }
        if quote.is_some() {
            continue;
        }
        match c {
            '(' | '[' | '{' => stack.push(c),
            ')' | ']' | '}'
                if stack.pop()
                    != Some(match c {
                        ')' => '(',
                        ']' => '[',
                        _ => '{',
                    }) =>
            {
                return false;
            }
            _ => {}
        }
    }
    stack.is_empty() && quote.is_none()
}

pub fn write_report(library: &Library, job: i64) -> Result<()> {
    let folder = pages::work(library, job);
    std::fs::create_dir_all(&folder)?;
    let rows = |sql: &str| -> Result<Vec<Value>> {
        let mut stmt = library.db.prepare(sql)?;
        let values = stmt
            .query_map([job], |r| r.get::<_, String>(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        values
            .into_iter()
            .map(|s| serde_json::from_str(&s).map_err(Into::into))
            .collect()
    };
    let mut stmt=library.db.prepare("SELECT stage,unit,source_id,disposition,reason FROM editorial_coverage WHERE job=? ORDER BY stage,unit,source_id")?;
    let coverage=stmt.query_map([job],|r|Ok(json!({"stage":r.get::<_,String>(0)?,"unit":r.get::<_,String>(1)?,"source_span_id":r.get::<_,String>(2)?,"disposition":r.get::<_,String>(3)?,"reason":r.get::<_,String>(4)?})))?.collect::<rusqlite::Result<Vec<_>>>()?;
    let report = json!({"version":editorial::VERSION,"job":job,"coverage_kind":"Processing/source-span accounting and model assessments; not a proof of semantic claim preservation.","source_coverage":coverage,"interventions":rows("SELECT payload FROM editorial_interventions WHERE job=? ORDER BY id")?,"revisions":rows("SELECT payload FROM editorial_revisions WHERE job=? ORDER BY id")?,"results":rows("SELECT response FROM editorial_results WHERE job=? ORDER BY stage,unit")?});
    let mut tmp = tempfile::NamedTempFile::new_in(&folder)?;
    use std::io::Write;
    tmp.write_all(serde_json::to_string_pretty(&report)?.as_bytes())?;
    tmp.as_file().sync_all()?;
    tmp.persist(folder.join("editorial-report.json"))?;
    Ok(())
}
