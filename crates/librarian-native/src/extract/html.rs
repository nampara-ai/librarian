use super::*;
use scraper::{ElementRef, Html, Selector};
use std::path::PathBuf;

fn inner_text(element: ElementRef<'_>) -> String {
    element
        .text()
        .collect::<Vec<_>>()
        .join("")
        .trim()
        .to_owned()
}
fn inline(node: ego_tree::NodeRef<'_, scraper::Node>) -> String {
    if let Some(text) = node.value().as_text() {
        return text
            .chars()
            .flat_map(|c| {
                if "\\`*_{}[]<>#!|$~".contains(c) {
                    vec!['\\', c]
                } else {
                    vec![c]
                }
            })
            .collect();
    }
    let Some(e) = ElementRef::wrap(node) else {
        return String::new();
    };
    if matches!(
        e.value().name(),
        "script" | "style" | "template" | "noscript"
    ) {
        return String::new();
    }
    let inside = e.children().map(inline).collect::<String>();
    match e.value().name() {
        "ul" | "ol" => format!("\n{}\n", inside.trim()),
        "li" => {
            let parent = e.parent().and_then(ElementRef::wrap);
            let ordered = parent.is_some_and(|p| p.value().name() == "ol");
            let start = parent
                .and_then(|p| p.value().attr("start"))
                .and_then(|s| s.parse::<usize>().ok())
                .unwrap_or(1);
            let position = e
                .prev_siblings()
                .filter_map(ElementRef::wrap)
                .filter(|n| n.value().name() == "li")
                .count();
            let marker = if ordered {
                format!("{}.", start + position)
            } else {
                "-".into()
            };
            format!("\n{marker} {}", inside.trim().replace('\n', "\n    "))
        }
        "p" | "div" => format!("\n\n{}\n\n", inside.trim()),
        "math" => {
            if let Some(annotation) = e
                .select(&Selector::parse("annotation[encoding='application/x-tex']").unwrap())
                .next()
            {
                format!("${}$", inner_text(annotation))
            } else {
                inside
            }
        }
        "strong" | "b" => format!("**{inside}**"),
        "em" | "i" => format!("*{inside}*"),
        "del" | "s" => format!("~~{inside}~~"),
        "br" => "\n".into(),
        "code" => {
            let raw = inner_text(e);
            let fence = "`".repeat(1 + raw.split(|c| c != '`').map(str::len).max().unwrap_or(0));
            format!("{fence} {raw} {fence}")
        }
        "a" => {
            let href = e.value().attr("href").unwrap_or("");
            if href.starts_with(['#', '/'])
                || href.starts_with("https://")
                || href.starts_with("http://")
                || href.starts_with("mailto:")
            {
                format!(
                    "[{inside}](<{}>)",
                    href.replace('>', "%3E").replace('<', "%3C")
                )
            } else {
                inside
            }
        }
        _ => inside,
    }
}

