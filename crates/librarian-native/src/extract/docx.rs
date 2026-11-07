use super::*;
use roxmltree::Node;

fn xml_entry(archive: &mut zip::ZipArchive<File>, name: &str) -> Result<Option<String>> {
    let Ok(file) = archive.by_name(name) else {
        return Ok(None);
    };
    if file.size() > 64 * 1024 * 1024 {
        bail!("DOCX XML part exceeds 64 MiB: {name}");
    }
    let mut text = String::new();
    file.take(64 * 1024 * 1024 + 1).read_to_string(&mut text)?;
    Ok(Some(text))
}
fn attr<'a>(node: Node<'a, 'a>, name: &str) -> Option<&'a str> {
    node.attributes()
        .find(|a| a.name() == name)
        .map(|a| a.value())
}
fn text(node: Node<'_, '_>) -> String {
    let mut s = String::new();
    for n in node.descendants().filter(|n| n.is_element()) {
        match n.tag_name().name() {
            "t" => s.push_str(n.text().unwrap_or("")),
            "tab" => s.push('\t'),
            "br" | "cr" => s.push('\n'),
            "noBreakHyphen" => s.push('‑'),
            _ => {}
        }
    }
    s
}

fn math(node: Node<'_, '_>) -> String {
    let child = |name: &str| {
        node.children()
            .find(|n| n.is_element() && n.tag_name().name() == name)
            .map(math)
            .unwrap_or_default()
    };
    match node.tag_name().name() {
        "t" => node
            .text()
            .unwrap_or("")
            .replace('\\', "\\backslash ")
            .replace('{', "\\{")
            .replace('}', "\\}"),
        "f" => format!("\\frac{{{}}}{{{}}}", child("num"), child("den")),
        "sSup" => format!("{{{}}}^{{{}}}", child("e"), child("sup")),
        "sSub" => format!("{{{}}}_{{{}}}", child("e"), child("sub")),
        "sSubSup" => format!(
            "{{{}}}_{{{}}}^{{{}}}",
            child("e"),
            child("sub"),
            child("sup")
        ),
        "rad" => {
            let d = child("deg");
            if d.is_empty() {
                format!("\\sqrt{{{}}}", child("e"))
            } else {
                format!("\\sqrt[{d}]{{{}}}", child("e"))
            }
        }
        "rPr" | "ctrlPr" | "oMathParaPr" | "fPr" | "radPr" | "sSupPr" | "sSubPr" => String::new(),
        _ => node
            .children()
            .filter(|n| n.is_element())
            .map(math)
            .collect(),
    }
}

fn escaped(value: &str) -> String {
    value
        .chars()
        .flat_map(|c| {
            if "\\`*_{}[]<>#!|$~".contains(c) {
                vec!['\\', c]
            } else {
                vec![c]
            }
        })
        .collect()
}

