//! Lines, document statistics and text composition.
use super::geom::{median, quantile, union_all};
use super::{DocumentInput, Font, PageInput, Rect, Span, Word};
use std::collections::{HashMap, HashSet};

/// Statistics measured on the document itself. Every threshold in the layout
/// engine is expressed relative to these, never as an absolute coordinate.
pub(crate) struct DocStats {
    /// Size most of the text is set in.
    pub body: f32,
    /// Typical baseline-to-baseline distance as a multiple of font size.
    pub pitch: f32,
    /// Whole tokens that occur in the document, lowercased.
    pub vocab: HashSet<String>,
    /// The typesetter breaks words at line ends (justified, hyphenated text).
    pub hyphenating: bool,
}

pub(crate) struct Ctx<'a> {
    pub page: &'a PageInput,
    pub fonts: &'a [Font],
    pub stats: &'a DocStats,
}

impl Ctx<'_> {
    pub fn word(&self, id: u32) -> &Word {
        &self.page.words[id as usize]
    }
    pub fn font(&self, id: u32) -> Option<&Font> {
        self.fonts.get(self.word(id).font as usize)
    }
}

pub(crate) fn word_size(word: &Word) -> f32 {
    if word.size.is_finite() && word.size >= 1.0 {
        word.size
    } else {
        word.bbox.h.max(1.0)
    }
}

pub(crate) fn word_baseline(word: &Word) -> f32 {
    if word.baseline > 0.0 && word.baseline.is_finite() {
        word.baseline
    } else {
        word.bbox.bottom()
    }
}

fn core_token(text: &str) -> String {
    text.trim_matches(|c: char| !c.is_alphanumeric())
        .to_lowercase()
}

fn hyphen_char(c: char) -> bool {
    matches!(c, '-' | '\u{ad}' | '\u{2010}' | '\u{2011}')
}

/// A word that ends with a hyphen directly after a letter.
pub(crate) fn ends_with_break_hyphen(text: &str) -> bool {
    let mut chars = text.chars().rev();
    chars.next().is_some_and(hyphen_char) && chars.next().is_some_and(char::is_alphabetic)
}

impl DocStats {
    pub fn measure(input: &DocumentInput) -> Self {
        // Character-weighted mode of half-point sizes.
        let mut histogram: HashMap<i32, usize> = HashMap::new();
        let mut vocab = HashSet::new();
        let mut ratios = Vec::new();
        let (mut line_count, mut hyphen_ends) = (0usize, 0usize);
        for page in &input.pages {
            let mut previous: Option<(u32, f32, f32, Rect)> = None; // line, baseline, size, bbox
            let mut current: Option<(u32, f32, f32, Rect)> = None;
            let mut last_text: &str = "";
            for word in page.words.iter().filter(|w| !w.hidden && !w.rotated) {
                *histogram
                    .entry((word_size(word) * 2.0).round() as i32)
                    .or_default() += word.text.chars().count();
                let token = core_token(&word.text);
                if !token.is_empty() {
                    vocab.insert(token);
                }
                match current.as_mut() {
                    Some((line, _, _, bbox)) if *line == word.line => {
                        *bbox = bbox.union(&word.bbox);
                    }
                    _ => {
                        if let Some(done) = current.take() {
                            line_count += 1;
                            if ends_with_break_hyphen(last_text) {
                                hyphen_ends += 1;
                            }
                            if let Some(before) = previous {
                                let size = before.2.max(done.2);
                                if (before.2 - done.2).abs() < 0.5
                                    && before.3.overlap_x(&done.3) > 0.0
                                    && size > 0.0
                                {
                                    let ratio = (done.1 - before.1) / size;
                                    if (0.8..3.0).contains(&ratio) {
                                        ratios.push(ratio);
                                    }
                                }
                            }
                            previous = Some(done);
                        }
                        current =
                            Some((word.line, word_baseline(word), word_size(word), word.bbox));
                    }
                }
                last_text = &word.text;
            }
        }
        let body = histogram
            .into_iter()
            .max_by_key(|(size, count)| (*count, -size))
            .map(|(size, _)| size as f32 / 2.0)
            .unwrap_or(10.0);
        // Paragraph breaks can outnumber line breaks in reference material, so
        // ordinary pitch is the lower cluster rather than the median.
        let pitch = if ratios.is_empty() {
            1.2
        } else {
            quantile(&mut ratios, 0.25)
        };
        DocStats {
            body,
            pitch,
            vocab,
            // Hyphenated typesetting breaks a few percent of all lines; a ragged
            // document only ends a line on a hyphen when a compound lands there.
            hyphenating: line_count >= 20 && hyphen_ends * 50 >= line_count,
        }
    }
}