pub(super) fn extract(
    library: &Library,
    job: i64,
    document: &str,
    path: &Path,
    config: &Config,
) -> Result<()> {
    let source_path = PathBuf::from(library.job(job)?.source);
    let source_folder = source_path
        .parent()
        .unwrap_or(Path::new("."))
        .canonicalize()?;
    let source = bounded_read(path, 32 * 1024 * 1024)?;
    let doc = Html::parse_document(&source);
    let root = doc
        .select(&Selector::parse("body").unwrap())
        .next()
        .unwrap_or_else(|| doc.root_element());
    let mut ordinal = 0;
    let mut consumed = std::collections::BTreeSet::new();
    for node in root.descendants().skip(1) {
        let is_image = ElementRef::wrap(node).is_some_and(|e| e.value().name() == "img");
        if node.ancestors().any(|a| {
            (consumed.contains(&a.id())
                && !(is_image
                    && ElementRef::wrap(a)
                        .is_some_and(|e| !matches!(e.value().name(), "figure" | "table"))))
                || ElementRef::wrap(a).is_some_and(|a| {
                    matches!(
                        a.value().name(),
                        "script" | "style" | "template" | "noscript"
                    )
                })
        }) {
            continue;
        }
        let Some(e) = ElementRef::wrap(node) else {
            if let Some(text) = node
                .value()
                .as_text()
                .map(|t| t.trim())
                .filter(|t| !t.is_empty())
            {
                let id = format!("html-{ordinal}");
                persist(
                    library,
                    job,
                    document,
                    ordinal,
                    &Observation {
                        page: 0,
                        method: "html-text".into(),
                        raw_text: text.into(),
                        blocks: vec![segment(
                            &id,
                            0,
                            0,
                            text.len(),
                            Content::Prose { text: text.into() },
                        )],
                        visuals: vec![],
                        findings: vec![],
                    },
                )?;
                ordinal += 1;
            }
            continue;
        };
        let name = e.value().name();
        if !matches!(
            name,
            "h1" | "h2"
                | "h3"
                | "h4"
                | "h5"
                | "h6"
                | "p"
                | "li"
                | "pre"
                | "table"
                | "img"
                | "math"
                | "blockquote"
                | "figure"
                | "ul"
                | "ol"
        ) {
            continue;
        }
        let raw = inner_text(e);
        let id = format!("html-{ordinal}");
        let mut blocks = Vec::new();
        let mut visuals = Vec::new();
        let mut findings = Vec::new();
        let content = match name {
            "h1" | "h2" | "h3" | "h4" | "h5" | "h6" => Some(Content::Heading {
                level: name.as_bytes()[1] - b'0',
                text: raw.clone(),
            }),
            "p" | "li" | "ul" | "ol" | "blockquote" => {
                let mut md = if matches!(name, "li" | "ul" | "ol") {
                    inline(*e)
                } else {
                    e.children().map(inline).collect::<String>()
                };
                if name == "blockquote" {
                    md = format!("> {}", md.replace('\n', "\n> "));
                }
                Some(Content::MarkdownProse { text: md })
            }
            "pre" => {
                let code = e.select(&Selector::parse("code").unwrap()).next();
                let language = code
                    .and_then(|c| c.value().attr("class"))
                    .and_then(|c| {
                        c.split_whitespace()
                            .find_map(|s| s.strip_prefix("language-"))
                    })
                    .unwrap_or("")
                    .chars()
                    .filter(|c| c.is_ascii_alphanumeric() || "_+.-".contains(*c))
                    .collect();
                Some(Content::Code {
                    language,
                    text: e.text().collect::<String>(),
                })
            }
            "table" => {
                let mut cells = Vec::new();
                let mut occupied = std::collections::BTreeSet::new();
                let mut rows = 0;
                let mut columns = 0;
                for row in e.select(&Selector::parse("tr").unwrap()).filter(|r| {
                    r.ancestors()
                        .filter_map(ElementRef::wrap)
                        .find(|n| n.value().name() == "table")
                        .is_some_and(|t| t.id() == e.id())
                }) {
                    let mut col = 0;
                    for cell in row
                        .children()
                        .filter_map(ElementRef::wrap)
                        .filter(|c| matches!(c.value().name(), "td" | "th"))
                    {
                        while occupied.contains(&(rows, col)) {
                            col += 1;
                        }
                        let rs = cell
                            .value()
                            .attr("rowspan")
                            .and_then(|s| s.parse::<u32>().ok())
                            .unwrap_or(1)
                            .clamp(1, 10000);
                        let cs = cell
                            .value()
                            .attr("colspan")
                            .and_then(|s| s.parse::<u32>().ok())
                            .unwrap_or(1)
                            .clamp(1, 1000);
                        if u64::from(rows + rs) * u64::from(col + cs) > 1_000_000 {
                            bail!("HTML table exceeds cell limit");
                        }
                        for r in rows..rows + rs {
                            for c in col..col + cs {
                                occupied.insert((r, c));
                            }
                        }
                        cells.push(Cell {
                            row: rows,
                            column: col,
                            row_span: rs,
                            column_span: cs,
                            header: cell.value().name() == "th",
                            text: inner_text(cell),
                        });
                        col += cs;
                    }
                    columns = columns.max(col);
                    rows += 1;
                }
                rows = rows.max(cells.iter().map(|c| c.row + c.row_span).max().unwrap_or(0));
                columns = columns.max(
                    cells
                        .iter()
                        .map(|c| c.column + c.column_span)
                        .max()
                        .unwrap_or(0),
                );
                // Ragged HTML rows have explicit empty cells; preserve that structure.
                for r in 0..rows {
                    for c in 0..columns {
                        if !occupied.contains(&(r, c)) {
                            cells.push(Cell {
                                row: r,
                                column: c,
                                row_span: 1,
                                column_span: 1,
                                header: false,
                                text: String::new(),
                            });
                        }
                    }
                }
                let caption = e
                    .select(&Selector::parse("caption").unwrap())
                    .next()
                    .map(inner_text)
                    .unwrap_or_default();
                if rows == 0 || columns == 0 {
                    None
                } else {
                    Some(Content::Table {
                        rows,
                        columns,
                        cells,
                        caption,
                    })
                }
            }
            "math" => {
                let tex = e
                    .select(&Selector::parse("annotation[encoding='application/x-tex']").unwrap())
                    .next()
                    .map(inner_text);
                if let Some(tex) = tex {
                    Some(Content::Equation { tex })
                } else {
                    findings.push(
                        "MathML retained as source text; symbolic conversion requires review"
                            .into(),
                    );
                    Some(Content::Prose { text: raw.clone() })
                }
            }
            "img" | "figure" => {
                let img = if name == "img" {
                    Some(e)
                } else {
                    e.select(&Selector::parse("img").unwrap()).next()
                };
                if let Some(img) = img {
                    let src = img
                        .value()
                        .attr("src")
                        .or_else(|| img.value().attr("data-src"))
                        .unwrap_or("");
                    let alt = img.value().attr("alt").unwrap_or("").to_owned();
                    let caption = e
                        .select(&Selector::parse("figcaption").unwrap())
                        .next()
                        .map(inner_text)
                        .unwrap_or_default();
                    let candidate = source_folder.join(src);
                    let local_root = source_folder.clone();
                    if !src.contains(':')
                        && candidate
                            .canonicalize()
                            .is_ok_and(|p| p.starts_with(&local_root) && p.is_file())
                    {
                        let image = image::open(candidate)?;
                        let asset = save_image(library, &image)?;
                        visuals.push(Visual {
                            asset: asset.clone(),
                            purpose: "figure".into(),
                            replace_page: false,
                            after_block: 0,
                        });
                        Some(Content::Figure {
                            asset: format!("assets/{asset}"),
                            alt,
                            caption,
                            description: String::new(),
                        })
                    } else {
                        findings.push(format!("Image is external or unavailable locally: {src}"));
                        Some(Content::Prose {
                            text: format!("[Image: {alt}] {caption}"),
                        })
                    }
                } else {
                    Some(Content::Prose { text: raw.clone() })
                }
            }
            _ => None,
        };
        if let Some(content) = content
            && (!content.searchable_text().is_empty() || matches!(content, Content::Figure { .. }))
        {
            blocks.push(segment(&id, 0, 0, raw.len(), content));
        }
        consumed.insert(e.id());
        if blocks.is_empty() && visuals.is_empty() {
            continue;
        }
        for b in &mut blocks {
            b.findings.extend(findings.clone());
        }
        persist(
            library,
            job,
            document,
            ordinal,
            &Observation {
                page: 0,
                method: "html-dom".into(),
                raw_text: raw,
                blocks,
                visuals,
                findings,
            },
        )?;
        ordinal += 1;
    }
    if ordinal == 0 {
        let text = inner_text(root);
        if text.is_empty() {
            bail!("HTML contains no readable content");
        }
        persist(
            library,
            job,
            document,
            0,
            &Observation {
                page: 0,
                method: "html-text-fallback".into(),
                blocks: plain_blocks(&text, "html-body", 0, config.processing.chunk_bytes),
                raw_text: text,
                visuals: vec![],
                findings: vec!["HTML structure required a whole-body text fallback".into()],
            },
        )?;
    }
    Ok(())
}
