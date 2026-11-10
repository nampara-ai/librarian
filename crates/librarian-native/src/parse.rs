//! Fast deterministic parsing: crack the file, extract positioned words with
//! font evidence plus everything drawn on the page, and rebuild layout without
//! model calls.
use anyhow::{Context, Result, bail};
use librarian_core::layout::{
    self, Document, DocumentInput, Font, Link, Origin, OutlineItem, PageInput, Rect, Word,
};
pub use pdfium_render::prelude::Pdfium;
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
    let pdf = pdfium.load_pdf_from_file(path, None).map_err(|error| {
        let reason = match error {
            PdfiumError::PdfiumLibraryInternalError(PdfiumInternalError::PasswordError) => {
                "it is password-protected; supply an unlocked copy"
            }
            PdfiumError::PdfiumLibraryInternalError(PdfiumInternalError::FormatError) => {
                "it is damaged or not a PDF"
            }
            PdfiumError::PdfiumLibraryInternalError(PdfiumInternalError::SecurityError) => {
                "it uses an unsupported security scheme"
            }
            _ => "the PDF engine could not read it",
        };
        anyhow::anyhow!("Cannot open {}: {reason}", path.display())
    })?;
    let opened = started.elapsed();
    let input = pdf_input(&pdf)?;
    let extracted = started.elapsed();
    let document = layout::reconstruct(&input);
    let finished = started.elapsed();
    if !document.stats.accounted() {
        bail!(
            "Layout accounting failed: {} words unassigned, {} duplicated, {} of {} characters represented",
            document.stats.unassigned_words,
            document.stats.duplicated_words,
            document.stats.chars_out + document.stats.hyphens_removed as u64,
            document.stats.chars_in
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
    // TeX fonts encode style in the name: cmbx10, sfbx0900, cmti10, cmtt9.
    let tex = ["cm", "sf", "ec", "lm", "tc"]
        .iter()
        .find_map(|p| lower.strip_prefix(p))
        .filter(|rest| rest.chars().any(|c| c.is_ascii_digit()) && rest.len() <= 12)
        .map(|rest| {
            rest.trim_end_matches(|c: char| c.is_ascii_digit())
                .to_owned()
        });
    let tex_is = |styles: &[&str]| tex.as_deref().is_some_and(|t| styles.contains(&t));
    let heavy = [
        "bold",
        "semibold",
        "demibold",
        "black",
        "heavy",
        "extrabold",
        "-medi",
        "-bd",
        "-demi",
    ]
    .iter()
    .any(|w| lower.contains(w))
        || tex_is(&["bx", "b", "bxti", "bxsl", "ssbx", "bsy", "mib"]);
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
        italic: lower.contains("italic")
            || lower.contains("oblique")
            || lower.ends_with("-it")
            || tex_is(&["ti", "sl", "bxti", "bxsl", "ssi", "u"])
            || (tex.is_none() && character.font_is_italic()),
        mono: character.font_is_fixed_pitch()
            || tex_is(&["tt", "tti", "sltt", "tcsc", "itt"])
            || ["mono", "courier", "consol", "menlo"]
                .iter()
                .any(|w| lower.contains(w)),
    }
}

struct Pending {
    text: String,
    bbox: Rect,
    /// Right edge of the last glyph's advance box.
    cell_right: f32,
    baseline: f32,
    font: u32,
    size: f32,
    hyphen: bool,
    hidden: bool,
    rotated: bool,
}

/// Maps unrotated page user space to the displayed page with a top-left origin.
#[derive(Clone, Copy)]
struct Display {
    rotation: u8,
    left: f32,
    bottom: f32,
    right: f32,
    top: f32,
    height: f32,
}

impl Display {
    fn point(&self, x: f32, y: f32) -> (f32, f32) {
        let (dx, dy) = match self.rotation {
            1 => (y - self.bottom, self.right - x),
            2 => (self.right - x, self.top - y),
            3 => (self.top - y, x - self.left),
            _ => (x - self.left, y - self.bottom),
        };
        (dx, self.height - dy)
    }

