//! Paragraph formation and role assignment inside one column of text.
use super::geom::{quantile, union_all};
use super::text::{
    Composed, Ctx, Join, Line, caption_like, compose, leader, lines_from, list_marker,
    section_number, sentence_end,
};
use super::{Rect, Role, Span};

pub(crate) struct Draft {
    pub role: Role,
    pub text: String,
    pub spans: Vec<Span>,
    pub bbox: Rect,
    pub size: f32,
    pub bold: bool,
    pub words: Vec<u32>,
    pub italic: bool,
    pub lines: usize,
    /// Distance from the previous block, in ordinary line steps.
    pub gap_above: f32,
    pub hyphens_removed: u32,
    pub hyphens_kept: u32,
}

struct Block {
    lines: Vec<Line>,
}

impl Block {
    fn first(&self) -> &Line {
        &self.lines[0]
    }
    fn last(&self) -> &Line {
        &self.lines[self.lines.len() - 1]
    }
}

/// A contents entry: text, a wide gap, then a page number at the line end.
fn folio_line(ctx: &Ctx, line: &Line) -> bool {
    let [.., before, last] = line.words.as_slice() else {
        return false;
    };
    let (before, last) = (ctx.word(*before), ctx.word(*last));
    let text = last.text.trim_matches(|c: char| ".·…".contains(c));
    let number = !text.is_empty()
        && text.chars().count() <= 4
        && (text.chars().all(|c| c.is_ascii_digit()) || text.chars().all(|c| "ivxlc".contains(c)));
    number && last.bbox.x - before.bbox.right() >= 1.5 * line.size
}

fn all_caps(text: &str) -> bool {
    let letters: Vec<char> = text.chars().filter(|c| c.is_alphabetic()).collect();
    letters.len() >= 4 && letters.iter().all(|c| c.is_uppercase())
}