#[derive(Debug, Clone)]
pub(crate) struct Line {
    /// Word indices, left to right.
    pub words: Vec<u32>,
    pub bbox: Rect,
    pub baseline: f32,
    pub size: f32,
    pub bold: bool,
    pub mono: bool,
    pub chars: usize,
}

impl Line {
    pub fn build(ctx: &Ctx, mut words: Vec<u32>) -> Line {
        words.sort_by(|a, b| {
            ctx.word(*a)
                .bbox
                .x
                .total_cmp(&ctx.word(*b).bbox.x)
                .then(a.cmp(b))
        });
        let bbox = union_all(words.iter().map(|id| ctx.word(*id).bbox));
        let mut sizes = Vec::with_capacity(words.len());
        let mut baselines = Vec::with_capacity(words.len());
        let (mut chars, mut bold, mut mono) = (0usize, 0usize, 0usize);
        for &id in &words {
            let word = ctx.word(id);
            let count = word.text.chars().count();
            chars += count;
            sizes.push(word_size(word));
            baselines.push(word_baseline(word));
            if let Some(font) = ctx.font(id) {
                bold += count * font.bold as usize;
                mono += count * font.mono as usize;
            }
        }
        // The dominant size carries the baseline; superscripts do not move it.
        let size = quantile(&mut sizes, 0.6);
        let mut main: Vec<f32> = words
            .iter()
            .map(|id| ctx.word(*id))
            .filter(|w| word_size(w) >= size * 0.85)
            .map(word_baseline)
            .collect();
        let baseline = if main.is_empty() {
            median(&mut baselines)
        } else {
            median(&mut main)
        };
        let most = |n: usize| n * 10 >= chars.max(1) * 6;
        Line {
            bbox,
            baseline,
            size,
            // A line that merely opens with a bold run-in phrase is not bold.
            bold: bold * 10 >= chars.max(1) * 9,
            mono: most(mono),
            chars,
            words,
        }
    }

    pub fn text(&self, ctx: &Ctx) -> String {
        let mut text = String::new();
        for &id in &self.words {
            if !text.is_empty() {
                text.push(' ');
            }
            text.push_str(&ctx.word(id).text);
        }
        text
    }
}

/// Group words of one column flow into text lines, top to bottom.
pub(crate) fn lines_from(ctx: &Ctx, words: &[u32]) -> Vec<Line> {
    let mut ids: Vec<u32> = words.to_vec();
    ids.sort_by(|a, b| {
        word_baseline(ctx.word(*a))
            .total_cmp(&word_baseline(ctx.word(*b)))
            .then(a.cmp(b))
    });
    // Baseline clusters.
    let mut groups: Vec<(f32, f32, Vec<u32>)> = Vec::new(); // mean baseline, max size, ids
    for id in ids {
        let word = ctx.word(id);
        let (baseline, size) = (word_baseline(word), word_size(word));
        match groups.last_mut() {
            Some((mean, max_size, members))
                if baseline - *mean <= 0.22 * size.max(*max_size).max(1.0) =>
            {
                *mean = (*mean * members.len() as f32 + baseline) / (members.len() as f32 + 1.0);
                *max_size = max_size.max(size);
                members.push(id);
            }
            _ => groups.push((baseline, size, vec![id])),
        }
    }
    let mut lines: Vec<Line> = groups
        .into_iter()
        .map(|(_, _, members)| Line::build(ctx, members))
        .collect();
    // Superscripts and subscripts sit on their own baseline. Fold a small-type
    // line into the neighbouring line whose band contains it.
    let mut index = 0;
    while index < lines.len() {
        let small = &lines[index];
        let mut target = None;
        for neighbour in [index.wrapping_sub(1), index + 1] {
            let Some(other) = lines.get(neighbour) else {
                continue;
            };
            let attached = small.size <= other.size * 0.85
                && small.bbox.overlap_y(&other.bbox) >= 0.5 * small.bbox.h.max(0.1)
                && small.bbox.x >= other.bbox.x - other.size
                && small.bbox.right() <= other.bbox.right() + 2.0 * other.size
                && small.chars * 4 <= other.chars.max(1);
            if attached {
                target = Some(neighbour);
                break;
            }
        }
        match target {
            Some(neighbour) => {
                let small = lines.remove(index);
                let neighbour = if neighbour > index {
                    neighbour - 1
                } else {
                    neighbour
                };
                let mut merged = lines[neighbour].words.clone();
                merged.extend(small.words);
                lines[neighbour] = Line::build(ctx, merged);
                index = index.saturating_sub(1);
            }
            None => index += 1,
        }
    }
    lines.sort_by(|a, b| {
        a.baseline
            .total_cmp(&b.baseline)
            .then(a.bbox.x.total_cmp(&b.bbox.x))
    });
    lines
}

