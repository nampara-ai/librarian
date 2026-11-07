use crate::{config::Config, store::Library};
use anyhow::{Context, Result, bail};
use librarian_core::corpus::{Cell, Content, Evidence, Segment, split_text};
use rusqlite::{OptionalExtension, params};
use serde::{Deserialize, Serialize};
use std::{
    collections::BTreeMap,
    fs::File,
    io::{BufReader, Read, Write},
    path::Path,
};

mod docx;
mod html;
mod pdf;
mod structured;

pub const EXTRACT_VERSION: &str = "extract-v2";
pub const WINDOW_BYTES: usize = 256 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Visual {
    pub asset: String,
    pub purpose: String,
    pub replace_page: bool,
    pub after_block: usize,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Observation {
    pub page: u32,
    pub method: String,
    pub raw_text: String,
    pub blocks: Vec<Segment>,
    pub visuals: Vec<Visual>,
    pub findings: Vec<String>,
}

pub fn format(path: &Path) -> String {
    path.extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_ascii_lowercase()
}

pub fn snapshot(
    library: &Library,
    job: i64,
    path: &Path,
    config: &Config,
) -> Result<(String, String, String)> {
    let mut input = File::open(path).with_context(|| format!("Cannot read {}", path.display()))?;
    if !input.metadata()?.is_file() {
        bail!("Source is not a regular file");
    }
    if input.metadata()?.len() > config.processing.max_file_bytes {
        bail!("Input exceeds configured file-size limit");
    }
    let mut temp = tempfile::NamedTempFile::new_in(library.root.join("originals"))?;
    let mut hash = blake3::Hasher::new();
    let mut buffer = vec![0u8; 1024 * 1024];
    let mut total = 0u64;
    loop {
        if !library.active(job)? {
            bail!("Job paused or cancelled");
        }
        let n = input.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        total += n as u64;
        if total > config.processing.max_file_bytes {
            bail!("Input exceeds configured file-size limit");
        }
        hash.update(&buffer[..n]);
        temp.write_all(&buffer[..n])?;
    }
    if total == 0 {
        bail!("Input is empty");
    }
    temp.as_file().sync_all()?;
    let digest = hash.finalize().to_hex().to_string();
    let extension = format(path);
    let extension = if extension.len() <= 16 && extension.bytes().all(|b| b.is_ascii_alphanumeric())
    {
        extension
    } else {
        "bin".into()
    };
    let name = format!("{digest}.{extension}");
    let target = library.root.join("originals").join(&name);
    if !target.exists() {
        match temp.persist_noclobber(&target) {
            Ok(_) => {}
            Err(e) if e.error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(e) => return Err(e.error.into()),
        }
    }
    Ok((digest, name, extension))
}

pub fn save_asset(library: &Library, bytes: &[u8], extension: &str) -> Result<String> {
    let name = format!("{}.{}", blake3::hash(bytes).to_hex(), extension);
    let path = library.root.join("assets").join(&name);
    if !path.exists() {
        let mut temp = tempfile::NamedTempFile::new_in(library.root.join("assets"))?;
        temp.write_all(bytes)?;
        temp.as_file().sync_all()?;
        match temp.persist_noclobber(path) {
            Ok(_) => {}
            Err(e) if e.error.kind() == std::io::ErrorKind::AlreadyExists => {}
            Err(e) => return Err(e.error.into()),
        }
    }
    Ok(name)
}

pub fn save_image(library: &Library, image: &image::DynamicImage) -> Result<String> {
    let mut bytes = std::io::Cursor::new(Vec::new());
    image.write_to(&mut bytes, image::ImageFormat::Png)?;
    save_asset(library, bytes.get_ref(), "png")
}

pub fn segment(id: &str, page: u32, start: usize, end: usize, content: Content) -> Segment {
    Segment {
        id: format!("{id}:{start}-{end}"),
        content,
        evidence: vec![Evidence {
            observation: id.into(),
            page,
            start: start as u32,
            end: end as u32,
        }],
        findings: vec![],
        continuation: false,
        tight_join: false,
        logical_part: None,
    }
}

pub fn plain_blocks(text: &str, id: &str, page: u32, limit: usize) -> Vec<Segment> {
    let mut blocks = Vec::new();
    let mut offset = 0;
    let mut start = None;
    let mut emit = |start: usize, end: usize| {
        for (index, (a, b)) in split_text(&text[start..end], limit).into_iter().enumerate() {
            let mut block = segment(
                id,
                page,
                start + a,
                start + b,
                Content::Prose {
                    text: text[start + a..start + b]
                        .trim()
                        .replace("\r\n", "\n")
                        .replace('\n', " "),
                },
            );
            block.continuation = index > 0;
            block.tight_join = index > 0
                && text[start + a..]
                    .chars()
                    .next()
                    .is_some_and(|c| !c.is_whitespace())
                && text[..start + a]
                    .chars()
                    .next_back()
                    .is_some_and(|c| !c.is_whitespace());
            blocks.push(block);
        }
    };
    for line in text.split_inclusive('\n') {
        if line.trim().is_empty() {
            if let Some(s) = start.take() {
                emit(s, offset);
            }
        } else if start.is_none() {
            start = Some(offset);
        }
        offset += line.len();
    }
    if let Some(s) = start {
        emit(s, text.len());
    }
    blocks
}

pub fn markdown_blocks(text: &str, id: &str, page: u32) -> Vec<Segment> {
    use pulldown_cmark::{Event, Parser, Tag};
    let mut blocks = Vec::new();
    let mut depth = 0usize;
    let mut start = 0;
    let mut editable = false;
    let mut cursor = 0;
    for (event, range) in
        Parser::new_ext(text, librarian_core::markdown_options()).into_offset_iter()
    {
        match event {
            Event::Start(tag) => {
                if depth == 0 {
                    start = range.start;
                    editable = matches!(
                        tag,
                        Tag::Paragraph | Tag::Heading { .. } | Tag::List(_) | Tag::BlockQuote(_)
                    );
                }
                depth += 1;
            }
            Event::End(_) => {
                depth = depth.saturating_sub(1);
                if depth == 0 {
                    let end = range.end;
                    if start > cursor && !text[cursor..start].trim().is_empty() {
                        blocks.push(segment(
                            id,
                            page,
                            cursor,
                            start,
                            Content::Markdown {
                                text: text[cursor..start].into(),
                            },
                        ));
                    }
                    let raw = text[start..end].to_owned();
                    let content = if editable {
                        Content::MarkdownProse { text: raw }
                    } else {
                        Content::Markdown { text: raw }
                    };
                    blocks.push(segment(id, page, start, end, content));
                    cursor = end;
                }
            }
            _ => {}
        }
    }
    if cursor < text.len() && !text[cursor..].trim().is_empty() {
        blocks.push(segment(
            id,
            page,
            cursor,
            text.len(),
            Content::Markdown {
                text: text[cursor..].into(),
            },
        ));
    }
    blocks
}

pub fn persist(
    library: &Library,
    job: i64,
    document: &str,
    ordinal: u64,
    observation: &Observation,
) -> Result<()> {
    if !library.active(job)? {
        bail!("Job paused or cancelled");
    }
    library.db.execute(
        "INSERT OR IGNORE INTO observations(document,ordinal,payload) VALUES(?,?,?)",
        params![
            document,
            ordinal as i64,
            serde_json::to_string(observation)?
        ],
    )?;
    library.progress(job, "extracting", ordinal + 1, 0)?;
    Ok(())
}

pub fn observed(library: &Library, document: &str, ordinal: u64) -> Result<bool> {
    Ok(library
        .db
        .query_row(
            "SELECT 1 FROM observations WHERE document=? AND ordinal=?",
            params![document, ordinal as i64],
            |_| Ok(()),
        )
        .optional()?
        .is_some())
}

pub fn extract_file(
    library: &Library,
    job: i64,
    document: &str,
    path: &Path,
    extension: &str,
    config: &Config,
) -> Result<()> {
    match extension {
        "txt" | "text" | "log" => text_file(library, job, document, path, extension, config),
        "csv" | "tsv" => structured::delimited(
            library,
            job,
            document,
            path,
            if extension == "csv" { b',' } else { b'\t' },
        ),
        "srt" | "vtt" => structured::subtitles(library, job, document, path, config),
        "tif" | "tiff" => structured::tiff(library, job, document, path),
        "gif" => structured::gif(library, job, document, path),
        "md" | "markdown" => {
            let text = bounded_read(path, 64 * 1024 * 1024)?;
            let blocks = markdown_blocks(&text, "markdown", 0);
            persist(
                library,
                job,
                document,
                0,
                &Observation {
                    page: 0,
                    method: "markdown".into(),
                    raw_text: text,
                    blocks,
                    visuals: vec![],
                    findings: vec![],
                },
            )
        }
        "docx" => docx::extract(library, job, document, path, config),
        "html" | "htm" | "xhtml" => html::extract(library, job, document, path, config),
        "pdf" => pdf::extract(library, job, document, path, config),
        "png" | "jpg" | "jpeg" | "bmp" | "webp" => {
            let mut reader = image::ImageReader::open(path)?.with_guessed_format()?;
            let mut limits = image::Limits::default();
            limits.max_image_width = Some(16000);
            limits.max_image_height = Some(16000);
            limits.max_alloc = Some(512 * 1024 * 1024);
            reader.limits(limits);
            let image = reader.decode()?;
            let asset = save_image(library, &image)?;
            persist(
                library,
                job,
                document,
                0,
                &Observation {
                    page: 1,
                    method: "image".into(),
                    raw_text: String::new(),
                    blocks: vec![],
                    visuals: vec![Visual {
                        asset,
                        purpose: "page".into(),
                        replace_page: true,
                        after_block: 0,
                    }],
                    findings: vec![],
                },
            )
        }
        _ => bail!(
            "Unsupported format .{extension}; source retained. Supported: text, Markdown, SRT/VTT, CSV/TSV, HTML, DOCX, PDF and common raster images"
        ),
    }
}

fn text_file(
    library: &Library,
    job: i64,
    document: &str,
    path: &Path,
    extension: &str,
    config: &Config,
) -> Result<()> {
    let mut reader = BufReader::new(File::open(path)?);
    let mut carry = Vec::new();
    let mut ordinal = 0;
    let mut continuing = false;
    let mut tight_join = false;
    loop {
        let mut bytes = vec![0u8; WINDOW_BYTES];
        let n = reader.read(&mut bytes)?;
        bytes.truncate(n);
        carry.extend(bytes);
        if carry.is_empty() {
            break;
        }
        let valid = match std::str::from_utf8(&carry) {
            Ok(_) => carry.len(),
            Err(e) if e.error_len().is_none() && n > 0 => e.valid_up_to(),
            Err(_) => bail!("Text input is not valid UTF-8; convert its encoding before import"),
        };
        let mut end = valid;
        if n > 0
            && let Some(pos) = carry[..valid].windows(2).rposition(|w| w == b"\n\n")
            && pos > valid / 2
        {
            end = pos + 2;
        }
        let text = String::from_utf8(carry[..end].to_vec())?;
        carry.drain(..end);
        if !observed(library, document, ordinal)? {
            let id = format!("window-{ordinal}");
            let mut blocks = if matches!(extension, "md" | "markdown") {
                markdown_blocks(&text, &id, 0)
            } else {
                plain_blocks(&text, &id, 0, config.processing.chunk_bytes)
            };
            if continuing && !blocks.is_empty() && matches!(extension, "txt" | "text" | "log") {
                blocks[0].continuation = true;
                blocks[0].tight_join =
                    tight_join && text.chars().next().is_some_and(|c| !c.is_whitespace());
            }
            let findings = if matches!(extension, "md" | "markdown") && n > 0 && end == valid {
                vec!["Markdown window ended without a paragraph boundary; inspect structure at this source boundary".into()]
            } else {
                vec![]
            };
            persist(
                library,
                job,
                document,
                ordinal,
                &Observation {
                    page: 0,
                    method: extension.into(),
                    raw_text: text.clone(),
                    blocks,
                    visuals: vec![],
                    findings,
                },
            )?;
        }
        continuing = !text.ends_with("\n\n");
        tight_join = text.chars().next_back().is_some_and(|c| !c.is_whitespace());
        ordinal += 1;
        if n == 0 && carry.is_empty() {
            break;
        }
    }
    Ok(())
}

pub(super) fn bounded_read(path: &Path, limit: u64) -> Result<String> {
    let file = File::open(path)?;
    if file.metadata()?.len() > limit {
        bail!("Structured input exceeds parser limit of {limit} bytes");
    }
    let mut text = String::new();
    file.take(limit + 1).read_to_string(&mut text)?;
    if text.len() as u64 > limit {
        bail!("Structured input exceeds parser limit");
    }
    Ok(text)
}
