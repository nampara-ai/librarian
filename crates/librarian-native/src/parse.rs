//! Fast deterministic parsing: crack the file, extract positioned words with
//! font evidence, and rebuild layout without model calls.
use anyhow::{Context, Result, bail};
use librarian_core::layout::{self, Document, DocumentInput, Font, Origin, PageInput, Rect, Word};
use pdfium_render::prelude::*;
use serde::Serialize;
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    time::Instant,
};

#[derive(Debug, Clone, Copy, Default, Serialize)]
pub struct Timing {
    pub open_ms: f64,
    pub extract_ms: f64,
    pub layout_ms: f64,
}

pub struct Parsed {
    pub input: DocumentInput,
    pub document: Document,
    pub timing: Timing,
}

/// Bind PDFium: an explicit library path, the managed engine folder, or the
/// engines folder bundled beside the executable.
pub fn pdfium(library_root: &Path, explicit: Option<&Path>) -> Result<Pdfium> {
    let path: PathBuf = match explicit {
        Some(path) => path.to_owned(),
        None => crate::assets::locate(
            &library_root.join("engines"),
            "pdfium",
            &Pdfium::pdfium_platform_library_name().to_string_lossy(),
        )?,
    };
    Ok(Pdfium::new(Pdfium::bind_to_library(&path).with_context(
        || format!("Cannot load PDFium from {}", path.display()),
    )?))
}

pub fn supported(path: &Path) -> bool {
    crate::extract::format(path) == "pdf"
}

pub fn parse(pdfium: &Pdfium, path: &Path) -> Result<Parsed> {
    if !supported(path) {
        bail!("parse currently reads PDF files; other formats use `librarian clean`");
    }
    let started = Instant::now();
    let pdf = pdfium
        .load_pdf_from_file(path, None)
        .context("Cannot open PDF (encrypted documents require an unencrypted copy)")?;
    let opened = started.elapsed();
    let input = pdf_input(&pdf)?;
    let extracted = started.elapsed();
    let document = layout::reconstruct(&input);
    let finished = started.elapsed();
    if document.stats.unassigned_words > 0 || document.stats.duplicated_words > 0 {
        bail!(
            "Layout accounting failed: {} words unassigned, {} duplicated",
            document.stats.unassigned_words,
            document.stats.duplicated_words
        );
    }
    Ok(Parsed {
        input,
        document,
        timing: Timing {
            open_ms: opened.as_secs_f64() * 1e3,
            extract_ms: (extracted - opened).as_secs_f64() * 1e3,
            layout_ms: (finished - extracted).as_secs_f64() * 1e3,
        },
    })
}

fn font_flags(name: &str, character: &PdfPageTextChar<'_>) -> Font {
    let base = name.split_once('+').map_or(name, |(_, rest)| rest);
    let lower = base.to_ascii_lowercase();
    let heavy = [
        "bold",
        "semibold",
        "demibold",
        "black",
        "heavy",
        "extrabold",
    ]
    .iter()
    .any(|w| lower.contains(w));
    let weight_bold = matches!(
        character.font_weight(),
        Some(
            PdfFontWeight::Weight600
                | PdfFontWeight::Weight700Bold
                | PdfFontWeight::Weight800
                | PdfFontWeight::Weight900
        )
    );
    Font {
        name: base.to_owned(),
        bold: heavy || weight_bold || character.font_is_bold_reenforced(),
        italic: lower.contains("italic") || lower.contains("oblique") || character.font_is_italic(),
        mono: character.font_is_fixed_pitch()
            || ["mono", "courier", "consol", "menlo", "code"]
                .iter()
                .any(|w| lower.contains(w)),
    }
}

struct Pending {
    text: String,
    bbox: Rect,
    baseline: f32,
    font: u32,
    size: f32,
    hyphen: bool,
}