fn inline(node: Node<'_, '_>, rels: &BTreeMap<String, String>) -> String {
    let children = || {
        node.children()
            .filter(|n| n.is_element())
            .map(|n| inline(n, rels))
            .collect::<String>()
    };
    match node.tag_name().name() {
        "t" => escaped(node.text().unwrap_or("")),
        "tab" => "    ".into(),
        "br" | "cr" => "  \n".into(),
        "noBreakHyphen" => "‑".into(),
        "softHyphen" => "\u{ad}".into(),
        "oMath" => format!("${}$", math(node)),
        "oMathPara" => format!("\n\n$$\n{}\n$$\n\n", math(node)),
        "footnoteReference" | "endnoteReference" => attr(node, "id")
            .map(|id| {
                format!(
                    "[^{}-{id}]",
                    if node.tag_name().name() == "footnoteReference" {
                        "fn"
                    } else {
                        "en"
                    }
                )
            })
            .unwrap_or_default(),
        "rPr" | "pPr" | "drawing" | "pict" | "instrText" | "del" => String::new(),
        "r" => {
            let mut content = children();
            let properties = node.children().find(|n| n.tag_name().name() == "rPr");
            let enabled = |name: &str| {
                properties.is_some_and(|p| {
                    p.children().any(|n| {
                        n.tag_name().name() == name
                            && !matches!(attr(n, "val"), Some("0" | "false" | "off"))
                    })
                })
            };
            if !content.trim().is_empty() {
                let leading = content.len() - content.trim_start().len();
                let trailing = content.trim_end().len();
                let mut body = content[leading..trailing].to_owned();
                if enabled("i") {
                    body = format!("*{body}*");
                }
                if enabled("b") {
                    body = format!("**{body}**");
                }
                if enabled("strike") {
                    body = format!("~~{body}~~");
                }
                content = format!("{}{body}{}", &content[..leading], &content[trailing..]);
            }
            content
        }
        "hyperlink" => {
            let content = children();
            let target = attr(node, "id")
                .and_then(|id| rels.get(id))
                .cloned()
                .or_else(|| attr(node, "anchor").map(|s| format!("#{s}")));
            if let Some(target) = target.filter(|s| {
                s.starts_with("https://")
                    || s.starts_with("http://")
                    || s.starts_with("mailto:")
                    || s.starts_with('#')
            }) {
                format!(
                    "[{content}](<{}>)",
                    target
                        .replace('<', "%3C")
                        .replace('>', "%3E")
                        .replace(['\r', '\n'], "")
                )
            } else {
                content
            }
        }
        _ => children(),
    }
}

fn numbering(xml: &str) -> Result<BTreeMap<(String, u32), (bool, u32)>> {
    let document = roxmltree::Document::parse(xml)?;
    let mut result = BTreeMap::new();
    for number in document
        .descendants()
        .filter(|n| n.tag_name().name() == "num")
    {
        let Some(id) = attr(number, "numId") else {
            continue;
        };
        let abstract_id = number
            .children()
            .find(|n| n.tag_name().name() == "abstractNumId")
            .and_then(|n| attr(n, "val"));
        let definition = document.descendants().find(|n| {
            n.tag_name().name() == "abstractNum" && attr(*n, "abstractNumId") == abstract_id
        });
        for level in definition
            .into_iter()
            .flat_map(|n| n.children())
            .filter(|n| n.tag_name().name() == "lvl")
        {
            let depth = attr(level, "ilvl")
                .and_then(|s| s.parse().ok())
                .unwrap_or(0);
            let ordered = level
                .children()
                .find(|n| n.tag_name().name() == "numFmt")
                .and_then(|n| attr(n, "val"))
                != Some("bullet");
            let start = level
                .children()
                .find(|n| n.tag_name().name() == "start")
                .and_then(|n| attr(n, "val"))
                .and_then(|s| s.parse().ok())
                .unwrap_or(1);
            let override_start = number
                .children()
                .find(|n| {
                    n.tag_name().name() == "lvlOverride"
                        && attr(*n, "ilvl").and_then(|s| s.parse().ok()) == Some(depth)
                })
                .and_then(|n| {
                    n.children()
                        .find(|n| n.tag_name().name() == "startOverride")
                })
                .and_then(|n| attr(n, "val"))
                .and_then(|s| s.parse().ok());
            result.insert(
                (id.to_owned(), depth),
                (ordered, override_start.unwrap_or(start)),
            );
        }
    }
    Ok(result)
}