/// Split a column of words into paragraphs and classify each.
pub(crate) fn flow_blocks(ctx: &Ctx, words: &[u32]) -> Vec<Draft> {
    let lines = lines_from(ctx, words);
    if lines.is_empty() {
        return Vec::new();
    }
    let body = ctx.stats.body;
    let left = lines.iter().map(|l| l.bbox.x).fold(f32::MAX, f32::min);
    let right = lines
        .iter()
        .map(|l| l.bbox.right())
        .fold(f32::MIN, f32::max);
    // Typical pitch of this column, in points per unit of font size.
    let mut ratios: Vec<f32> = lines
        .windows(2)
        .filter(|w| (w[0].size - w[1].size).abs() <= 0.05 * w[0].size.max(1.0))
        .map(|w| (w[1].baseline - w[0].baseline) / w[0].size.max(1.0))
        .filter(|r| (0.8..3.0).contains(r))
        .collect();
    // Ordinary leading is the smallest step that recurs. A column of one-line
    // entries shows only paragraph steps, so the document's own leading caps it.
    ratios.sort_by(f32::total_cmp);
    let need = 2.max(ratios.len() / 12);
    let local = (0..ratios.len()).find_map(|i| {
        let run = ratios[i..]
            .iter()
            .take_while(|r| **r <= ratios[i] * 1.06)
            .count();
        (run >= need).then(|| ratios[i..i + run].iter().sum::<f32>() / run as f32)
    });
    let pitch = local.unwrap_or(ctx.stats.pitch).min(ctx.stats.pitch * 1.05);
    let flush = lines
        .iter()
        .filter(|l| l.bbox.right() >= right - 1.5)
        .count();
    let justified = lines.len() >= 4 && flush * 5 >= lines.len() * 3;

    let mut blocks: Vec<Block> = Vec::new();
    for line in lines {
        let starts_new = match blocks.last() {
            None => true,
            Some(block) => {
                let previous = block.last();
                let first = block.first();
                let size = previous.size.max(line.size).max(1.0);
                let step = line.baseline - previous.baseline;
                let text = line.text(ctx);
                let before = previous.text(ctx);
                let hanging = block.lines.len() >= 2
                    && previous.bbox.x >= first.bbox.x + 0.8 * size
                    && line.bbox.x <= first.bbox.x + 0.3 * size;
                let indented = line.bbox.x > previous.bbox.x + 0.8 * size
                    && line.bbox.x < previous.bbox.x + 4.0 * size
                    && (sentence_end(&before) || previous.bbox.right() < right - 3.0 * size)
                    && list_marker(&block.first().text(ctx)).is_none();
                // Display type (titles) wraps centred or ragged; only spacing
                // and style separate its blocks.
                let display = line.size >= 1.15 * body && previous.size >= 1.15 * body;
                let (hanging, indented) = (hanging && !display, indented && !display);
                let short_end = justified
                    && !display
                    && previous.bbox.right() < right - (3.0 * size).max(0.12 * (right - left))
                    && sentence_end(&before)
                    && text.chars().next().is_some_and(|c| !c.is_lowercase());
                step > 1.2 * pitch * size
                    || step < 0.4 * size
                    || (previous.size - line.size).abs() > 0.12 * size
                    || previous.bold != line.bold
                    // An italic lead that runs on into the sentence is emphasis,
                    // not a separate block.
                    || (previous.italic != line.italic
                        && (sentence_end(&before)
                            || !text.chars().next().is_some_and(char::is_lowercase)))
                    || previous.mono != line.mono
                    || list_marker(&text).is_some()
                    || text.starts_with(['†', '‡'])
                    || leader(&before)
                    || folio_line(ctx, previous)
                    || hanging
                    || indented
                    || short_end
            }
        };
        if starts_new {
            blocks.push(Block { lines: vec![line] });
        } else if let Some(block) = blocks.last_mut() {
            block.lines.push(line);
        }
    }

    let entries = blocks
        .iter()
        .filter(|b| {
            b.lines.len() == 1 && (folio_line(ctx, b.first()) || leader(&b.first().text(ctx)))
        })
        .count();
    let contents = entries >= 4 && entries * 5 >= blocks.len() * 2;
    let page_height = ctx.page.height.max(1.0);
    let mut drafts: Vec<Draft> = Vec::with_capacity(blocks.len());
    let mut caps: Vec<bool> = Vec::with_capacity(blocks.len());
    for (block_index, block) in blocks.iter().enumerate() {
        let refs: Vec<&Line> = block.lines.iter().collect();
        let gap_above = match block_index.checked_sub(1) {
            Some(before) => {
                let (above, first) = (blocks[before].last(), block.first());
                (first.baseline - above.baseline) / (pitch * first.size.max(above.size).max(1.0))
            }
            None => f32::MAX,
        };
        let mono = refs.iter().filter(|l| l.mono).count() * 10 >= refs.len() * 6;
        let Composed {
            text,
            spans,
            hyphens_removed,
            hyphens_kept,
        } = compose(ctx, &refs, if mono { Join::Lines } else { Join::Flow });
        let mut sizes: Vec<f32> = refs.iter().map(|l| l.size).collect();
        let size = quantile(&mut sizes, 0.5);
        let bold = refs.iter().all(|l| l.bold);
        let bbox = union_all(refs.iter().map(|l| l.bbox));
        let chars = text.chars().count();
        let ratio = size / body.max(1.0);
        let numbered = section_number(&text);
        let marker = list_marker(&text).is_some();
        let upper = all_caps(&text);
        let in_margin = bbox.cy() < 0.07 * page_height || bbox.cy() > 0.93 * page_height;
        let ends_like_sentence = text.trim_end().ends_with(['.', ',', ';']);
        let entry = contents && (ratio < 1.5 || folio_line(ctx, block.last()));
        let compact =
            refs.len() <= 3 && chars <= 200 && !leader(&text) && !caption_like(&text) && !entry;
        let larger = ratio >= 1.15 && !(marker && !numbered && ratio < 1.3);
        let bold_title = bold
            && ratio >= 0.95
            && chars <= 120
            && refs.len() <= 2
            && (!ends_like_sentence || numbered)
            && (!marker || numbered);
        // "3.2 Method" on its own line with space above is a heading even in
        // the body face.
        // A bare "1." is how list items start, so the number must be
        // multi-level ("3.2") or undotted ("3 Method").
        let label = text.split_whitespace().next().unwrap_or("");
        let isolated = refs.len() == 1
            && gap_above >= 1.3
            && chars <= 80
            && !ends_like_sentence
            && (!label.ends_with(['.', ')']) || label.trim_end_matches('.').contains('.'));
        let numbered_title = numbered
            && ratio >= 0.95
            && (bold || ratio >= 1.08 || upper || isolated)
            && chars <= 120
            && refs.len() <= 2;
        // Small type low on the page that opens with a reference mark is a
        // note, even though an asterisk can also be a bullet.
        let note = ratio <= 0.88
            && bbox.y > 0.6 * page_height
            && text.chars().next().is_some_and(|c| "*†‡".contains(c));
        let role = if mono && chars >= 2 {
            Role::Code
        } else if compact
            && text.chars().any(char::is_alphanumeric)
            && ((!in_margin && (larger || bold_title || numbered_title))
                || (in_margin && ratio >= 1.3))
        {
            Role::Heading
        } else if note {
            Role::Footnote
        } else if marker {
            Role::ListItem
        } else if chars <= 600 && caption_like(&text) {
            Role::Caption
        } else if ratio <= 0.88
            && bbox.y > 0.6 * page_height
            && text
                .chars()
                .next()
                .is_some_and(|c| c.is_ascii_digit() || "*†‡§".contains(c))
        {
            Role::Footnote
        } else {
            Role::Paragraph
        };
        caps.push(
            upper
                && !in_margin
                && refs.len() <= 2
                && chars <= 80
                && !marker
                && !entry
                && !leader(&text)
                && !text.chars().any(|c| c.is_ascii_digit()),
        );
        drafts.push(Draft {
            role,
            text,
            spans,
            bbox,
            size,
            bold,
            words: refs.iter().flat_map(|l| l.words.iter().copied()).collect(),
            italic: refs.iter().all(|l| l.italic),
            lines: refs.len(),
            gap_above,
            hyphens_removed,
            hyphens_kept,
        });
    }
    // A short bold label set at or below body size that introduces regular
    // text of the same size is a heading ("New entries" above a small list).
    for index in 0..drafts.len().saturating_sub(1) {
        let (current, next) = (&drafts[index], &drafts[index + 1]);
        let chars = current.text.chars().count();
        // Bold labels need no extra space; an italic line does, or it is just
        // an emphasised line of the paragraph.
        let letters = current.text.chars().filter(|c| c.is_alphabetic()).count();
        let wordy = letters >= 8
            && letters * 10 >= chars * 7
            && current.text.chars().next().is_some_and(char::is_uppercase);
        let distinct = (current.bold && !next.bold)
            || (current.italic
                && !next.italic
                && current.lines == 1
                && current.gap_above >= 1.25
                && wordy);
        if current.role == Role::Paragraph
            && distinct
            && current.size >= 0.7 * body
            && next.size <= current.size + 0.25
            && matches!(next.role, Role::Paragraph | Role::ListItem)
            && (2..=60).contains(&chars)
            && !current.text.contains('\n')
            && !current
                .text
                .trim_end()
                .ends_with(['.', ',', ';', ':', '"', '”', ')'])
            && !leader(&current.text)
            && current.text.chars().any(char::is_alphabetic)
        {
            drafts[index].role = Role::Heading;
        }
    }
    // A short all-capitals line that introduces body text is a heading.
    for index in 0..drafts.len().saturating_sub(1) {
        if caps[index]
            && drafts[index].role == Role::Paragraph
            && drafts[index].size >= 0.95 * body
            && matches!(drafts[index + 1].role, Role::Paragraph | Role::ListItem)
            && !caps[index + 1]
            && drafts[index + 1].text.chars().count() >= 60
        {
            drafts[index].role = Role::Heading;
        }
    }
    drafts
}

/// Text of a table cell or a figure: lines stay separate unless they wrap.
pub(crate) fn cell_text(ctx: &Ctx, words: &[u32]) -> Composed {
    let lines = lines_from(ctx, words);
    let refs: Vec<&Line> = lines.iter().collect();
    compose(ctx, &refs, Join::Flow)
}

pub(crate) fn figure_text(ctx: &Ctx, words: &[u32]) -> Composed {
    let lines = lines_from(ctx, words);
    let refs: Vec<&Line> = lines.iter().collect();
    compose(ctx, &refs, Join::Lines)
}