    fn rect(&self, corners: [(f32, f32); 4]) -> Rect {
        let points = corners.map(|(x, y)| self.point(x, y));
        let left = points.iter().map(|p| p.0).fold(f32::MAX, f32::min);
        let right = points.iter().map(|p| p.0).fold(f32::MIN, f32::max);
        let top = points.iter().map(|p| p.1).fold(f32::MAX, f32::min);
        let bottom = points.iter().map(|p| p.1).fold(f32::MIN, f32::max);
        Rect::from_edges(left, top, right, bottom)
    }
}

#[derive(Default)]
struct Drawn {
    images: Vec<Rect>,
    rules: Vec<Rect>,
    fills: Vec<Rect>,
    drawings: Vec<Rect>,
    paths: usize,
}

const PATH_BUDGET: usize = 6000;
const SEGMENT_BUDGET: u32 = 400;

fn apply(chain: &[PdfMatrix], x: f32, y: f32) -> (f32, f32) {
    let mut point = (PdfPoints::new(x), PdfPoints::new(y));
    for matrix in chain.iter().rev() {
        point = matrix.apply_to_points(point.0, point.1);
    }
    (point.0.value, point.1.value)
}

fn walk<'a>(
    objects: impl Iterator<Item = PdfPageObject<'a>>,
    chain: &mut Vec<PdfMatrix>,
    display: &Display,
    page_area: f32,
    drawn: &mut Drawn,
    depth: usize,
) {
    for object in objects {
        // Bounds are reported in the parent's space.
        let bounds = |chain: &[PdfMatrix]| -> Option<Rect> {
            let quad = object.bounds().ok()?.to_rect();
            let corners = [
                (quad.left().value, quad.bottom().value),
                (quad.right().value, quad.bottom().value),
                (quad.right().value, quad.top().value),
                (quad.left().value, quad.top().value),
            ]
            .map(|(x, y)| apply(chain, x, y));
            let rect = display.rect(corners);
            rect.finite().then_some(rect)
        };
        match &object {
            PdfPageObject::Image(_) => {
                if let Some(rect) = bounds(chain) {
                    drawn.images.push(rect);
                }
            }
            PdfPageObject::Shading(_) => {
                if let Some(rect) = bounds(chain) {
                    drawn.drawings.push(rect);
                }
            }
            PdfPageObject::XObjectForm(form) => {
                if depth < 8
                    && let Ok(matrix) = object.matrix()
                {
                    chain.push(matrix);
                    walk(form.iter(), chain, display, page_area, drawn, depth + 1);
                    chain.pop();
                }
            }
            PdfPageObject::Path(path) => {
                let Some(rect) = bounds(chain) else {
                    continue;
                };
                drawn.paths += 1;
                let stroked = path.is_stroked().unwrap_or(false);
                let filled = !matches!(path.fill_mode(), Ok(PdfPathFillMode::None) | Err(_));
                if !stroked && !filled {
                    continue; // A clipping or invisible path draws nothing.
                }
                if rect.h <= 1.5 && rect.w >= 4.0 {
                    drawn.rules.push(Rect::new(rect.x, rect.cy(), rect.w, 0.0));
                    continue;
                }
                if rect.w <= 1.5 && rect.h >= 4.0 {
                    drawn.rules.push(Rect::new(rect.cx(), rect.y, 0.0, rect.h));
                    continue;
                }
                let segments = path.segments();
                if drawn.paths > PATH_BUDGET || segments.len() > SEGMENT_BUDGET {
                    drawn.drawings.push(rect);
                    continue;
                }
                let Ok(own) = object.matrix() else {
                    continue;
                };
                chain.push(own);
                let mut lines: Vec<Rect> = Vec::new();
                let mut curved = false;
                let mut start: Option<(f32, f32)> = None;
                let mut cursor: Option<(f32, f32)> = None;
                let mut edge = |from: (f32, f32), to: (f32, f32), curved: &mut bool| {
                    let (dx, dy) = ((to.0 - from.0).abs(), (to.1 - from.1).abs());
                    if dy <= 0.6 && dx >= 4.0 {
                        lines.push(Rect::new(from.0.min(to.0), (from.1 + to.1) / 2.0, dx, 0.0));
                    } else if dx <= 0.6 && dy >= 4.0 {
                        lines.push(Rect::new((from.0 + to.0) / 2.0, from.1.min(to.1), 0.0, dy));
                    } else if dx > 0.6 && dy > 0.6 {
                        *curved = true;
                    }
                };
                for segment in segments.iter() {
                    let raw = (segment.x().value, segment.y().value);
                    let user = apply(chain, raw.0, raw.1);
                    let point = display.point(user.0, user.1);
                    match segment.segment_type() {
                        PdfPathSegmentType::MoveTo => {
                            start = Some(point);
                            cursor = Some(point);
                        }
                        PdfPathSegmentType::LineTo => {
                            if let Some(from) = cursor {
                                edge(from, point, &mut curved);
                            }
                            cursor = Some(point);
                        }
                        PdfPathSegmentType::BezierTo => {
                            curved = true;
                            cursor = Some(point);
                        }
                        _ => {}
                    }
                    if segment.is_close()
                        && let (Some(from), Some(to)) = (cursor, start)
                    {
                        edge(from, to, &mut curved);
                        cursor = start;
                    }
                }
                chain.pop();
                if curved {
                    drawn.drawings.push(rect);
                } else if stroked {
                    drawn.rules.extend(lines);
                } else if rect.area() < 0.9 * page_area {
                    // A filled box: shading behind cells or a callout.
                    drawn.fills.push(rect);
                }
            }
            _ => {}
        }
    }
}

