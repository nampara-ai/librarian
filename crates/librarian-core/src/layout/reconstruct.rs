//! Deterministic layout reconstruction. Every threshold is relative to measured
//! document statistics (body font size, typical line gap, page size), never to a
//! particular fixture's absolute coordinates.
use super::*;
use std::collections::{BTreeMap, BTreeSet, HashMap};

#[derive(Debug, Clone)]
struct Line {
    words: Vec<u32>,
    bbox: Rect,
    size: f32,
    bold: bool,
    mono: bool,
    chars: usize,
    text: String,
    hyphen_end: bool,
    /// Left edge of the text after a leading list marker (hanging indent).
    text_x: f32,
    baseline: f32,
    leader: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Furniture {
    None,
    Header,
    Footer,
    PageNumber,
}

struct Table {
    bbox: Rect,
    lines: Vec<usize>,
    rows: u32,
    cols: u32,
    cells: Vec<(u32, u32, Vec<usize>)>,
    fill: f32,
    bold_header: bool,
}

enum Atom {
    Line(usize),
    Table(usize),
    Picture(Rect),
}

struct Stats {
    body: f32,
    gap: f32,
    /// Typical baseline-to-baseline distance as a multiple of font size.
    pitch: f32,
}

fn word_size(word: &Word) -> f32 {
    if word.size.is_finite() && word.size >= 1.0 {
        word.size
    } else {
        word.bbox.h.max(1.0)
    }
}

fn median(values: &mut [f32]) -> f32 {
    if values.is_empty() {
        return 0.0;
    }
    values.sort_by(f32::total_cmp);
    values[values.len() / 2]
}

/// Character-weighted mode of rounded sizes: the size most text is set in.
fn body_size(input: &DocumentInput) -> f32 {
    let mut histogram: HashMap<i32, usize> = HashMap::new();
    for page in &input.pages {
        for word in &page.words {
            *histogram
                .entry((word_size(word) * 2.0).round() as i32)
                .or_default() += word.text.chars().count();
        }
    }
    histogram
        .into_iter()
        .max_by_key(|(size, count)| (*count, -size))
        .map(|(size, _)| size as f32 / 2.0)
        .unwrap_or(10.0)
}

fn build_lines(page: &PageInput, fonts: &[Font]) -> Vec<Line> {
    let mut groups: Vec<Vec<u32>> = Vec::new();
    for (index, word) in page.words.iter().enumerate() {
        let start_new = match groups.last().and_then(|g| g.last()) {
            None => true,
            Some(&previous) => {
                let previous = &page.words[previous as usize];
                let size = word_size(previous).max(word_size(word));
                let gap = word.bbox.x - previous.bbox.right();
                previous.line != word.line
                    || previous.bbox.overlap_y(&word.bbox)
                        < 0.4 * previous.bbox.h.min(word.bbox.h).max(0.1)
                    || gap > 1.8 * size
                    || gap < -size
            }
        };
        if start_new {
            groups.push(vec![index as u32]);
        } else if let Some(group) = groups.last_mut() {
            group.push(index as u32);
        }
    }
    groups
        .into_iter()
        .map(|mut ids| {
            ids.sort_by(|a, b| {
                page.words[*a as usize]
                    .bbox
                    .x
                    .total_cmp(&page.words[*b as usize].bbox.x)
            });
            let mut bbox = page.words[ids[0] as usize].bbox;
            let mut sizes = Vec::with_capacity(ids.len());
            let (mut chars, mut bold, mut mono) = (0usize, 0usize, 0usize);
            let mut text = String::new();
            for &id in &ids {
                let word = &page.words[id as usize];
                bbox = bbox.union(&word.bbox);
                sizes.push(word_size(word));
                let count = word.text.chars().count();
                chars += count;
                if let Some(font) = fonts.get(word.font as usize) {
                    if font.bold {
                        bold += count;
                    }
                    if font.mono {
                        mono += count;
                    }
                }
                if !text.is_empty() {
                    text.push(' ');
                }
                text.push_str(&word.text);
            }
            let hyphen_end = ids.last().is_some_and(|id| page.words[*id as usize].hyphen);
            let text_x = match list_marker(&text) {
                Some(len) => ids
                    .iter()
                    .map(|id| &page.words[*id as usize])
                    .scan(0usize, |seen, word| {
                        let start = *seen;
                        *seen += word.text.len() + 1;
                        Some((start, word))
                    })
                    .find(|(start, _)| *start >= len)
                    .map_or(bbox.x, |(_, word)| word.bbox.x),
                None => bbox.x,
            };
            let leader = leader(&text);
            let mut baselines: Vec<f32> = ids
                .iter()
                .map(|id| {
                    let word = &page.words[*id as usize];
                    if word.baseline > 0.0 {
                        word.baseline
                    } else {
                        word.bbox.bottom()
                    }
                })
                .collect();
            Line {
                baseline: median(&mut baselines),
                text_x,
                leader,
                bbox,
                size: median(&mut sizes),
                bold: bold * 10 >= chars.max(1) * 6,
                mono: mono * 10 >= chars.max(1) * 6,
                chars,
                text,
                hyphen_end,
                words: ids,
            }
        })
        .collect()
}

fn furniture_key(text: &str) -> String {
    let mut key = String::new();
    for c in text.chars() {
        if c.is_ascii_digit() {
            if !key.ends_with('#') {
                key.push('#');
            }
        } else if c.is_whitespace() {
            if !key.ends_with(' ') {
                key.push(' ');
            }
        } else {
            key.extend(c.to_lowercase());
        }
    }
    key.trim().to_owned()
}

/// The printed number of a folio ("12", "Page 12", "12 of 80"), if the line is one.
fn folio_value(text: &str) -> Option<i64> {
    let t = text.trim();
    if t.is_empty() || t.chars().count() > 14 {
        return None;
    }
    let lower = t.to_lowercase();
    let body = lower.strip_prefix("page ").unwrap_or(&lower);
    let first = body.split(" of ").next().unwrap_or(body).trim();
    let digits: String = first
        .trim_matches(|c: char| " -–—|·.".contains(c))
        .to_owned();
    if !digits.is_empty() && digits.chars().all(|c| c.is_ascii_digit()) {
        return digits.parse().ok();
    }
    None
}

/// Lowercase roman folios (front matter). Single capitals are too often letters.
fn roman_folio(text: &str) -> bool {
    let t = text.trim();
    !t.is_empty() && t.chars().count() <= 6 && t.chars().all(|c| "ivxlcdm".contains(c))
}

/// Length in bytes of a leading list marker and its following whitespace.
pub(crate) fn list_marker(text: &str) -> Option<usize> {
    let mut chars = text.char_indices();
    let (_, first) = chars.next()?;
    let marker_end = if "•◦▪▫‣⁃●○■□–—-*·➢►▶✓✔".contains(first) {
        first.len_utf8()
    } else {
        let bytes = text.as_bytes();
        let mut i = 0;
        let open = bytes.first() == Some(&b'(');
        if open {
            i += 1;
        }
        let digits = bytes[i..].iter().take_while(|b| b.is_ascii_digit()).count();
        if (1..=3).contains(&digits) {
            i += digits;
        } else if bytes.get(i).is_some_and(|b| b.is_ascii_lowercase())
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

/// Table-of-contents style dot leaders ("Introduction ........ 7").
fn leader(text: &str) -> bool {
    text.contains("....") || text.contains(". . . .") || text.contains("····")
}

fn terminal(text: &str) -> bool {
    text.trim_end()
        .chars()
        .last()
        .is_some_and(|c| ".!?:;\"”’)…]".contains(c))
}

fn cluster_positions(mut values: Vec<f32>, tolerance: f32) -> Vec<(f32, usize)> {
    values.sort_by(f32::total_cmp);
    let mut clusters: Vec<(f32, usize)> = Vec::new();
    for x in values {
        match clusters.last_mut() {
            Some((center, count)) if x - *center <= tolerance => {
                *center = (*center * *count as f32 + x) / (*count as f32 + 1.0);
                *count += 1;
            }
            _ => clusters.push((x, 1)),
        }
    }
    clusters
}

/// Grids drawn with horizontal rules: rows are the bands between rules and
/// columns start where per-column rule segments start (or where text aligns).
fn detect_ruled_tables(
    lines: &[Line],
    usable: &[bool],
    stats: &Stats,
    rules: &[Rect],
) -> Vec<Table> {
    let mut segments: Vec<Rect> = rules
        .iter()
        .filter(|r| r.h <= 2.5 && r.w >= 20.0)
        .copied()
        .collect();
    segments.sort_by(|a, b| a.cy().total_cmp(&b.cy()));
    // Rule rows: segments sharing a y position.
    let mut rows: Vec<(f32, Vec<Rect>)> = Vec::new();
    for segment in segments {
        match rows.last_mut() {
            Some((y, group)) if (segment.cy() - *y).abs() <= 1.5 => group.push(segment),
            _ => rows.push((segment.cy(), vec![segment])),
        }
    }
    let extent = |group: &[Rect]| {
        group
            .iter()
            .copied()
            .reduce(|a, b| a.union(&b))
            .unwrap_or_default()
    };
    let tolerance = stats.body * 0.9;
    let mut tables = Vec::new();
    let mut start = 0;
    while start < rows.len() {
        let frame = extent(&rows[start].1);
        let mut end = start;
        while end + 1 < rows.len() {
            let next = extent(&rows[end + 1].1);
            if next.overlap_x(&frame) < 0.8 * frame.w.max(next.w) {
                break;
            }
            end += 1;
        }
        let run = &rows[start..=end];
        start = end + 1;
        if run.len() < 3 {
            continue;
        }
        let inside = |i: usize, top: f32, bottom: f32| {
            usable[i]
                && lines[i].bbox.cy() > top
                && lines[i].bbox.cy() < bottom
                && lines[i].bbox.overlap_x(&frame) > 0.5 * lines[i].bbox.w
        };
        let ys: Vec<f32> = run.iter().map(|(y, _)| *y).collect();
        // Column starts from per-column rule segments, else recurring left edges.
        let mut anchors: Vec<f32> = cluster_positions(
            run.iter()
                .flat_map(|(_, g)| g.iter().map(|r| r.x))
                .collect(),
            tolerance,
        )
        .into_iter()
        .filter(|(_, count)| *count * 2 >= run.len())
        .map(|(x, _)| x)
        .collect();
        if anchors.len() < 2 {
            let all: Vec<f32> = (0..lines.len())
                .filter(|&i| inside(i, ys[0], ys[ys.len() - 1]))
                .map(|i| lines[i].bbox.x)
                .collect();
            anchors = cluster_positions(all, tolerance)
                .into_iter()
                .filter(|(_, count)| *count * 2 >= (run.len() - 1).max(2))
                .map(|(x, _)| x)
                .collect();
        }
        if anchors.len() < 2 {
            continue;
        }
        let column = |x: f32| -> u32 {
            anchors
                .iter()
                .rposition(|a| *a <= x + tolerance)
                .unwrap_or(0) as u32
        };
        // A band is tabular when text sits side by side in different columns.
        let bands: Vec<(f32, f32, bool)> = ys
            .windows(2)
            .map(|band| {
                let ids: Vec<usize> = (0..lines.len())
                    .filter(|&i| inside(i, band[0], band[1]))
                    .collect();
                let tabular = ids.iter().any(|&i| {
                    ids.iter().any(|&j| {
                        column(lines[i].bbox.x) != column(lines[j].bbox.x)
                            && lines[i].bbox.overlap_x(&lines[j].bbox) <= 0.0
                            && lines[i].bbox.overlap_y(&lines[j].bbox)
                                > 0.5 * lines[i].bbox.h.min(lines[j].bbox.h)
                    })
                });
                (band[0], band[1], tabular)
            })
            .collect();
        let mut heights: Vec<f32> = bands.iter().map(|(a, b, _)| b - a).collect();
        let typical = median(&mut heights);
        // Split at tall non-tabular bands (prose between two separate grids),
        // then trim non-tabular bands from each end.
        let mut groups: Vec<Vec<(f32, f32, bool)>> = vec![Vec::new()];
        for band in bands {
            if !band.2 && band.1 - band.0 > 3.0 * typical {
                groups.push(Vec::new());
            } else if let Some(group) = groups.last_mut() {
                group.push(band);
            }
        }
        for group in groups {
            let (Some(first), Some(last)) = (
                group.iter().position(|b| b.2),
                group.iter().rposition(|b| b.2),
            ) else {
                continue;
            };
            let kept = &group[first..=last];
            if kept.iter().filter(|b| b.2).count() < 2 {
                continue;
            }
            let mut cells: BTreeMap<(u32, u32), Vec<usize>> = BTreeMap::new();
            let mut members = Vec::new();
            for (row, (top, bottom, _)) in kept.iter().enumerate() {
                for i in (0..lines.len()).filter(|&i| inside(i, *top, *bottom)) {
                    cells
                        .entry((row as u32, column(lines[i].bbox.x)))
                        .or_default()
                        .push(i);
                    members.push(i);
                }
            }
            let used_rows: BTreeSet<u32> = cells.keys().map(|(r, _)| *r).collect();
            let renumber: HashMap<u32, u32> = used_rows
                .iter()
                .enumerate()
                .map(|(n, r)| (*r, n as u32))
                .collect();
            let rows_count = used_rows.len() as u32;
            let cols = anchors.len() as u32;
            let bbox = members
                .iter()
                .map(|&i| lines[i].bbox)
                .reduce(|a, b| a.union(&b))
                .unwrap_or_default();
            let bold_header = cells
                .iter()
                .filter(|((r, _), _)| renumber[r] == 0)
                .all(|(_, ids)| ids.iter().all(|&i| lines[i].bold));
            let fill = cells.len() as f32 / (rows_count * cols).max(1) as f32;
            tables.push(Table {
                bbox,
                lines: members,
                rows: rows_count,
                cols,
                cells: cells
                    .into_iter()
                    .map(|((r, c), ids)| (renumber[&r], c, ids))
                    .collect(),
                fill,
                bold_header,
            });
        }
    }
    tables
}

/// Repeated aligned columns of short fragments become candidate tables.
fn detect_tables(lines: &[Line], usable: &[bool], stats: &Stats, page_width: f32) -> Vec<Table> {
    let tolerance = stats.body * 0.6;
    let mut order: Vec<usize> = (0..lines.len()).filter(|&i| usable[i]).collect();
    order.sort_by(|a, b| lines[*a].bbox.cy().total_cmp(&lines[*b].bbox.cy()));
    // Group fragments that share a baseline band into rows.
    let mut rows: Vec<Vec<usize>> = Vec::new();
    for index in order {
        let line = &lines[index];
        match rows.last_mut() {
            Some(row)
                if (lines[row[0]].bbox.cy() - line.bbox.cy()).abs()
                    <= tolerance.max(line.bbox.h * 0.4) =>
            {
                row.push(index)
            }
            _ => rows.push(vec![index]),
        }
    }
    for row in &mut rows {
        row.sort_by(|a, b| lines[*a].bbox.x.total_cmp(&lines[*b].bbox.x));
    }
    let mut tables = Vec::new();
    let mut start = 0;
    while start < rows.len() {
        if rows[start].len() < 2 {
            start += 1;
            continue;
        }
        // Extend a run of multi-fragment rows, admitting single-fragment rows
        // (wrapped cell text) between them when the vertical rhythm continues.
        let mut end = start;
        let mut multi = 1;
        let mut last_multi = start;
        let mut next = start + 1;
        while next < rows.len() {
            let gap = lines[rows[next][0]].bbox.y - lines[rows[next - 1][0]].bbox.bottom();
            if gap > stats.gap + stats.body * 1.6 {
                break;
            }
            if rows[next].len() >= 2 {
                multi += 1;
                last_multi = next;
                end = next;
            } else if next - last_multi > 2 {
                break;
            }
            next += 1;
        }
        if multi < 3 {
            start += 1;
            continue;
        }
        let run = &rows[start..=end];
        // Column anchors: left edges that recur across the run's multi rows.
        let mut starts: Vec<f32> = run
            .iter()
            .filter(|row| row.len() >= 2)
            .flat_map(|row| row.iter().map(|&i| lines[i].bbox.x))
            .collect();
        starts.sort_by(f32::total_cmp);
        let mut clusters: Vec<(f32, usize)> = Vec::new();
        for x in starts {
            match clusters.last_mut() {
                Some((center, count)) if x - *center <= tolerance * 1.5 => {
                    *center = (*center * *count as f32 + x) / (*count as f32 + 1.0);
                    *count += 1;
                }
                _ => clusters.push((x, 1)),
            }
        }
        let anchors: Vec<f32> = clusters
            .iter()
            .filter(|(_, count)| *count * 2 >= multi)
            .map(|(x, _)| *x)
            .collect();
        let aligned = run
            .iter()
            .filter(|row| row.len() >= 2)
            .filter(|row| {
                row.iter().all(|&i| {
                    anchors
                        .iter()
                        .any(|a| (lines[i].bbox.x - a).abs() <= tolerance * 1.5)
                })
            })
            .count();
        let mut lengths: Vec<f32> = run
            .iter()
            .flatten()
            .map(|&i| lines[i].chars as f32)
            .collect();
        let long_fragments = median(&mut lengths) > 60.0;
        // Text columns whose baselines happen to align are not tables: each
        // column's wrapped lines are wide and nearly the same width.
        let prose_columns = anchors.len() >= 2
            && median(&mut lengths) >= 25.0
            && anchors.iter().enumerate().all(|(index, anchor)| {
                let next = anchors.get(index + 1).copied().unwrap_or(f32::MAX);
                let mut widths: Vec<f32> = run
                    .iter()
                    .flatten()
                    .filter(|&&i| {
                        lines[i].bbox.x >= anchor - tolerance * 1.5
                            && lines[i].bbox.x < next - tolerance * 1.5
                    })
                    .map(|&i| lines[i].bbox.w)
                    .collect();
                let widest = widths.iter().copied().fold(0.0, f32::max);
                let full = widths.iter().filter(|w| **w >= widest * 0.8).count();
                !widths.is_empty()
                    && median(&mut widths) >= page_width * 0.22
                    && full * 10 >= widths.len() * 6
            });
        if anchors.len() < 2 || aligned * 10 < multi * 7 || long_fragments || prose_columns {
            start = end + 1;
            continue;
        }
        let column = |x: f32| -> u32 {
            anchors
                .iter()
                .rposition(|a| *a <= x + tolerance * 1.5)
                .unwrap_or(0) as u32
        };
        let mut cells: BTreeMap<(u32, u32), Vec<usize>> = BTreeMap::new();
        let mut row_number: i64 = -1;
        let mut members = Vec::new();
        for row in run {
            let first_column = column(lines[row[0]].bbox.x);
            if row.len() >= 2 || first_column == 0 || row_number < 0 {
                row_number += 1;
            }
            for &i in row {
                cells
                    .entry((row_number as u32, column(lines[i].bbox.x)))
                    .or_default()
                    .push(i);
                members.push(i);
            }
        }
        let rows_count = row_number as u32 + 1;
        let cols = anchors.len() as u32;
        let fill = cells.len() as f32 / (rows_count * cols).max(1) as f32;
        let bbox = members
            .iter()
            .map(|&i| lines[i].bbox)
            .reduce(|a, b| a.union(&b))
            .unwrap_or_default();
        let bold_header = cells
            .iter()
            .filter(|((r, _), _)| *r == 0)
            .all(|(_, ids)| ids.iter().all(|&i| lines[i].bold));
        tables.push(Table {
            bbox,
            lines: members,
            rows: rows_count,
            cols,
            cells: cells.into_iter().map(|((r, c), ids)| (r, c, ids)).collect(),
            fill,
            bold_header,
        });
        start = end + 1;
    }
    tables
}

fn atom_box(atom: &Atom, lines: &[Line], tables: &[Table]) -> Rect {
    match atom {
        Atom::Line(i) => lines[*i].bbox,
        Atom::Table(i) => tables[*i].bbox,
        Atom::Picture(r) => *r,
    }
}

/// Recursive XY-cut. A vertical gutter is preferred so multi-column text reads
/// down each column; horizontal cuts only at gaps wider than ordinary leading.
fn xy_cut(
    ids: Vec<usize>,
    atoms: &[Atom],
    lines: &[Line],
    tables: &[Table],
    stats: &Stats,
    leaf: &mut u32,
    out: &mut Vec<(usize, u32)>,
) {
    if ids.len() <= 1 {
        out.extend(ids.into_iter().map(|i| (i, *leaf)));
        *leaf += 1;
        return;
    }
    let boxes: Vec<Rect> = ids
        .iter()
        .map(|&i| atom_box(&atoms[i], lines, tables))
        .collect();
    // Vertical cut.
    let mut by_x: Vec<usize> = (0..ids.len()).collect();
    by_x.sort_by(|a, b| boxes[*a].x.total_cmp(&boxes[*b].x));
    let mut best: Option<(f32, f32)> = None;
    let mut reach = boxes[by_x[0]].right();
    for &k in &by_x[1..] {
        let gap = boxes[k].x - reach;
        if gap >= (stats.body * 1.2).max(8.0) && best.is_none_or(|(g, _)| gap > g) {
            best = Some((gap, boxes[k].x));
        }
        reach = reach.max(boxes[k].right());
    }
    if let Some((_, cut)) = best {
        let (left, right): (Vec<usize>, Vec<usize>) =
            (0..ids.len()).partition(|&k| boxes[k].x < cut);
        let substantial = |side: &[usize]| {
            let mut lengths: Vec<f32> = side
                .iter()
                .map(|&k| match atoms[ids[k]] {
                    Atom::Line(i) => lines[i].chars as f32,
                    _ => 100.0,
                })
                .collect();
            side.len() >= 2 && median(&mut lengths) >= 12.0
        };
        if substantial(&left) && substantial(&right) {
            for side in [left, right] {
                xy_cut(
                    side.into_iter().map(|k| ids[k]).collect(),
                    atoms,
                    lines,
                    tables,
                    stats,
                    leaf,
                    out,
                );
            }
            return;
        }
    }
    // Horizontal cuts at clearly separated bands.
    let mut by_y: Vec<usize> = (0..ids.len()).collect();
    by_y.sort_by(|a, b| boxes[*a].y.total_cmp(&boxes[*b].y));
    let threshold = stats.gap + stats.body * 0.9;
    let mut bands: Vec<Vec<usize>> = vec![vec![by_y[0]]];
    let mut reach = boxes[by_y[0]].bottom();
    for &k in &by_y[1..] {
        if boxes[k].y - reach > threshold {
            bands.push(Vec::new());
        }
        reach = reach.max(boxes[k].bottom());
        if let Some(band) = bands.last_mut() {
            band.push(k);
        }
    }
    if bands.len() > 1 {
        for band in bands {
            xy_cut(
                band.into_iter().map(|k| ids[k]).collect(),
                atoms,
                lines,
                tables,
                stats,
                leaf,
                out,
            );
        }
        return;
    }
    // Leaf: top-to-bottom, then left-to-right within a shared baseline band.
    let mut leaf_ids: Vec<usize> = (0..ids.len()).collect();
    leaf_ids.sort_by(|a, b| {
        let (ra, rb) = (&boxes[*a], &boxes[*b]);
        if ra.overlap_y(rb) > 0.5 * ra.h.min(rb.h) {
            ra.x.total_cmp(&rb.x)
        } else {
            ra.y.total_cmp(&rb.y)
        }
    });
    out.extend(leaf_ids.into_iter().map(|k| (ids[k], *leaf)));
    *leaf += 1;
}

fn join_lines(lines: &[&Line]) -> String {
    let mut text = String::new();
    for (index, line) in lines.iter().enumerate() {
        let mut piece = line.text.as_str();
        let hyphenated = line.hyphen_end && index + 1 < lines.len();
        if hyphenated {
            piece = piece.strip_suffix(['-', '\u{ad}']).unwrap_or(piece);
        }
        if index > 0 && !lines[index - 1].hyphen_end {
            text.push(' ');
        }
        text.push_str(piece);
    }
    text
}

struct Block {
    lines: Vec<usize>,
    leaf: u32,
}

fn typical_pitch(pages: &[(Vec<Line>, Vec<bool>)]) -> f32 {
    let mut ratios = Vec::new();
    for (lines, _) in pages {
        for pair in lines.windows(2) {
            let (a, b) = (&pair[0], &pair[1]);
            let size = a.size.max(b.size);
            if (a.size - b.size).abs() < 0.5 && a.bbox.overlap_x(&b.bbox) > 0.0 && size > 0.0 {
                let ratio = (b.baseline - a.baseline) / size;
                if (0.8..3.0).contains(&ratio) {
                    ratios.push(ratio);
                }
            }
        }
    }
    // Paragraph breaks can outnumber line breaks in reference material, so the
    // ordinary line pitch is the lower cluster, not the median.
    lower_quartile(&mut ratios).unwrap_or(1.2)
}

fn lower_quartile(values: &mut [f32]) -> Option<f32> {
    if values.is_empty() {
        return None;
    }
    values.sort_by(f32::total_cmp);
    Some(values[values.len() / 4])
}

fn typical_gap(pages: &[(Vec<Line>, Vec<bool>)], body: f32) -> f32 {
    let mut gaps = Vec::new();
    for (lines, _) in pages {
        for pair in lines.windows(2) {
            let (a, b) = (&pair[0], &pair[1]);
            if (a.size - b.size).abs() < 0.5 && a.bbox.overlap_x(&b.bbox) > 0.0 {
                let gap = b.bbox.y - a.bbox.bottom();
                if gap > -0.2 * body && gap < 1.5 * body {
                    gaps.push(gap);
                }
            }
        }
    }
    lower_quartile(&mut gaps).map_or(body * 0.3, |gap| gap.max(0.0))
}

pub fn reconstruct(input: &DocumentInput) -> Document {
    let body = body_size(input);
    let page_lines: Vec<(Vec<Line>, Vec<bool>)> = input
        .pages
        .iter()
        .map(|page| {
            let lines = build_lines(page, &input.fonts);
            let flags = vec![true; lines.len()];
            (lines, flags)
        })
        .collect();
    let stats = Stats {
        body,
        gap: typical_gap(&page_lines, body),
        pitch: typical_pitch(&page_lines),
    };

    // Page furniture: margin lines that are page numbers or recur across pages.
    let band = |page: &PageInput, line: &Line| -> Option<bool> {
        let cy = line.bbox.cy();
        if cy < page.height * 0.1 {
            Some(true)
        } else if cy > page.height * 0.9 {
            Some(false)
        } else {
            None
        }
    };
    let mut recurrence: HashMap<String, std::collections::HashSet<usize>> = HashMap::new();
    for (p, (lines, _)) in page_lines.iter().enumerate() {
        for line in lines {
            if band(&input.pages[p], line).is_some()
                && line.chars <= 160
                && folio_value(&line.text).is_none()
                && !roman_folio(&line.text)
            {
                recurrence
                    .entry(furniture_key(&line.text))
                    .or_default()
                    .insert(p);
            }
        }
    }
    let threshold = 3.max(input.pages.len() / 10);
    // Real folios keep a constant offset from the page index; table-of-contents
    // numbers near a margin do not.
    let mut offsets: HashMap<i64, usize> = HashMap::new();
    for (p, (lines, _)) in page_lines.iter().enumerate() {
        for line in lines {
            if band(&input.pages[p], line).is_some()
                && let Some(value) = folio_value(&line.text)
            {
                *offsets.entry(value - p as i64).or_default() += 1;
            }
        }
    }
    let folio_offset = offsets
        .into_iter()
        .filter(|(_, count)| *count >= 3)
        .max_by_key(|(offset, count)| (*count, -offset.abs()))
        .map(|(offset, _)| offset);

    let mut elements: Vec<Element> = Vec::new();
    let mut routing = Vec::new();
    let mut summaries = Vec::new();
    let mut sized_headings: Vec<f32> = Vec::new();
    let mut heading_slots: Vec<(usize, f32, bool)> = Vec::new();

    for (p, page) in input.pages.iter().enumerate() {
        let lines = &page_lines[p].0;
        let furniture: Vec<Furniture> = lines
            .iter()
            .map(|line| match band(page, line) {
                None => Furniture::None,
                Some(top) => {
                    let folio = folio_value(&line.text)
                        .is_some_and(|v| Some(v - p as i64) == folio_offset)
                        || (roman_folio(&line.text) && p < 40);
                    if folio {
                        Furniture::PageNumber
                    } else if recurrence
                        .get(&furniture_key(&line.text))
                        .is_some_and(|pages| pages.len() >= threshold)
                    {
                        if top {
                            Furniture::Header
                        } else {
                            Furniture::Footer
                        }
                    } else {
                        Furniture::None
                    }
                }
            })
            .collect();
        // A margin line sharing its row with body text belongs to the body.
        let furniture: Vec<Furniture> = (0..lines.len())
            .map(|i| {
                if furniture[i] != Furniture::None
                    && (0..lines.len()).any(|j| {
                        j != i
                            && furniture[j] == Furniture::None
                            && lines[i].bbox.overlap_y(&lines[j].bbox)
                                > 0.5 * lines[i].bbox.h.min(lines[j].bbox.h)
                    })
                {
                    Furniture::None
                } else {
                    furniture[i]
                }
            })
            .collect();
        let shares_row: Vec<bool> = (0..lines.len())
            .map(|i| {
                (0..lines.len()).any(|j| {
                    j != i
                        && lines[i].bbox.overlap_y(&lines[j].bbox)
                            > 0.5 * lines[i].bbox.h.min(lines[j].bbox.h)
                })
            })
            .collect();
        let page_area = (page.width * page.height).max(1.0);
        let pictures: Vec<Rect> = page
            .images
            .iter()
            .filter(|r| r.w >= 24.0 && r.h >= 24.0 && r.area() >= page_area * 0.015)
            .filter(|r| !(r.area() >= page_area * 0.8 && page.words.len() > 50))
            .copied()
            .collect();
        let usable: Vec<bool> = furniture.iter().map(|f| *f == Furniture::None).collect();
        let mut tables = detect_ruled_tables(lines, &usable, &stats, &page.rules);
        let mut open = usable.clone();
        for table in &tables {
            for &i in &table.lines {
                open[i] = false;
            }
        }
        tables.extend(detect_tables(lines, &open, &stats, page.width));
        let mut in_table = vec![false; lines.len()];
        for table in &tables {
            for &i in &table.lines {
                in_table[i] = true;
            }
        }
        let mut atoms = Vec::new();
        for (i, used) in usable.iter().enumerate() {
            if *used && !in_table[i] {
                atoms.push(Atom::Line(i));
            }
        }
        atoms.extend((0..tables.len()).map(Atom::Table));
        atoms.extend(pictures.iter().map(|r| Atom::Picture(*r)));
        let mut ordered = Vec::new();
        let mut leaf = 0;
        xy_cut(
            (0..atoms.len()).collect(),
            &atoms,
            lines,
            &tables,
            &stats,
            &mut leaf,
            &mut ordered,
        );

        let page_number = page.number;
        let mut counter = 0u32;
        let mut push = |elements: &mut Vec<Element>, mut element: Element| {
            element.id = format!("p{page_number}-e{counter}");
            counter += 1;
            element.order = elements.len() as u32;
            elements.push(element);
        };
        let blank = |role: Role, bbox: Rect| Element {
            id: String::new(),
            page: page_number,
            role,
            level: None,
            text: String::new(),
            bbox,
            order: 0,
            font_size: 0.0,
            bold: false,
            cells: Vec::new(),
            rows: 0,
            cols: 0,
            continues: false,
            origin: Origin::Native,
            confidence: 1.0,
            words: Vec::new(),
        };
        let furniture_element = |i: usize, role: Role| {
            let line = &lines[i];
            let mut e = blank(role, line.bbox);
            e.text = line.text.clone();
            e.font_size = line.size;
            e.bold = line.bold;
            e.words = line.words.clone();
            e
        };
        let mut top: Vec<usize> = (0..lines.len())
            .filter(|&i| furniture[i] != Furniture::None && band(page, &lines[i]) == Some(true))
            .collect();
        top.sort_by(|a, b| lines[*a].bbox.y.total_cmp(&lines[*b].bbox.y));
        for i in top {
            let role = match furniture[i] {
                Furniture::PageNumber => Role::PageNumber,
                _ => Role::Header,
            };
            push(&mut elements, furniture_element(i, role));
        }

        // Group ordered lines into blocks; tables and pictures stand alone.
        let mut blocks: Vec<Block> = Vec::new();
        let mut sequence: Vec<Result<usize, usize>> = Vec::new(); // Ok(block) / Err(atom)
        for &(atom, leaf_id) in &ordered {
            match atoms[atom] {
                Atom::Line(i) => {
                    let line = &lines[i];
                    let continues = blocks.last().is_some_and(|block| {
                        matches!(sequence.last(), Some(Ok(_))) && block.leaf == leaf_id && {
                            let first = &lines[block.lines[0]];
                            let previous = &lines[*block.lines.last().unwrap_or(&i)];
                            let size = previous.size.max(line.size);
                            let gap = line.bbox.y - previous.bbox.bottom();
                            let pitch = line.baseline - previous.baseline;
                            let in_list = list_marker(&first.text).is_some();
                            let hanging = first.text_x;
                            gap <= stats.gap + 0.6 * size
                                && gap > -0.5 * size
                                && pitch > 0.5 * size
                                && pitch <= stats.pitch * size * 1.22
                                && (previous.size - line.size).abs() <= 0.15 * size
                                && previous.bold == line.bold
                                && previous.mono == line.mono
                                && list_marker(&line.text).is_none()
                                && previous.bbox.overlap_x(&line.bbox) > 0.0
                                && !(line.bbox.x > previous.bbox.x + size
                                    && terminal(&previous.text)
                                    && !in_list)
                                && !(in_list && line.bbox.x < hanging - 0.5 * size)
                                && !previous.leader
                        }
                    });
                    if continues {
                        if let Some(block) = blocks.last_mut() {
                            block.lines.push(i);
                        }
                    } else {
                        blocks.push(Block {
                            lines: vec![i],
                            leaf: leaf_id,
                        });
                        sequence.push(Ok(blocks.len() - 1));
                    }
                }
                _ => sequence.push(Err(atom)),
            }
        }

        let mut table_uncertain = false;
        for item in sequence {
            match item {
                Err(atom) => match &atoms[atom] {
                    Atom::Picture(r) => push(&mut elements, blank(Role::Picture, *r)),
                    Atom::Table(t) => {
                        let table = &tables[*t];
                        let mut e = blank(Role::Table, table.bbox);
                        e.rows = table.rows;
                        e.cols = table.cols;
                        e.bold = table.bold_header;
                        e.confidence = table.fill.clamp(0.0, 1.0);
                        if table.fill < 0.65 {
                            table_uncertain = true;
                        }
                        let mut rows_text: BTreeMap<u32, Vec<String>> = BTreeMap::new();
                        for (r, c, ids) in &table.cells {
                            let mut refs: Vec<&Line> = ids.iter().map(|&i| &lines[i]).collect();
                            refs.sort_by(|a, b| a.baseline.total_cmp(&b.baseline));
                            // Stacked items inside a cell stay separate lines.
                            let mut items: Vec<Vec<&Line>> = Vec::new();
                            for line in &refs {
                                let split = items.last().and_then(|item| item.last()).is_none_or(
                                    |previous| {
                                        line.baseline - previous.baseline
                                            > stats.pitch * previous.size.max(line.size) * 1.22
                                    },
                                );
                                if split {
                                    items.push(vec![*line]);
                                } else if let Some(item) = items.last_mut() {
                                    item.push(*line);
                                }
                            }
                            let text = items
                                .iter()
                                .map(|item| join_lines(item))
                                .collect::<Vec<_>>()
                                .join("\n");
                            let bbox = refs
                                .iter()
                                .map(|l| l.bbox)
                                .reduce(|a, b| a.union(&b))
                                .unwrap_or_default();
                            let words: Vec<u32> =
                                refs.iter().flat_map(|l| l.words.iter().copied()).collect();
                            rows_text.entry(*r).or_default().push(text.clone());
                            e.words.extend(words.iter().copied());
                            e.cells.push(Cell {
                                row: *r,
                                col: *c,
                                text,
                                bbox,
                                words,
                            });
                        }
                        e.text = rows_text
                            .values()
                            .map(|cells| cells.join(" | "))
                            .collect::<Vec<_>>()
                            .join("\n");
                        e.font_size = body;
                        push(&mut elements, e);
                    }
                    Atom::Line(_) => {}
                },
                Ok(b) => {
                    let block = &blocks[b];
                    let refs: Vec<&Line> = block.lines.iter().map(|&i| &lines[i]).collect();
                    let mut sizes: Vec<f32> = refs.iter().map(|l| l.size).collect();
                    let size = median(&mut sizes);
                    let bold = refs.iter().all(|l| l.bold);
                    let mono = refs.iter().filter(|l| l.mono).count() * 10 >= refs.len() * 6;
                    let bbox = refs
                        .iter()
                        .map(|l| l.bbox)
                        .reduce(|a, b| a.union(&b))
                        .unwrap_or_default();
                    let text = if mono {
                        refs.iter()
                            .map(|l| l.text.as_str())
                            .collect::<Vec<_>>()
                            .join("\n")
                    } else {
                        join_lines(&refs)
                    };
                    let chars = text.chars().count();
                    let standalone = !block.lines.iter().any(|&i| shares_row[i])
                        && text.chars().any(char::is_alphabetic);
                    let leaders = refs.iter().any(|l| l.leader);
                    // Larger than body text, or a short bold line that does not
                    // read as a sentence.
                    let sized_heading = size >= body * 1.18 && chars <= 200 && refs.len() <= 3;
                    let bold_heading = bold
                        && refs.len() <= 2
                        && chars <= 80
                        && size >= body * 0.95
                        && !text.trim_end().ends_with([',', '.', ';', ':'])
                        && !text.trim_end().ends_with(['"', '”', ')']);
                    let role = if mono {
                        Role::Code
                    } else if list_marker(&text).is_some() {
                        Role::ListItem
                    } else if standalone && !leaders && (sized_heading || bold_heading) {
                        Role::Heading
                    } else if chars <= 160 && caption_like(&text) {
                        Role::Caption
                    } else {
                        Role::Paragraph
                    };
                    let mut e = blank(role, bbox);
                    e.text = text;
                    e.font_size = size;
                    e.bold = bold;
                    e.words = refs.iter().flat_map(|l| l.words.iter().copied()).collect();
                    if role == Role::Heading {
                        let sized = size >= body * 1.18;
                        if sized {
                            sized_headings.push((size * 2.0).round() / 2.0);
                        }
                        heading_slots.push((elements.len(), size, sized));
                    }
                    push(&mut elements, e);
                }
            }
        }

        let mut bottom: Vec<usize> = (0..lines.len())
            .filter(|&i| furniture[i] != Furniture::None && band(page, &lines[i]) == Some(false))
            .collect();
        bottom.sort_by(|a, b| lines[*a].bbox.y.total_cmp(&lines[*b].bbox.y));
        for i in bottom {
            let role = match furniture[i] {
                Furniture::PageNumber => Role::PageNumber,
                _ => Role::Footer,
            };
            push(&mut elements, furniture_element(i, role));
        }

        // Routing: where would intelligence earn its cost on this page?
        let alnum_chars: usize = page
            .words
            .iter()
            .map(|w| w.text.chars().filter(|c| c.is_alphanumeric()).count())
            .sum();
        let mut reasons = Vec::new();
        let mut tier = Tier::Deterministic;
        let image_cover: f32 = page.images.iter().map(|r| r.area()).sum::<f32>() / page_area;
        if alnum_chars < 16 {
            tier = Tier::Ocr;
            reasons.push(if image_cover > 0.3 {
                "scanned or image-only page: no usable text layer".to_owned()
            } else {
                "no usable text layer".to_owned()
            });
        } else if page.bad_chars * 50 > page.text_chars.max(1) {
            tier = Tier::Ocr;
            reasons.push("text layer contains undecodable characters".to_owned());
        }
        let meaningful: Vec<&Rect> = pictures
            .iter()
            .filter(|r| r.area() >= page_area * 0.06)
            .collect();
        if !meaningful.is_empty() {
            if tier < Tier::Vision {
                tier = Tier::Vision;
            }
            reasons.push(format!(
                "{} picture(s) may carry information not in the text layer",
                meaningful.len()
            ));
        }
        if table_uncertain {
            if tier < Tier::Review {
                tier = Tier::Review;
            }
            reasons.push("sparse table grid: cell structure needs confirmation".to_owned());
        }
        if reasons.is_empty() {
            reasons.push("native text layer reconstructed deterministically".to_owned());
        }
        routing.push(Route {
            page: page.number,
            tier,
            reasons,
        });
        summaries.push(PageSummary {
            number: page.number,
            width: page.width,
            height: page.height,
            words: page.words.len() as u32,
            tier,
        });
    }

    // A short bold line set below body size that introduces regular text of
    // the same size is a label heading ("New entries" above a small list).
    // Body-size bold lines are already judged by the block rules above.
    for index in 0..elements.len().saturating_sub(1) {
        let (current, next) = (&elements[index], &elements[index + 1]);
        if current.role == Role::Paragraph
            && current.bold
            && current.font_size < body * 0.95
            && current.page == next.page
            && matches!(next.role, Role::Paragraph | Role::ListItem | Role::Table)
            && !next.bold
            && next.font_size <= current.font_size + 0.25
            && current.text.chars().count() <= 60
            && !current.text.contains('\n')
            && !current
                .text
                .trim_end()
                .ends_with([',', '.', ';', ':', '"', '”', ')'])
            && !leader(&current.text)
            && list_marker(&current.text).is_none()
        {
            let size = current.font_size;
            elements[index].role = Role::Heading;
            heading_slots.push((index, size, false));
        }
    }

    // Heading levels from distinct sizes; the largest rare size near the start is the title.
    sized_headings.sort_by(|a, b| b.total_cmp(a));
    sized_headings.dedup();
    let largest = sized_headings.first().copied();
    let is_title = |page: u32, size: f32, sized: bool| {
        sized && Some((size * 2.0).round() / 2.0) == largest && page <= 2 && size >= body * 1.5
    };
    // Ranks exclude a size used only by the title, so chapters start at level 1.
    let title_only = largest.filter(|top| {
        heading_slots.iter().all(|(index, size, sized)| {
            (size * 2.0).round() / 2.0 != *top || is_title(elements[*index].page, *size, *sized)
        })
    });
    // Only recurring sizes define levels; a one-off size takes the level of the
    // nearest recurring size at or below it.
    let mut uses: HashMap<i32, usize> = HashMap::new();
    for (_, size, sized) in &heading_slots {
        if *sized {
            *uses.entry((size * 2.0).round() as i32).or_default() += 1;
        }
    }
    let ranks: Vec<f32> = sized_headings
        .iter()
        .copied()
        .filter(|s| Some(*s) != title_only)
        .filter(|s| uses.get(&((s * 2.0).round() as i32)).copied().unwrap_or(0) >= 2)
        .collect();
    for (index, size, sized) in heading_slots {
        let rounded = (size * 2.0).round() / 2.0;
        let element = &mut elements[index];
        if is_title(element.page, size, sized) {
            element.role = Role::Title;
            element.level = None;
            continue;
        }
        let rank = if sized {
            ranks
                .iter()
                .position(|s| *s <= rounded)
                .unwrap_or(ranks.len().saturating_sub(1))
        } else {
            ranks.len()
        };
        element.level = Some((rank as u8 + 1).min(5));
    }

    // Paragraphs that run across a page break continue into the next body element.
    let body_indices: Vec<usize> = (0..elements.len())
        .filter(|&i| !elements[i].role.furniture())
        .collect();
    for pair in body_indices.windows(2) {
        let (a, b) = (pair[0], pair[1]);
        if elements[a].page != elements[b].page
            && elements[a].role == Role::Paragraph
            && elements[b].role == Role::Paragraph
            && !terminal(&elements[a].text)
            && (elements[b]
                .text
                .chars()
                .next()
                .is_some_and(|c| c.is_lowercase())
                || elements[a].text.ends_with(',')
                || elements[a].text.ends_with('-'))
        {
            elements[a].continues = true;
        }
    }

    // Every native word must land in exactly one element: silent loss or
    // duplication is a technical failure, never an editorial choice.
    let mut seen: Vec<Vec<u8>> = input.pages.iter().map(|p| vec![0; p.words.len()]).collect();
    for element in &elements {
        let page = element.page as usize - 1;
        let refs: Box<dyn Iterator<Item = &u32>> = if element.cells.is_empty() {
            Box::new(element.words.iter())
        } else {
            Box::new(element.cells.iter().flat_map(|c| c.words.iter()))
        };
        for &word in refs {
            if let Some(count) = seen.get_mut(page).and_then(|p| p.get_mut(word as usize)) {
                *count = count.saturating_add(1);
            }
        }
    }
    let unassigned = seen.iter().flatten().filter(|c| **c == 0).count() as u32;
    let duplicated = seen.iter().flatten().filter(|c| **c > 1).count() as u32;

    let mut roles = BTreeMap::new();
    for element in &elements {
        *roles
            .entry(
                serde_json::to_value(element.role)
                    .ok()
                    .and_then(|v| v.as_str().map(str::to_owned))
                    .unwrap_or_default(),
            )
            .or_insert(0u32) += 1;
    }
    let mut tiers = BTreeMap::new();
    for route in &routing {
        *tiers
            .entry(
                serde_json::to_value(route.tier)
                    .ok()
                    .and_then(|v| v.as_str().map(str::to_owned))
                    .unwrap_or_default(),
            )
            .or_insert(0u32) += 1;
    }
    Document {
        schema: SCHEMA.to_owned(),
        format: input.format.clone(),
        stats: super::Stats {
            pages: input.pages.len() as u32,
            words: input.pages.iter().map(|p| p.words.len() as u32).sum(),
            elements: elements.len() as u32,
            body_font_size: body,
            line_pitch: stats.pitch,
            unassigned_words: unassigned,
            duplicated_words: duplicated,
            line_gap: stats.gap,
            roles,
            tiers,
        },
        pages: summaries,
        elements,
        routing,
    }
}

fn caption_like(text: &str) -> bool {
    let lower = text.trim_start().to_lowercase();
    ["figure ", "fig. ", "table ", "chart ", "exhibit ", "image "]
        .iter()
        .any(|prefix| {
            lower
                .strip_prefix(prefix)
                .is_some_and(|rest| rest.chars().next().is_some_and(|c| c.is_ascii_digit()))
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn word(text: &str, x: f32, y: f32, line: u32, size: f32) -> Word {
        let w = text.chars().count() as f32 * size * 0.5;
        Word {
            text: text.into(),
            bbox: Rect::new(x, y, w, size * 0.7),
            font: 0,
            size,
            line,
            baseline: y + size * 0.7,
            hyphen: false,
            origin: Origin::Native,
            confidence: 1.0,
        }
    }

    /// Two text columns, a footer folio and a page break: every word lands in
    /// exactly one element, columns read top-to-bottom left before right, and
    /// the folio is furniture. Neither benchmark document is multi-column.
    #[test]
    fn two_columns_read_down_each_column_without_loss() {
        let mut pages = Vec::new();
        for number in 1..=3u32 {
            let mut words = Vec::new();
            let mut line = 0;
            for (column, x) in [(0, 72.0), (1, 320.0)] {
                for row in 0..12 {
                    let y = 100.0 + row as f32 * 14.0;
                    // Ordinary word spacing (a quarter em) across a ~200pt column.
                    let mut cursor = x;
                    for k in 0..6 {
                        let text = format!("c{column}r{row}w{k}");
                        let w = word(&text, cursor, y, line, 10.0);
                        cursor = w.bbox.right() + 2.5;
                        words.push(w);
                    }
                    line += 1;
                }
            }
            words.push(word(&number.to_string(), 300.0, 760.0, line, 8.0));
            pages.push(PageInput {
                number,
                width: 612.0,
                height: 792.0,
                text_chars: 400,
                words,
                ..Default::default()
            });
        }
        let input = DocumentInput {
            format: "test".into(),
            fonts: vec![Font::default()],
            pages,
        };
        let document = reconstruct(&input);
        assert_eq!(document.stats.unassigned_words, 0);
        assert_eq!(document.stats.duplicated_words, 0);
        let page_one: Vec<&Element> = document
            .elements
            .iter()
            .filter(|e| e.page == 1 && !e.role.furniture())
            .collect();
        let text: String = page_one
            .iter()
            .map(|e| e.text.as_str())
            .collect::<Vec<_>>()
            .join(" ");
        let left_end = text.find("c0r11w5").expect("left column present");
        let right_start = text.find("c1r0w0").expect("right column present");
        assert!(
            left_end < right_start,
            "left column must be read before the right: {:?}",
            page_one
                .iter()
                .map(|e| (e.role, e.text.chars().take(30).collect::<String>()))
                .collect::<Vec<_>>()
        );
        assert!(
            document
                .elements
                .iter()
                .filter(|e| e.role == Role::PageNumber)
                .count()
                == 3
        );
    }
}
