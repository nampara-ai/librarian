use pulldown_cmark::{CodeBlockKind, Event, Options, Parser, Tag};
use serde::{Deserialize, Serialize};

use crate::{BlockKind, Document, DocumentError, Join, SourceSpan, document::validate};

/// Explicit dialect; smart punctuation is editorial work, never a parser side effect.
pub fn markdown_options() -> Options {
    Options::ENABLE_TABLES
        | Options::ENABLE_FOOTNOTES
        | Options::ENABLE_STRIKETHROUGH
        | Options::ENABLE_TASKLISTS
        | Options::ENABLE_MATH
        | Options::ENABLE_YAML_STYLE_METADATA_BLOCKS
}

#[derive(Debug, Default, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct MarkdownStats {
    pub headings: u32,
    pub paragraphs: u32,
    pub tables: u32,
    pub code_blocks: u32,
    pub mermaid_blocks: u32,
    pub equations: u32,
    pub images: u32,
    pub footnotes: u32,
}

/// Counts syntax only. A Mermaid fence is not proof of valid or faithful Mermaid.
pub fn inspect_markdown(markdown: &str) -> MarkdownStats {
    let mut stats = MarkdownStats::default();
    for event in Parser::new_ext(markdown, markdown_options()) {
        match event {
            Event::Start(Tag::Heading { .. }) => stats.headings += 1,
            Event::Start(Tag::Paragraph) => stats.paragraphs += 1,
            Event::Start(Tag::Table(_)) => stats.tables += 1,
            Event::Start(Tag::CodeBlock(kind)) => {
                stats.code_blocks += 1;
                if matches!(kind, CodeBlockKind::Fenced(info) if info.split_whitespace().next() == Some("mermaid"))
                {
                    stats.mermaid_blocks += 1;
                }
            }
            Event::InlineMath(_) | Event::DisplayMath(_) => stats.equations += 1,
            Event::Start(Tag::Image { .. }) => stats.images += 1,
            Event::Start(Tag::FootnoteDefinition(_)) => stats.footnotes += 1,
            _ => {}
        }
    }
    stats
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RenderedBlock {
    pub id: String,
    /// UTF-8 byte offsets in generated Markdown, including this block's syntax.
    pub start: u32,
    pub end: u32,
    pub sources: Vec<SourceSpan>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceDigest {
    pub source: String,
    pub blake3: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RenderedDocument {
    pub markdown: String,
    pub blocks: Vec<RenderedBlock>,
    pub stats: MarkdownStats,
    pub source_digests: Vec<SourceDigest>,
}

pub fn render(document: &Document) -> Result<RenderedDocument, DocumentError> {
    let sources = validate(document)?;
    let mut markdown = String::new();
    let mut blocks = Vec::with_capacity(document.blocks.len());
    for block in &document.blocks {
        let mut content = String::new();
        for fragment in &block.fragments {
            content.push_str(match fragment.join_before {
                Join::None => "",
                Join::Space => " ",
                Join::LineBreak => "\n",
            });
            let span = &fragment.span;
            content
                .push_str(&sources[span.source.as_str()][span.start as usize..span.end as usize]);
        }
        if !markdown.is_empty() {
            if !markdown.ends_with('\n') {
                markdown.push('\n');
            }
            if !markdown.ends_with("\n\n") {
                markdown.push('\n');
            }
        }
        let start = markdown.len() as u32;
        let invalid = |reason| DocumentError::InvalidBlock {
            id: block.id.clone(),
            reason,
        };
        match &block.kind {
            BlockKind::Paragraph => escape_prose(&content, &mut markdown, false),
            BlockKind::Heading { level } => {
                if content.contains(['\n', '\r']) {
                    return Err(invalid("heading contains a line break"));
                }
                markdown.push_str(&"#".repeat(*level as usize));
                markdown.push(' ');
                escape_prose(&content, &mut markdown, true);
            }
            BlockKind::Code { language } => {
                let longest = content.split(|c| c != '`').map(str::len).max().unwrap_or(0);
                let fence = "`".repeat(3.max(longest + 1));
                markdown.push_str(&fence);
                markdown.push_str(language);
                markdown.push('\n');
                markdown.push_str(&content);
                if !content.ends_with('\n') {
                    markdown.push('\n');
                }
                markdown.push_str(&fence);
            }
            BlockKind::DisplayMath => {
                if content.contains("$$") {
                    return Err(invalid("math contains an ambiguous display delimiter"));
                }
                markdown.push_str("$$\n");
                markdown.push_str(&content);
                if !content.ends_with('\n') {
                    markdown.push('\n');
                }
                markdown.push_str("$$");
            }
            BlockKind::Markdown => markdown.push_str(&content),
        }
        blocks.push(RenderedBlock {
            id: block.id.clone(),
            start,
            end: markdown.len() as u32,
            sources: block.fragments.iter().map(|f| f.span.clone()).collect(),
        });
    }
    if !markdown.is_empty() && !markdown.ends_with('\n') {
        markdown.push('\n');
    }
    let stats = inspect_markdown(&markdown);
    Ok(RenderedDocument {
        markdown,
        blocks,
        stats,
        source_digests: document
            .sources
            .iter()
            .map(|source| SourceDigest {
                source: source.id.clone(),
                blake3: blake3::hash(source.text.as_bytes()).to_hex().to_string(),
            })
            .collect(),
    })
}

/// Literal prose cannot become headings, HTML, lists, links, code, or math.
/// Preserve punctuation instead of performing unrequested editorial changes.
fn escape_prose(text: &str, output: &mut String, heading: bool) {
    let normalized = text.replace("\r\n", "\n").replace('\r', "\n");
    for (index, line) in normalized.split('\n').enumerate() {
        if index > 0 {
            output.push('\n');
        }
        let digits = line.bytes().take_while(u8::is_ascii_digit).count();
        let ordered_marker = (1..=9).contains(&digits)
            && matches!(line.as_bytes().get(digits), Some(b'.' | b')'))
            && line
                .as_bytes()
                .get(digits + 1)
                .is_none_or(u8::is_ascii_whitespace);
        let mut leading = true;
        for (position, ch) in line.char_indices() {
            if leading && matches!(ch, ' ' | '\t') {
                output.push_str(if ch == ' ' { "&#32;" } else { "&#9;" });
                continue;
            }
            leading = false;
            match ch {
                '&' => output.push_str("&amp;"),
                '\\' | '`' | '*' | '_' | '[' | ']' | '<' | '>' | '|' | '$' | '~' => {
                    output.push('\\');
                    output.push(ch);
                }
                '#' if heading || position == 0 => {
                    output.push('\\');
                    output.push(ch);
                }
                '+' | '-' | '=' if position == 0 => {
                    output.push('\\');
                    output.push(ch);
                }
                '.' | ')' if ordered_marker && position == digits => {
                    output.push('\\');
                    output.push(ch);
                }
                _ => output.push(ch),
            }
        }
    }
}
