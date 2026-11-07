//! Portable corpus rules. Hosts supply observations, storage and inference.
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Evidence {
    pub observation: String,
    pub page: u32,
    pub start: u32,
    pub end: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Cell {
    pub row: u32,
    pub column: u32,
    pub row_span: u32,
    pub column_span: u32,
    pub header: bool,
    pub text: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Node {
    pub id: String,
    pub label: String,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Edge {
    pub from: String,
    pub to: String,
    pub label: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum Content {
    Prose {
        text: String,
    },
    Heading {
        level: u8,
        text: String,
    },
    Markdown {
        text: String,
    },
    MarkdownProse {
        text: String,
    },
    ListItem {
        ordered: bool,
        number: u32,
        level: u8,
        text: String,
    },
    Code {
        language: String,
        text: String,
    },
    Equation {
        tex: String,
    },
    Table {
        rows: u32,
        columns: u32,
        cells: Vec<Cell>,
        caption: String,
    },
    Figure {
        asset: String,
        alt: String,
        caption: String,
        description: String,
    },
    Flowchart {
        nodes: Vec<Node>,
        edges: Vec<Edge>,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Segment {
    pub id: String,
    pub content: Content,
    pub evidence: Vec<Evidence>,
    #[serde(default)]
    pub findings: Vec<String>,
    /// A continued logical paragraph is exported with a space, not a blank line.
    #[serde(default)]
    pub continuation: bool,
    /// A transport boundary inside a word must not insert a space.
    #[serde(default)]
    pub tight_join: bool,
    /// Range within the immutable logical parent when a large block is split.
    #[serde(default)]
    pub logical_part: Option<LogicalPart>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct LogicalPart {
    pub parent: String,
    pub start: u32,
    pub end: u32,
}

#[derive(Debug, Default, Clone, Serialize, Deserialize, PartialEq)]
#[serde(default, deny_unknown_fields)]
pub struct Metadata {
    pub title: String,
    pub summary: String,
    pub tags: Vec<String>,
    pub class_code: String,
    pub taxonomy: String,
}

impl Content {
    pub fn editable_text(&self) -> Option<&str> {
        match self {
            Self::Prose { text }
            | Self::Heading { text, .. }
            | Self::MarkdownProse { text }
            | Self::ListItem { text, .. } => Some(text),
            _ => None,
        }
    }
    pub fn with_edit(&self, text: String) -> Self {
        match self {
            Self::Prose { .. } => Self::Prose { text },
            Self::Heading { level, .. } => Self::Heading {
                level: *level,
                text,
            },
            Self::MarkdownProse { .. } => Self::MarkdownProse { text },
            Self::ListItem {
                ordered,
                number,
                level,
                ..
            } => Self::ListItem {
                ordered: *ordered,
                number: *number,
                level: *level,
                text,
            },
            _ => self.clone(),
        }
    }
    pub fn searchable_text(&self) -> String {
        match self {
            Self::Prose { text }
            | Self::Heading { text, .. }
            | Self::Markdown { text }
            | Self::MarkdownProse { text }
            | Self::ListItem { text, .. }
            | Self::Code { text, .. } => text.clone(),
            Self::Equation { tex } => tex.clone(),
            Self::Table { cells, caption, .. } => format!(
                "{} {}",
                caption,
                cells
                    .iter()
                    .map(|c| c.text.as_str())
                    .collect::<Vec<_>>()
                    .join(" ")
            ),
            Self::Figure {
                alt,
                caption,
                description,
                ..
            } => format!("{alt} {caption} {description}"),
            Self::Flowchart { nodes, edges } => nodes
                .iter()
                .map(|n| n.label.as_str())
                .chain(edges.iter().map(|e| e.label.as_str()))
                .collect::<Vec<_>>()
                .join(" "),
        }
    }
}

fn literal(text: &str, kind: crate::BlockKind) -> Result<String, String> {
    if text.is_empty() {
        return Ok(String::new());
    }
    let doc = crate::Document {
        sources: vec![crate::SourceText {
            id: "source".into(),
            text: text.into(),
        }],
        blocks: vec![crate::Block {
            id: "block".into(),
            kind,
            fragments: vec![crate::Fragment {
                span: crate::SourceSpan {
                    source: "source".into(),
                    start: 0,
                    end: text.len() as u32,
                },
                join_before: crate::Join::None,
            }],
        }],
        exclusions: vec![],
    };
    crate::render(&doc)
        .map(|r| r.markdown)
        .map_err(|e| e.to_string())
}

fn html(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\n', "<br>")
}

pub fn render_content(content: &Content) -> Result<String, String> {
    match content {
        Content::Prose { text } => literal(text, crate::BlockKind::Paragraph),
        Content::Heading { level, text } => {
            literal(text, crate::BlockKind::Heading { level: *level })
        }
        Content::Markdown { text } | Content::MarkdownProse { text } => Ok(text.clone()),
        Content::ListItem {
            ordered,
            number,
            level,
            text,
        } => {
            let marker = if *ordered {
                format!("{number}.")
            } else {
                "-".into()
            };
            let body = literal(text, crate::BlockKind::Paragraph)?;
            let indent = "    ".repeat((*level).min(8) as usize);
            Ok(format!(
                "{indent}{marker} {}",
                body.replace('\n', &format!("\n{indent}    ")).trim_end()
            ))
        }
        Content::Code { language, text } => literal(
            text,
            crate::BlockKind::Code {
                language: language.clone(),
            },
        ),
        Content::Equation { tex } => literal(tex, crate::BlockKind::DisplayMath),
        Content::Figure {
            asset,
            alt,
            caption,
            description,
        } => {
            if asset.contains(['\\', '\n', '\r', '(', ')', '<', '>'])
                || asset.starts_with('/')
                || asset.split('/').any(|s| s == ".." || s.contains(':'))
            {
                return Err("Image asset must be a safe relative path".into());
            }
            let alt = alt
                .replace(['\n', '\r'], " ")
                .replace('\\', "\\\\")
                .replace('[', "\\[")
                .replace(']', "\\]");
            let mut result = format!("![{alt}]({})\n", asset.replace(' ', "%20"));
            if !caption.is_empty() {
                result.push_str(&format!(
                    "\n{}",
                    literal(caption, crate::BlockKind::Paragraph)?
                ));
            }
            if !description.is_empty() {
                result.push_str(&format!(
                    "\n**Generated description:** {}",
                    literal(description, crate::BlockKind::Paragraph)?
                ));
            }
            Ok(result)
        }
        Content::Table {
            rows,
            columns,
            cells,
            caption,
        } => {
            if *rows == 0 || *columns == 0 || u64::from(*rows) * u64::from(*columns) > 1_000_000 {
                return Err("Invalid or oversized table dimensions".into());
            }
            let mut occupied = BTreeSet::new();
            for cell in cells {
                let end_row = cell
                    .row
                    .checked_add(cell.row_span)
                    .ok_or("Table row overflow")?;
                let end_col = cell
                    .column
                    .checked_add(cell.column_span)
                    .ok_or("Table column overflow")?;
                if cell.row_span == 0
                    || cell.column_span == 0
                    || end_row > *rows
                    || end_col > *columns
                {
                    return Err("Cell outside table dimensions".into());
                }
                for row in cell.row..end_row {
                    for col in cell.column..end_col {
                        if !occupied.insert((row, col)) {
                            return Err("Overlapping table cells".into());
                        }
                    }
                }
            }
            if occupied.len() as u64 != u64::from(*rows) * u64::from(*columns) {
                return Err("Table has unaccounted cells".into());
            }
            let grid: BTreeMap<_, _> = cells.iter().map(|c| ((c.row, c.column), c)).collect();
            let simple = cells.iter().all(|c| {
                c.row_span == 1
                    && c.column_span == 1
                    && c.header == (c.row == 0)
                    && !c.text.contains('\n')
            });
            let mut result = String::new();
            if !caption.is_empty() {
                result.push_str(&literal(caption, crate::BlockKind::Paragraph)?);
                result.push('\n');
            }
            if simple {
                for row in 0..*rows {
                    result.push('|');
                    for col in 0..*columns {
                        result.push_str(&format!(
                            " {} |",
                            literal(&grid[&(row, col)].text, crate::BlockKind::Paragraph)?
                                .trim_end_matches('\n')
                        ));
                    }
                    result.push('\n');
                    if row == 0 {
                        result.push('|');
                        for _ in 0..*columns {
                            result.push_str(" --- |");
                        }
                        result.push('\n');
                    }
                }
            } else {
                result.push_str("<table>\n");
                for row in 0..*rows {
                    result.push_str("<tr>");
                    for col in 0..*columns {
                        if let Some(c) = grid.get(&(row, col)) {
                            let tag = if c.header { "th" } else { "td" };
                            result.push_str(&format!(
                                "<{tag} rowspan=\"{}\" colspan=\"{}\">{}</{tag}>",
                                c.row_span,
                                c.column_span,
                                html(&c.text)
                            ));
                        }
                    }
                    result.push_str("</tr>\n");
                }
                result.push_str("</table>\n");
            }
            Ok(result)
        }
        Content::Flowchart { nodes, edges } => {
            if nodes.is_empty() || nodes.len() > 1000 || edges.len() > 5000 {
                return Err("Invalid flowchart size".into());
            }
            let mut ids = BTreeMap::new();
            let label = |s: &str| {
                s.replace('&', "#38;")
                    .replace('"', "#quot;")
                    .replace('<', "#60;")
                    .replace('>', "#62;")
                    .replace(['\n', '\r'], " ")
            };
            let mut source = "flowchart TD\n".to_owned();
            for (i, node) in nodes.iter().enumerate() {
                if node.id.is_empty() || ids.insert(&node.id, i).is_some() {
                    return Err("Duplicate or empty diagram node".into());
                }
                source.push_str(&format!("  n{i}[\"{}\"]\n", label(&node.label)));
            }
            for edge in edges {
                let from = ids.get(&edge.from).ok_or("Unknown edge source")?;
                let to = ids.get(&edge.to).ok_or("Unknown edge destination")?;
                if edge.label.is_empty() {
                    source.push_str(&format!("  n{from} --> n{to}\n"));
                } else {
                    source.push_str(&format!(
                        "  n{from} -->|\"{}\"| n{to}\n",
                        label(&edge.label)
                    ));
                }
            }
            literal(
                &source,
                crate::BlockKind::Code {
                    language: "mermaid".into(),
                },
            )
        }
    }
}

pub fn separator(previous: &Content, next: &Segment) -> &'static str {
    if next.continuation {
        if next.tight_join { "" } else { " " }
    } else if matches!(previous, Content::ListItem { .. })
        && matches!(next.content, Content::ListItem { .. })
    {
        "\n"
    } else {
        "\n\n"
    }
}

/// Portable body renderer shared with browser/WASM hosts. Metadata is a host policy.
pub fn render_segments(segments: &[Segment]) -> Result<String, String> {
    let mut result = String::new();
    let mut ids = BTreeSet::new();
    for (i, segment) in segments.iter().enumerate() {
        if !ids.insert(&segment.id) {
            return Err("Duplicate segment identity".into());
        }
        if segment.evidence.iter().any(|e| e.start > e.end) {
            return Err("Invalid source evidence range".into());
        }
        if i > 0 {
            result.push_str(separator(&segments[i - 1].content, segment));
        }
        result.push_str(render_content(&segment.content)?.trim_end_matches('\n'));
    }
    if !result.is_empty() {
        result.push('\n');
    }
    Ok(result)
}

/// Bounded source slices. No continuation is ever synthesized from generated text.
pub fn split_text(text: &str, limit: usize) -> Vec<(usize, usize)> {
    assert!(limit >= 4, "a UTF-8 scalar needs up to four bytes");
    let mut result = Vec::new();
    let mut start = 0;
    while start < text.len() {
        let mut end = start.saturating_add(limit).min(text.len());
        while !text.is_char_boundary(end) {
            end -= 1;
        }
        if end < text.len()
            && let Some((cut, ch)) = text[start..end]
                .char_indices()
                .rev()
                .find(|(i, c)| *i >= limit / 2 && c.is_whitespace())
        {
            end = start + cut + ch.len_utf8();
        }
        result.push((start, end));
        start = end;
    }
    result
}

pub fn edit_findings(source: &str, candidate: &str) -> Vec<String> {
    let mut findings = Vec::new();
    if candidate.trim().is_empty() {
        findings.push("empty_edit".into());
        return findings;
    }
    fn numbers(s: &str) -> Vec<String> {
        let mut values = Vec::new();
        let chars: Vec<char> = s.chars().collect();
        let mut i = 0;
        while i < chars.len() {
            let start = i;
            if chars[i].is_numeric()
                || ("-+−".contains(chars[i]) && chars.get(i + 1).is_some_and(|c| c.is_numeric()))
            {
                i += 1;
                while i < chars.len()
                    && (chars[i].is_numeric()
                        || (".,:/eE-+−".contains(chars[i])
                            && chars.get(i + 1).is_some_and(|c| c.is_numeric())))
                {
                    i += 1;
                }
                values.push(chars[start..i].iter().collect());
            } else {
                i += 1;
            }
        }
        values
    }
    if numbers(source) != numbers(candidate) {
        findings.push("changed_numeric_values".into());
    }
    let before = source.split_whitespace().count().max(1);
    let after = candidate.split_whitespace().count();
    if after * 100 < before * 65 || after * 100 > before * 150 {
        findings.push("large_length_change".into());
    }
    for quote in source.split('"').skip(1).step_by(2) {
        if quote.len() > 4 && !candidate.contains(quote) {
            findings.push("changed_quotation".into());
        }
    }
    if candidate.contains("```") && !source.contains("```") {
        findings.push("unexpected_code_fence".into());
    }
    findings.sort();
    findings.dedup();
    findings
}

/// Conservative joining: ordinary lower-case continuation, never across protected content.
pub fn can_continue(left: &Segment, right: &Segment) -> bool {
    let (Content::Prose { text: a }, Content::Prose { text: b }) = (&left.content, &right.content)
    else {
        return false;
    };
    let a = a.trim_end();
    let b = b.trim_start();
    !a.is_empty()
        && b.chars().next().is_some_and(char::is_alphanumeric)
        && !a.ends_with(['.', '!', '?', ':', ';', '。', '！', '？'])
        && left
            .evidence
            .last()
            .zip(right.evidence.first())
            .is_some_and(|(l, r)| l.page > 0 && l.page.checked_add(1) == Some(r.page))
}

pub fn join_segments(mut left: Segment, right: Segment) -> Segment {
    if let (Content::Prose { text: a }, Content::Prose { text: b }) =
        (&mut left.content, &right.content)
    {
        let left = a.trim_end();
        *a = if left.ends_with('\u{ad}') {
            format!("{}{}", left.trim_end_matches('\u{ad}'), b.trim_start())
        } else if left.ends_with('-') {
            format!("{left}{}", b.trim_start())
        } else {
            format!("{left} {}", b.trim_start())
        };
    }
    left.evidence.extend(right.evidence);
    left.findings.extend(right.findings);
    left
}

pub fn safe_filename(
    template: &str,
    metadata: &Metadata,
    id: &str,
    accession: u64,
) -> Result<String, String> {
    let placeholders = template
        .replace("{title}", "")
        .replace("{class}", "")
        .replace("{id}", "")
        .replace("{number}", "");
    if placeholders.contains(['{', '}']) {
        return Err("Unknown filename template field".into());
    }
    let mut result = template
        .replace("{title}", &metadata.title)
        .replace(
            "{class}",
            if metadata.class_code.is_empty() {
                "unclassified"
            } else {
                &metadata.class_code
            },
        )
        .replace("{id}", id)
        .replace("{number}", &format!("{accession:06}"));
    result = result
        .chars()
        .map(|c| {
            if c.is_control() || "<>:\"/\\|?*".contains(c) {
                '_'
            } else {
                c
            }
        })
        .collect();
    result = result.trim_matches([' ', '.']).to_owned();
    let mut cut = result.len().min(180);
    while !result.is_char_boundary(cut) {
        cut -= 1;
    }
    result.truncate(cut);
    result = result.trim_end_matches([' ', '.']).to_owned();
    if result.is_empty() {
        result = "untitled".into();
    }
    let stem = result.split('.').next().unwrap_or("").to_ascii_uppercase();
    if matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || (stem.len() == 4
            && (stem.starts_with("COM") || stem.starts_with("LPT"))
            && stem.ends_with(|c: char| c.is_ascii_digit()))
    {
        result.insert(0, '_');
    }
    if !result.ends_with(".md") {
        result.push_str(".md");
    }
    Ok(result)
}