fn outline(pdf: &PdfDocument<'_>, heights: &[f32]) -> Vec<OutlineItem> {
    let mut items = Vec::new();
    for bookmark in pdf.bookmarks().iter().take(5000) {
        let Some(title) = bookmark.title() else {
            continue;
        };
        let title = title.trim().to_owned();
        if title.is_empty() {
            continue;
        }
        let target = |destination: &PdfDestination<'_>| -> Option<(u32, Option<f32>)> {
            let page = destination.page_index().ok()? as u32;
            let y = match destination.view_settings() {
                Ok(PdfDestinationViewSettings::SpecificCoordinatesAndZoom(_, Some(y), _))
                | Ok(PdfDestinationViewSettings::FitPageHorizontallyToWindow(Some(y))) => {
                    heights.get(page as usize).map(|height| height - y.value)
                }
                _ => None,
            };
            Some((page, y))
        };
        let resolved = bookmark
            .destination()
            .as_ref()
            .and_then(target)
            .or_else(|| {
                let action = bookmark.action()?;
                let local = action.as_local_destination_action()?;
                let destination = local.destination().ok()?;
                target(&destination)
            });
        let Some((page, y)) = resolved else {
            continue;
        };
        let mut level = 1u8;
        let mut parent = bookmark.parent();
        while let Some(up) = parent {
            level = level.saturating_add(1);
            if level > 8 {
                break;
            }
            parent = up.parent();
        }
        items.push(OutlineItem {
            level,
            title,
            page: page + 1,
            y,
        });
    }
    items
}