pub fn pdf_input(pdf: &PdfDocument<'_>) -> Result<DocumentInput> {
    let mut fonts: Vec<Font> = Vec::new();
    let mut font_index: HashMap<String, u32> = HashMap::new();
    let mut pages = Vec::new();
    for (index, page) in pdf.pages().iter().enumerate() {
        let (width, height) = (page.width().value, page.height().value);
        let to_rect = |r: PdfRect| {
            Rect::new(
                r.left().value,
                height - r.top().value,
                r.right().value - r.left().value,
                r.top().value - r.bottom().value,
            )
        };
        let mut words = Vec::new();
        let (mut text_chars, mut bad_chars, mut line) = (0u32, 0u32, 0u32);
        let mut current: Option<Pending> = None;
        let flush = |current: &mut Option<Pending>, words: &mut Vec<Word>, line: u32| {
            if let Some(p) = current.take() {
                words.push(Word {
                    text: p.text,
                    bbox: p.bbox,
                    font: p.font,
                    size: p.size,
                    line,
                    baseline: p.baseline,
                    hyphen: p.hyphen,
                    origin: Origin::Native,
                    confidence: 1.0,
                });
            }
        };
        let text = page.text()?;
        for character in text.chars().iter() {
            let c = character.unicode_char().unwrap_or('\0');
            if c == '\n' || c == '\r' {
                flush(&mut current, &mut words, line);
                if c == '\n' {
                    line += 1;
                }
                continue;
            }
            if c.is_whitespace() {
                flush(&mut current, &mut words, line);
                continue;
            }
            text_chars += 1;
            if c == '\0' || c == '\u{fffd}' {
                bad_chars += 1;
                continue;
            }
            let soft = c == '\u{2}';
            let Ok(bounds) = character.tight_bounds() else {
                continue;
            };
            let bbox = to_rect(bounds);
            let size = character.scaled_font_size().value;
            // PDFium emits no line break after a line-end hyphen, so a word can
            // silently wrap ("four-" / "day"). A glyph below the word and back to
            // its left starts a new line; the hyphen is a line-break hyphen.
            if current
                .as_ref()
                .is_some_and(|p| bbox.cy() > p.bbox.bottom() && bbox.x < p.bbox.x)
            {
                if let Some(p) = current.as_mut() {
                    p.hyphen = p.text.ends_with('-');
                }
                flush(&mut current, &mut words, line);
                line += 1;
            }
            let needs_font = current
                .as_ref()
                .is_none_or(|p| (p.size - size).abs() > 0.05);
            let font = if needs_font {
                let name = character.font_name();
                match font_index.get(&name) {
                    Some(id) => *id,
                    None => {
                        let id = fonts.len() as u32;
                        fonts.push(font_flags(&name, &character));
                        font_index.insert(name, id);
                        id
                    }
                }
            } else {
                current.as_ref().map_or(0, |p| p.font)
            };
            let hyphen =
                soft || ((c == '-' || c == '\u{ad}') && character.is_hyphen().unwrap_or(false));
            let glyph = if soft { '-' } else { c };
            match current.as_mut() {
                // A style change inside a word keeps the word whole; the first
                // glyph's font describes it.
                Some(p) => {
                    p.text.push(glyph);
                    p.bbox = p.bbox.union(&bbox);
                    p.hyphen = hyphen;
                }
                None => {
                    let baseline = character
                        .origin_y()
                        .map_or(bbox.bottom(), |y| height - y.value);
                    current = Some(Pending {
                        text: glyph.to_string(),
                        bbox,
                        baseline,
                        font,
                        size,
                        hyphen,
                    });
                }
            }
        }
        flush(&mut current, &mut words, line);
        let mut images = Vec::new();
        let mut rules = Vec::new();
        for object in page.objects().iter() {
            let Ok(bounds) = object.bounds() else {
                continue;
            };
            let rect = to_rect(bounds.to_rect());
            match object {
                PdfPageObject::Image(_) => images.push(rect),
                PdfPageObject::Path(_)
                    if (rect.h <= 2.0 && rect.w >= 8.0) || (rect.w <= 2.0 && rect.h >= 8.0) =>
                {
                    rules.push(rect)
                }
                _ => {}
            }
        }
        pages.push(PageInput {
            number: index as u32 + 1,
            width,
            height,
            words,
            images,
            rules,
            text_chars,
            bad_chars,
        });
    }
    Ok(DocumentInput {
        format: "pdf".to_owned(),
        fonts,
        pages,
    })
}