pub(super) fn extract(
    library: &Library,
    job: i64,
    document: &str,
    path: &Path,
    config: &Config,
) -> Result<()> {
    let mut archive = zip::ZipArchive::new(File::open(path)?).context("Invalid DOCX archive")?;
    let xml =
        xml_entry(&mut archive, "word/document.xml")?.context("DOCX has no word/document.xml")?;
    let rels_xml = xml_entry(&mut archive, "word/_rels/document.xml.rels")?
        .unwrap_or_else(|| "<Relationships/>".into());
    let rels_doc = roxmltree::Document::parse(&rels_xml)?;
    let rels: BTreeMap<String, String> = rels_doc
        .descendants()
        .filter(|n| n.has_tag_name("Relationship"))
        .filter_map(|n| Some((n.attribute("Id")?.into(), n.attribute("Target")?.into())))
        .collect();
    let doc = roxmltree::Document::parse(&xml).context("Invalid DOCX document XML")?;
    let body = doc
        .descendants()
        .find(|n| n.tag_name().name() == "body")
        .context("DOCX body missing")?;
    let mut ordinal = 0u64;
    let numbering = numbering(
        &xml_entry(&mut archive, "word/numbering.xml")?.unwrap_or_else(|| "<numbering/>".into()),
    )?;
    let mut list_counts: BTreeMap<(String, u32), u32> = BTreeMap::new();
    for node in body.children().filter(|n| n.is_element()) {
        if !library.active(job)? {
            bail!("Job paused or cancelled");
        }
        let id = format!("docx-{ordinal}");
        let raw = text(node);
        let mut blocks = Vec::new();
        let mut visuals = Vec::new();
        let mut findings = Vec::new();
        match node.tag_name().name() {
            "p" => {
                let style = node
                    .descendants()
                    .find(|n| n.tag_name().name() == "pStyle")
                    .and_then(|n| attr(n, "val"))
                    .unwrap_or("");
                let mut markdown = inline(node, &rels);
                if !markdown.trim().is_empty() {
                    if let Some(level) = style
                        .strip_prefix("Heading")
                        .or_else(|| style.strip_prefix("heading"))
                        .and_then(|s| s.parse::<u8>().ok())
                        .filter(|n| (1..=6).contains(n))
                    {
                        markdown =
                            format!("{} {}", "#".repeat(usize::from(level)), markdown.trim());
                    } else if let Some(num) = node
                        .descendants()
                        .find(|n| n.tag_name().name() == "numId")
                        .and_then(|n| attr(n, "val"))
                    {
                        let level = node
                            .descendants()
                            .find(|n| n.tag_name().name() == "ilvl")
                            .and_then(|n| attr(n, "val"))
                            .and_then(|s| s.parse().ok())
                            .unwrap_or(0u32);
                        let key = (num.to_owned(), level);
                        let (ordered, start) = numbering.get(&key).copied().unwrap_or((true, 1));
                        list_counts.retain(|(list, depth), _| list != num || *depth <= level);
                        let number = list_counts.entry(key).or_insert(start.saturating_sub(1));
                        *number += 1;
                        let indent = "    ".repeat(level.min(8) as usize);
                        let marker = if ordered {
                            format!("{number}.")
                        } else {
                            "-".into()
                        };
                        markdown = format!(
                            "{indent}{marker} {}",
                            markdown.trim().replace('\n', &format!("\n{indent}    "))
                        );
                    }
                    blocks.push(segment(
                        &id,
                        0,
                        0,
                        raw.len(),
                        Content::MarkdownProse { text: markdown },
                    ));
                }
                for blip in node.descendants().filter(|n| n.tag_name().name() == "blip") {
                    if let Some(target) = attr(blip, "embed").and_then(|key| rels.get(key)) {
                        let entry = if target.starts_with('/') {
                            target.trim_start_matches('/').to_owned()
                        } else {
                            format!("word/{target}")
                        };
                        let mut file = archive
                            .by_name(&entry)
                            .with_context(|| format!("Missing DOCX media {entry}"))?;
                        if file.size() > 64 * 1024 * 1024 {
                            bail!("DOCX media exceeds size limit");
                        }
                        let mut bytes = Vec::new();
                        file.read_to_end(&mut bytes)?;
                        match image::load_from_memory(&bytes) {
                            Ok(image) => {
                                let asset = save_image(library, &image)?;
                                visuals.push(Visual {
                                    asset,
                                    purpose: "figure".into(),
                                    replace_page: false,
                                    after_block: blocks.len(),
                                });
                            }
                            Err(_) => {
                                let extension = Path::new(target)
                                    .extension()
                                    .and_then(|s| s.to_str())
                                    .unwrap_or("bin");
                                save_asset(library, &bytes, extension)?;
                                findings.push(format!("Unsupported embedded image format: {extension}; original media retained"));
                            }
                        }
                    }
                }
            }
            "tbl" => {
                let mut cells = Vec::new();
                let mut rows = 0;
                let mut columns = 0;
                for row in node.children().filter(|n| n.tag_name().name() == "tr") {
                    let mut col = 0;
                    for cell in row.children().filter(|n| n.tag_name().name() == "tc") {
                        let span = cell
                            .descendants()
                            .find(|n| n.tag_name().name() == "gridSpan")
                            .and_then(|n| attr(n, "val"))
                            .and_then(|s| s.parse::<u32>().ok())
                            .unwrap_or(1)
                            .max(1);
                        let vmerge = cell.descendants().find(|n| n.tag_name().name() == "vMerge");
                        if vmerge.is_some_and(|v| attr(v, "val") != Some("restart")) {
                            if let Some(prior) = cells.iter_mut().rev().find(|c: &&mut Cell| {
                                c.column == col
                                    && c.column_span == span
                                    && c.row + c.row_span == rows
                            }) {
                                prior.row_span += 1;
                            } else {
                                findings.push("Unresolved vertical table merge".into());
                                cells.push(Cell {
                                    row: rows,
                                    column: col,
                                    row_span: 1,
                                    column_span: span,
                                    header: false,
                                    text: text(cell),
                                });
                            }
                        } else {
                            cells.push(Cell {
                                row: rows,
                                column: col,
                                row_span: 1,
                                column_span: span,
                                header: row
                                    .descendants()
                                    .any(|n| n.tag_name().name() == "tblHeader"),
                                text: text(cell),
                            });
                        }
                        col += span;
                    }
                    columns = columns.max(col);
                    rows += 1;
                }
                let occupied: std::collections::BTreeSet<_> = cells
                    .iter()
                    .flat_map(|c| {
                        (c.row..c.row + c.row_span).flat_map(move |r| {
                            (c.column..c.column + c.column_span).map(move |col| (r, col))
                        })
                    })
                    .collect();
                if u64::from(rows) * u64::from(columns) > 1_000_000 {
                    bail!("DOCX table exceeds cell limit");
                }
                for r in 0..rows {
                    for col in 0..columns {
                        if !occupied.contains(&(r, col)) {
                            cells.push(Cell {
                                row: r,
                                column: col,
                                row_span: 1,
                                column_span: 1,
                                header: false,
                                text: String::new(),
                            });
                        }
                    }
                }
                blocks.push(segment(
                    &id,
                    0,
                    0,
                    raw.len(),
                    Content::Table {
                        rows,
                        columns,
                        cells,
                        caption: String::new(),
                    },
                ));
            }
            "sectPr" => continue,
            other => {
                if !raw.trim().is_empty() {
                    blocks.extend(plain_blocks(&raw, &id, 0, config.processing.chunk_bytes));
                    findings.push(format!("DOCX structure {other} retained as text"));
                } else {
                    continue;
                }
            }
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
                method: "docx-xml".into(),
                raw_text: raw,
                blocks,
                visuals,
                findings,
            },
        )?;
        ordinal += 1;
    }
    for (entry, prefix) in [("word/footnotes.xml", "fn"), ("word/endnotes.xml", "en")] {
        if let Some(xml) = xml_entry(&mut archive, entry)? {
            let doc = roxmltree::Document::parse(&xml)?;
            for note in doc.root_element().children().filter(|n| n.is_element()) {
                let Some(number) = attr(note, "id") else {
                    continue;
                };
                if number.starts_with('-') || number == "0" {
                    continue;
                }
                let raw = text(note);
                let id = format!("{prefix}-{number}");
                let md = format!("[^{id}]: {}", raw.replace('\n', "\n    "));
                persist(
                    library,
                    job,
                    document,
                    ordinal,
                    &Observation {
                        page: 0,
                        method: "docx-note".into(),
                        raw_text: raw.clone(),
                        blocks: vec![segment(
                            &id,
                            0,
                            0,
                            raw.len(),
                            Content::Markdown { text: md },
                        )],
                        visuals: vec![],
                        findings: vec![],
                    },
                )?;
                ordinal += 1;
            }
        }
    }
    Ok(())
}