pub fn pdf_input(pdf: &PdfDocument<'_>) -> Result<DocumentInput> {
    let mut fonts: Vec<Font> = Vec::new();
    let mut font_index: HashMap<String, u32> = HashMap::new();
    let mut pages = Vec::new();
    for (index, page) in pdf.pages().iter().enumerate() {
        let (width, height) = (page.width().value, page.height().value);
        let rotation = match page.rotation() {
            Ok(PdfPageRenderRotation::Degrees90) => 1,
            Ok(PdfPageRenderRotation::Degrees180) => 2,
            Ok(PdfPageRenderRotation::Degrees270) => 3,
            _ => 0,
        };
        let user = page
            .boundaries()
            .bounding()
            .map(|b| b.bounds)
            .unwrap_or_else(|_| page.page_size());
        let display = Display {
            rotation,
            left: user.left().value,
            bottom: user.bottom().value,
            right: user.right().value,
            top: user.top().value,
            height,
        };
        // Text positions are reported in page user space; the visible page may
        // be an offset or rotated window onto it.
        let to_rect = |r: PdfRect| {
            display.rect([
                (r.left().value, r.bottom().value),
                (r.right().value, r.bottom().value),
                (r.right().value, r.top().value),
                (r.left().value, r.top().value),
            ])
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
                    hidden: p.hidden,
                    rotated: p.rotated,
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
            // PDFium replaces every line-end hyphen with a marker, whether it is
            // a word break or part of a compound; only U+00AD is truly soft.
            let marker = c == '\u{2}';
            let soft = c == '\u{ad}';
            let Ok(bounds) = character.tight_bounds() else {
                continue;
            };
            let bbox = to_rect(bounds);
            if !bbox.finite() {
                continue;
            }
            // PDFium emits no line break after a line-end hyphen, so a word can
            // silently wrap ("four-" / "day"). A glyph below the word and back
            // to its left starts a new line.
            if current
                .as_ref()
                .is_some_and(|p| !p.rotated && bbox.cy() > p.bbox.bottom() && bbox.x < p.bbox.x)
            {
                flush(&mut current, &mut words, line);
                line += 1;
            }
            let size = character.scaled_font_size().value;
            // Some files carry no space characters at all. The advance boxes
            // of neighbouring letters touch; a clear gap between them is a
            // word space the file left implicit.
            let cell = character.loose_bounds().map(&to_rect).ok();
            if let (Some(p), Some(cell)) = (current.as_ref(), cell.as_ref())
                && !p.rotated
                && cell.x - p.cell_right > 0.1 * size.max(1.0)
                && cell.overlap_y(&p.bbox) > 0.0
            {
                flush(&mut current, &mut words, line);
            }
            let cell_right = cell.map_or(bbox.right(), |c| c.right());
            let glyph = if soft || marker { '-' } else { c };
            match current.as_mut() {
                // A style change inside a word keeps the word whole; the first
                // glyph describes it.
                Some(p) => {
                    p.text.push(glyph);
                    p.cell_right = cell_right;
                    p.bbox = p.bbox.union(&bbox);
                    p.hyphen = soft;
                }
                None => {
                    let name = character.font_name();
                    let font = match font_index.get(&name) {
                        Some(id) => *id,
                        None => {
                            let id = fonts.len() as u32;
                            fonts.push(font_flags(&name, &character));
                            font_index.insert(name, id);
                            id
                        }
                    };
                    let baseline = match character.origin() {
                        Ok((x, y)) if rotation == 0 => display.point(x.value, y.value).1,
                        _ => bbox.bottom(),
                    };
                    let hidden = matches!(
                        character.render_mode(),
                        Ok(PdfPageTextRenderMode::Invisible
                            | PdfPageTextRenderMode::InvisibleClipping)
                    );
                    let angle = (character.angle_degrees().unwrap_or(0.0)
                        + 90.0 * f32::from(rotation))
                    .rem_euclid(360.0);
                    let rotated = angle > 5.0 && angle < 355.0;
                    current = Some(Pending {
                        text: glyph.to_string(),
                        bbox,
                        cell_right,
                        baseline,
                        font,
                        size,
                        hyphen: soft,
                        hidden,
                        rotated,
                    });
                }
            }
        }
        flush(&mut current, &mut words, line);

        let mut drawn = Drawn::default();
        let mut chain = Vec::new();
        walk(
            page.objects().iter(),
            &mut chain,
            &display,
            width * height,
            &mut drawn,
            0,
        );
        let mut links = Vec::new();
        for link in page.links().iter().take(2000) {
            let Ok(rect) = link.rect() else {
                continue;
            };
            let rect = display.rect([
                (rect.left().value, rect.bottom().value),
                (rect.right().value, rect.bottom().value),
                (rect.right().value, rect.top().value),
                (rect.left().value, rect.top().value),
            ]);
            let uri = link
                .action()
                .and_then(|a| a.as_uri_action().and_then(|u| u.uri().ok()));
            let target = link
                .destination()
                .and_then(|d| d.page_index().ok())
                .or_else(|| {
                    let action = link.action()?;
                    let local = action.as_local_destination_action()?;
                    local.destination().ok()?.page_index().ok()
                })
                .map(|p| p as u32 + 1);
            if rect.finite() && (uri.is_some() || target.is_some()) {
                links.push(Link {
                    rect,
                    uri,
                    page: target,
                });
            }
        }
        pages.push(PageInput {
            number: index as u32 + 1,
            width,
            height,
            words,
            images: drawn.images,
            rules: drawn.rules,
            fills: drawn.fills,
            drawings: drawn.drawings,
            links,
            text_chars,
            bad_chars,
        });
    }
    let heights: Vec<f32> = pages.iter().map(|p| p.height).collect();
    Ok(DocumentInput {
        format: "pdf".to_owned(),
        fonts,
        outline: outline(pdf, &heights),
        pages,
    })
}