/// Length in bytes of a leading list marker and its following whitespace.
pub(crate) fn list_marker(text: &str) -> Option<usize> {
    let first = text.chars().next()?;
    let marker_end = if "•◦▪▫‣⁃●○■□–—-*·➢►▶✓✔❖◆◇➤".contains(first)
    {
        first.len_utf8()
    } else {
        let bytes = text.as_bytes();
        let mut i = 0;
        if bytes.first() == Some(&b'(') {
            i += 1;
        }
        let digits = bytes[i..].iter().take_while(|b| b.is_ascii_digit()).count();
        let romans = bytes[i..]
            .iter()
            .take_while(|b| b"ivxIVX".contains(b))
            .count();
        if (1..=3).contains(&digits) {
            i += digits;
        } else if (1..=4).contains(&romans)
            && bytes
                .get(i + romans)
                .is_some_and(|b| *b == b'.' || *b == b')')
        {
            i += romans;
        } else if bytes.get(i).is_some_and(u8::is_ascii_alphabetic)
            && bytes.get(i + 1).is_some_and(|b| *b == b'.' || *b == b')')
        {
            i += 1;
        } else {
            return None;
        }
        match bytes.get(i) {
            Some(b'.') | Some(b')') => i + 1,
            _ => return None,
        }
    };
    let rest = &text[marker_end..];
    let space = rest.len() - rest.trim_start().len();
    (space > 0 && !rest.trim_start().is_empty()).then_some(marker_end + space)
}

/// Section numbering such as `3`, `3.1`, `A.2`, `IV.` followed by a title.
pub(crate) fn section_number(text: &str) -> bool {
    let Some((head, rest)) = text.split_once(char::is_whitespace) else {
        return false;
    };
    let head = head.trim_end_matches(['.', ')', ':']);
    if head.is_empty() || head.len() > 8 {
        return false;
    }
    let parts: Vec<&str> = head.split('.').collect();
    let numeric = |p: &str| {
        !p.is_empty()
            && p.len() <= 3
            && p.chars().all(|c| c.is_ascii_digit())
            && !(p.len() > 1 && p.starts_with('0'))
            && p != "0"
    };
    let letter = |p: &str| p.len() == 1 && p.chars().all(|c| c.is_ascii_uppercase());
    let roman = |p: &str| !p.is_empty() && p.len() <= 5 && p.chars().all(|c| "IVXLC".contains(c));
    let shaped = parts
        .iter()
        .enumerate()
        .all(|(index, part)| numeric(part) || (index == 0 && (letter(part) || roman(part))));
    shaped
        && rest
            .trim_start()
            .chars()
            .next()
            .is_some_and(|c| c.is_uppercase() || c.is_ascii_digit())
}

/// Table-of-contents style dot leaders ("Introduction ........ 7").
pub(crate) fn leader(text: &str) -> bool {
    text.contains("....") || text.contains(". . . .") || text.contains("····")
}

pub(crate) fn terminal(text: &str) -> bool {
    text.trim_end()
        .chars()
        .last()
        .is_some_and(|c| ".!?:;\"”’)…]".contains(c))
}

pub(crate) fn sentence_end(text: &str) -> bool {
    let trimmed = text.trim_end_matches(['"', '”', '’', ')', ']', ' ']);
    trimmed.ends_with(['.', '!', '?', '…'])
}

/// "Figure 3: ...", "Table 2.1 Results", "Source: ..." — a label, a number,
/// then a separator or a capitalised title. "Table 3 lists the pins" is prose.
pub(crate) fn caption_like(text: &str) -> bool {
    let trimmed = text.trim_start();
    let lower = trimmed.to_lowercase();
    if ["source:", "sources:", "table:", "figure:"]
        .iter()
        .any(|p| lower.starts_with(p))
    {
        return true;
    }
    let labels = [
        "figure",
        "fig.",
        "fig",
        "table",
        "chart",
        "exhibit",
        "image",
        "plate",
        "scheme",
        "listing",
        "algorithm",
    ];
    let Some(label) = labels.iter().find(|l| lower.starts_with(**l)) else {
        return false;
    };
    let rest = &trimmed[label.len()..];
    if !rest.starts_with(char::is_whitespace) {
        return false;
    }
    let rest = rest.trim_start();
    let number: String = rest
        .chars()
        .take_while(|c| c.is_alphanumeric() || ".-–".contains(*c))
        .collect();
    if number.is_empty()
        || number.chars().count() > 7
        || !number.chars().any(|c| c.is_ascii_digit())
    {
        return false;
    }
    let after = &rest[number.len()..];
    if number.ends_with('.') || after.is_empty() {
        return true;
    }
    match after.chars().next() {
        Some(':' | '|' | '—') => true,
        Some(c) if c.is_whitespace() => after
            .trim_start()
            .chars()
            .next()
            .is_some_and(|c| c.is_uppercase() || c.is_ascii_digit() || "\"“(:—–-".contains(c)),
        _ => false,
    }
}

#[derive(Default)]
pub(crate) struct Composed {
    pub text: String,
    pub spans: Vec<Span>,
    pub hyphens_removed: u32,
    pub hyphens_kept: u32,
}

#[derive(Clone, Copy, PartialEq)]
pub(crate) enum Join {
    /// Flowing prose: lines join with a space, broken words are repaired.
    Flow,
    /// Lines stay on separate lines (code, stacked cell items).
    Lines,
}

#[derive(Clone, PartialEq, Default)]
struct Style {
    bold: bool,
    italic: bool,
    code: bool,
    link: Option<String>,
}

impl Style {
    fn plain(&self) -> bool {
        !self.bold && !self.italic && !self.code && self.link.is_none()
    }
}

/// What to do with a hyphen at the end of a line: `true` keeps it as part of
/// a compound, `false` removes it as a typesetter's word break.
pub(crate) fn keep_hyphen(stats: &DocStats, left: &str, soft: bool, right: &str) -> bool {
    if soft || left.ends_with('\u{ad}') {
        return false; // The file itself marks a soft hyphen.
    }
    let next = right.chars().next().unwrap_or(' ');
    if !next.is_alphabetic() || next.is_uppercase() {
        return true;
    }
    let head = core_token(left.trim_end_matches(hyphen_char));
    let tail = core_token(right);
    if head.is_empty() || tail.is_empty() {
        return true;
    }
    if stats.vocab.contains(&format!("{head}{tail}")) {
        return false;
    }
    if stats.vocab.contains(&format!("{head}-{tail}")) {
        return true;
    }
    !stats.hyphenating
}

/// Join lines into text with inline style spans and repaired line breaks.
pub(crate) fn compose(ctx: &Ctx, lines: &[&Line], join: Join) -> Composed {
    let mut out = Composed::default();
    let mut run: Option<(Style, u32)> = None;
    let style_of = |id: u32| -> Style {
        let word = ctx.word(id);
        let font = ctx.font(id);
        let link = ctx
            .page
            .links
            .iter()
            .find(|l| l.uri.is_some() && l.rect.contains_center(&word.bbox))
            .and_then(|l| l.uri.clone());
        Style {
            bold: font.is_some_and(|f| f.bold),
            italic: font.is_some_and(|f| f.italic),
            code: font.is_some_and(|f| f.mono),
            link,
        }
    };
    let close = |run: &mut Option<(Style, u32)>, end: usize, spans: &mut Vec<Span>| {
        if let Some((style, start)) = run.take()
            && !style.plain()
            && end as u32 > start
        {
            spans.push(Span {
                start,
                end: end as u32,
                bold: style.bold,
                italic: style.italic,
                code: style.code,
                link: style.link,
            });
        }
    };
    let mut previous: Option<u32> = None;
    for (line_index, line) in lines.iter().enumerate() {
        for (word_index, &id) in line.words.iter().enumerate() {
            let word = ctx.word(id);
            let style = style_of(id);
            // Separator before this word.
            let mut separator = "";
            if let Some(before) = previous {
                if word_index > 0 {
                    separator = " ";
                } else if join == Join::Lines {
                    separator = "\n";
                } else {
                    let left = ctx.word(before);
                    let _ = line_index;
                    if ends_with_break_hyphen(&left.text) {
                        if keep_hyphen(ctx.stats, &left.text, left.hyphen, &word.text) {
                            out.hyphens_kept += 1;
                        } else {
                            let cut = out
                                .text
                                .char_indices()
                                .next_back()
                                .map_or(0, |(index, _)| index);
                            out.text.truncate(cut);
                            out.hyphens_removed += 1;
                        }
                    } else {
                        separator = " ";
                    }
                }
            }
            let same = run.as_ref().is_some_and(|(s, _)| *s == style);
            if !same || separator == "\n" {
                close(&mut run, out.text.len(), &mut out.spans);
            }
            out.text.push_str(separator);
            if run.is_none() {
                run = Some((style, out.text.len() as u32));
            }
            out.text.push_str(&word.text);
            previous = Some(id);
        }
    }
    close(&mut run, out.text.len(), &mut out.spans);
    // A removed hyphen may have shortened the text under an open span.
    let len = out.text.len() as u32;
    for span in &mut out.spans {
        span.end = span.end.min(len);
        span.start = span.start.min(span.end);
    }
    out.spans.retain(|s| {
        s.end > s.start
            && out.text.is_char_boundary(s.start as usize)
            && out.text.is_char_boundary(s.end as usize)
    });
    out
}

pub(crate) fn non_space_chars(text: &str) -> u64 {
    text.chars().filter(|c| !c.is_whitespace()).count() as u64
}
