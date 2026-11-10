//! Table construction: cells from a drawn grid, or rows and columns from
//! aligned text when no grid was drawn.
use super::Rect;
use super::geom::{cluster, free_of, intersect, median, merge_intervals, quantile, union_all};
use super::lattice::Grid;
use super::text::{Ctx, Line, lines_from};

#[derive(Debug, Clone)]
pub(crate) struct TableCell {
    pub row: u32,
    pub col: u32,
    pub rowspan: u32,
    pub colspan: u32,
    pub header: bool,
    pub words: Vec<u32>,
    pub rect: Rect,
}

#[derive(Debug, Clone)]
pub(crate) struct TableNode {
    pub bbox: Rect,
    pub rows: u32,
    pub cols: u32,
    pub cells: Vec<TableCell>,
    /// Cell boundaries come from lines drawn in the file.
    pub ruled: bool,
    pub confidence: f32,
}

impl TableNode {
    pub fn words(&self) -> impl Iterator<Item = u32> + '_ {
        self.cells.iter().flat_map(|c| c.words.iter().copied())
    }
}

/// Fill a drawn grid with the words that lie inside its cells.
pub(crate) fn from_grid(ctx: &Ctx, grid: &Grid, available: &[bool]) -> Option<TableNode> {
    let area = grid.bbox.grow(1.5);
    let inside: Vec<u32> = (0..ctx.page.words.len() as u32)
        .filter(|id| available[*id as usize] && area.contains_center(&ctx.word(*id).bbox))
        .collect();
    if inside.len() < 2 {
        return None;
    }
    if grid.rows_only {
        return from_row_bands(ctx, grid, &inside);
    }
    let mut cells: Vec<TableCell> = grid
        .cells
        .iter()
        .map(|c| TableCell {
            row: c.row,
            col: c.col,
            rowspan: c.rowspan,
            colspan: c.colspan,
            header: false,
            words: Vec::new(),
            rect: c.rect,
        })
        .collect();
    for id in inside {
        let bbox = ctx.word(id).bbox;
        let home = cells
            .iter_mut()
            .filter(|c| c.rect.grow(0.5).contains_center(&bbox))
            .min_by(|a, b| a.rect.area().total_cmp(&b.rect.area()));
        if let Some(cell) = home {
            cell.words.push(id);
        } else if let Some(cell) = cells.iter_mut().min_by(|a, b| {
            let distance = |c: &TableCell| (c.rect.cx() - bbox.cx()).hypot(c.rect.cy() - bbox.cy());
            distance(a).total_cmp(&distance(b))
        }) {
            cell.words.push(id);
        }
    }
    let filled = cells.iter().filter(|c| !c.words.is_empty()).count();
    // One framed block of text is a callout, not a table.
    if filled < 2 || cells.len() < 2 {
        return None;
    }
    // A grid whose text sits in very few cells is chart scaffolding.
    if cells.len() >= 12 && filled * 5 < cells.len() {
        return None;
    }
    let mut table = TableNode {
        bbox: grid.bbox,
        rows: grid.rows,
        cols: grid.cols,
        cells,
        ruled: true,
        confidence: 0.95,
    };
    drop_empty_tracks(&mut table);
    mark_header(&mut table);
    Some(table)
}

/// Remove rows and columns in which no cell starts with content, renumbering
/// the rest. Drawn double borders and spacer tracks otherwise become empty cells.
fn drop_empty_tracks(table: &mut TableNode) {
    let (rows, cols) = (table.rows as usize, table.cols as usize);
    let mut row_used = vec![false; rows];
    let mut col_used = vec![false; cols];
    for cell in table.cells.iter().filter(|c| !c.words.is_empty()) {
        for r in cell.row..cell.row + cell.rowspan {
            if let Some(slot) = row_used.get_mut(r as usize) {
                *slot = true;
            }
        }
        for c in cell.col..cell.col + cell.colspan {
            if let Some(slot) = col_used.get_mut(c as usize) {
                *slot = true;
            }
        }
    }
    let renumber = |used: &[bool]| -> Vec<u32> {
        let mut next = 0;
        used.iter()
            .map(|u| {
                let index = next;
                if *u {
                    next += 1;
                }
                index
            })
            .collect()
    };
    let (row_map, col_map) = (renumber(&row_used), renumber(&col_used));
    let count = |used: &[bool], from: u32, span: u32| -> u32 {
        (from..from + span)
            .filter(|i| used.get(*i as usize).copied().unwrap_or(false))
            .count() as u32
    };
    table.cells.retain(|c| {
        count(&row_used, c.row, c.rowspan) > 0 && count(&col_used, c.col, c.colspan) > 0
    });
    for cell in &mut table.cells {
        let (rowspan, colspan) = (
            count(&row_used, cell.row, cell.rowspan),
            count(&col_used, cell.col, cell.colspan),
        );
        let first_row = (cell.row..cell.row + cell.rowspan)
            .find(|r| row_used[*r as usize])
            .unwrap_or(cell.row);
        let first_col = (cell.col..cell.col + cell.colspan)
            .find(|c| col_used[*c as usize])
            .unwrap_or(cell.col);
        cell.row = row_map[first_row as usize];
        cell.col = col_map[first_col as usize];
        cell.rowspan = rowspan;
        cell.colspan = colspan;
    }
    table.rows = row_used.iter().filter(|u| **u).count() as u32;
    table.cols = col_used.iter().filter(|u| **u).count() as u32;
    table.cells.sort_by_key(|c| (c.row, c.col));
}

fn mark_header(table: &mut TableNode) {
    for cell in table.cells.iter_mut().filter(|c| c.row == 0) {
        cell.header = true;
    }
}

/// Gaps in the horizontal projection of `lines` at least `min` wide.
pub(crate) fn column_gaps(lines: &[Line], min: f32) -> Vec<(f32, f32)> {
    let mut ink: Vec<(f32, f32)> = lines.iter().map(|l| (l.bbox.x, l.bbox.right())).collect();
    ink.sort_by(|a, b| a.0.total_cmp(&b.0));
    let mut gaps = Vec::new();
    let mut reach = f32::MIN;
    for (start, end) in ink {
        if reach > f32::MIN && start - reach >= min {
            gaps.push((reach, start));
        }
        reach = reach.max(end);
    }
    gaps
}

/// A grid with horizontal lines only. When the lines separate individual rows
/// the bands are the rows and columns come from the text.
fn from_row_bands(ctx: &Ctx, grid: &Grid, inside: &[u32]) -> Option<TableNode> {
    let edges = &grid.row_edges;
    let bands = edges.len().saturating_sub(1);
    if bands < 2 {
        return None;
    }
    let band_of = |y: f32| -> Option<usize> {
        (0..bands).find(|b| y >= edges[*b] - 0.5 && y <= edges[*b + 1] + 0.5)
    };
    let mut band_words: Vec<Vec<u32>> = vec![Vec::new(); bands];
    for &id in inside {
        if let Some(band) = band_of(ctx.word(id).bbox.cy()) {
            band_words[band].push(id);
        }
    }
    let used: Vec<usize> = (0..bands).filter(|b| !band_words[*b].is_empty()).collect();
    if used.len() < 2 {
        return None;
    }
    // Rules that frame sections rather than rows leave tall bands full of lines.
    let band_lines: Vec<Vec<Line>> = band_words.iter().map(|w| lines_from(ctx, w)).collect();
    let total_lines: usize = band_lines.iter().map(Vec::len).sum();
    let row_ruled = used.len() >= 3 && used.len() * 5 >= total_lines * 2;
    if !row_ruled {
        return sectioned(ctx, grid, inside, &band_lines);
    }
    let all_lines: Vec<Line> = {
        // Split every band line at wide gaps so cells are separate fragments.
        let mut fragments = Vec::new();
        for line in band_lines.iter().flatten() {
            fragments.extend(split_line(ctx, line, 1.0));
        }
        fragments
    };
    let size = {
        let mut sizes: Vec<f32> = all_lines.iter().map(|l| l.size).collect();
        quantile(&mut sizes, 0.5).max(1.0)
    };
    let gaps = column_gaps(&all_lines, 0.75 * size);
    let cuts: Vec<f32> = gaps.iter().map(|(a, b)| (a + b) / 2.0).collect();
    let cols = cuts.len() + 1;
    if cols == 1 {
        // Rules between paragraphs divide sections; a one-column table has
        // short entries, one or two lines to a row.
        let mut chars: Vec<f32> = all_lines.iter().map(|l| l.chars as f32).collect();
        if band_lines.iter().any(|b| b.len() > 2) || median(&mut chars) > 50.0 {
            return None;
        }
    }
    let col_of = |x: f32| cuts.iter().filter(|c| **c < x).count();
    let mut cells: Vec<TableCell> = Vec::new();
    for (row, band) in used.iter().enumerate() {
        let mut per_col: Vec<Vec<u32>> = vec![Vec::new(); cols];
        for &id in &band_words[*band] {
            per_col[col_of(ctx.word(id).bbox.cx())].push(id);
        }
        for (col, words) in per_col.into_iter().enumerate() {
            let rect = if words.is_empty() {
                Rect::default()
            } else {
                union_all(words.iter().map(|id| ctx.word(*id).bbox))
            };
            cells.push(TableCell {
                row: row as u32,
                col: col as u32,
                rowspan: 1,
                colspan: 1,
                header: row == 0,
                words,
                rect,
            });
        }
    }
    if cells.iter().filter(|c| !c.words.is_empty()).count() < 2 {
        return None;
    }
    Some(TableNode {
        bbox: grid.bbox,
        rows: used.len() as u32,
        cols: cols as u32,
        cells,
        ruled: true,
        confidence: 0.85,
    })
}

/// Break a line at gaps wider than `em` times its font size.
pub(crate) fn split_line(ctx: &Ctx, line: &Line, em: f32) -> Vec<Line> {
    let mut parts: Vec<Vec<u32>> = Vec::new();
    let mut reach = f32::MIN;
    for &id in &line.words {
        let bbox = ctx.word(id).bbox;
        if parts.is_empty() || bbox.x - reach > em * line.size {
            parts.push(Vec::new());
        }
        if let Some(part) = parts.last_mut() {
            part.push(id);
        }
        reach = reach.max(bbox.right());
    }
    parts
        .into_iter()
        .map(|words| Line::build(ctx, words))
        .collect()
}

/// Build a table from text columns (`strips`, left to right). `rules` are the
/// y positions of horizontal lines drawn inside the table's extent.
pub(crate) fn from_strips(ctx: &Ctx, strips: &[Vec<Line>], rules: &[f32]) -> Option<TableNode> {
    let cols = strips.len();
    let mut sizes: Vec<f32> = strips.iter().flatten().map(|l| l.size).collect();
    if cols < 2 || sizes.is_empty() {
        return None;
    }
    let size = quantile(&mut sizes, 0.5).max(1.0);
    // Row lines: baselines shared across strips.
    let baselines = cluster(
        strips.iter().flatten().map(|l| l.baseline).collect(),
        0.35 * size,
    );
    let row_of = |baseline: f32| -> usize {
        baselines
            .iter()
            .enumerate()
            .min_by(|a, b| {
                (a.1.0 - baseline)
                    .abs()
                    .total_cmp(&(b.1.0 - baseline).abs())
            })
            .map_or(0, |(index, _)| index)
    };
    let count = baselines.len();
    let mut grid: Vec<Vec<Vec<u32>>> = vec![vec![Vec::new(); cols]; count];
    for (col, lines) in strips.iter().enumerate() {
        for line in lines {
            grid[row_of(line.baseline)][col].extend(line.words.iter().copied());
        }
    }
    let filled = |row: &Vec<Vec<u32>>| row.iter().filter(|c| !c.is_empty()).count();
    let pitches: Vec<f32> = baselines.windows(2).map(|w| w[1].0 - w[0].0).collect();
    let tight = {
        let mut sorted = pitches.clone();
        quantile(&mut sorted, 0.15)
    };
    let loose = pitches.iter().copied().fold(0.0, f32::max);
    let bimodal = tight > 0.0 && loose >= 1.3 * tight;
    let first_ratio = grid.iter().filter(|row| !row[0].is_empty()).count() as f32 / count as f32;
    let rule_between = |above: f32, below: f32| {
        rules
            .iter()
            .any(|y| *y > above + 0.5 && *y < below - 0.45 * size)
    };
    let ruled_breaks = (1..count)
        .filter(|r| rule_between(baselines[r - 1].0, baselines[*r].0))
        .count();
    let per_row_rules = count >= 3 && ruled_breaks * 2 >= count - 1;
    // Merge wrapped lines into the row they continue.
    let mut rows: Vec<Vec<Vec<u32>>> = Vec::new();
    let mut head_filled = 0;
    for (index, row) in grid.into_iter().enumerate() {
        let continues = index > 0 && !rows.is_empty() && {
            let pitch = pitches[index - 1];
            let ruled = rule_between(baselines[index - 1].0, baselines[index].0);
            if per_row_rules {
                !ruled
            } else {
                let sparse = filled(&row) <= (cols / 2).max(1) && filled(&row) < cols;
                !ruled
                    && pitch <= 1.6 * tight.max(0.1)
                    && ((row[0].is_empty() && first_ratio >= 0.6 && sparse)
                        || (bimodal && pitch <= 1.15 * tight && filled(&row) < head_filled))
            }
        };
        if continues {
            if let Some(last) = rows.last_mut() {
                for (col, words) in row.into_iter().enumerate() {
                    last[col].extend(words);
                }
            }
        } else {
            head_filled = filled(&row);
            rows.push(row);
        }
    }
    if rows.len() < 2 {
        return None;
    }
    let total = rows.len() * cols;
    let mut cells = Vec::with_capacity(total);
    let mut nonempty = 0;
    for (r, row) in rows.into_iter().enumerate() {
        for (c, words) in row.into_iter().enumerate() {
            let rect = if words.is_empty() {
                Rect::default()
            } else {
                nonempty += 1;
                union_all(words.iter().map(|id| ctx.word(*id).bbox))
            };
            cells.push(TableCell {
                row: r as u32,
                col: c as u32,
                rowspan: 1,
                colspan: 1,
                header: r == 0,
                words,
                rect,
            });
        }
    }
    let bbox = union_all(strips.iter().flatten().map(|l| l.bbox));
    let rows_count = cells.last().map_or(0, |c| c.row + 1);
    Some(TableNode {
        bbox,
        rows: rows_count,
        cols: cols as u32,
        cells,
        ruled: false,
        confidence: (nonempty as f32 / total.max(1) as f32).clamp(0.3, 0.9),
    })
}

/// Running text: several lines, most of them filling the column.
pub(crate) fn prose_like(lines: &[Line], size: f32) -> bool {
    if lines.len() < 3 {
        return false;
    }
    let left = lines.iter().map(|l| l.bbox.x).fold(f32::MAX, f32::min);
    let right = lines
        .iter()
        .map(|l| l.bbox.right())
        .fold(f32::MIN, f32::max);
    let width = right - left;
    if width < 8.0 * size {
        return false;
    }
    let full = lines.iter().filter(|l| l.bbox.w >= 0.8 * width).count();
    let mut chars: Vec<f32> = lines.iter().map(|l| l.chars as f32).collect();
    full * 2 >= lines.len() && median(&mut chars) >= 15.0
}

/// Rules above, below and under the header only (the usual style in papers):
/// columns come from whitespace shared by every line, rows from the text.
fn sectioned(
    ctx: &Ctx,
    grid: &Grid,
    inside: &[u32],
    band_lines: &[Vec<Line>],
) -> Option<TableNode> {
    let lines: Vec<&Line> = band_lines.iter().flatten().collect();
    if lines.len() < 2 || lines.len() > 90 {
        return None;
    }
    let mut sizes: Vec<f32> = lines.iter().map(|l| l.size).collect();
    let size = median(&mut sizes).max(1.0);
    let (left, right) = (grid.bbox.x - 1.0, grid.bbox.right() + 1.0);
    let min = 0.75 * size;
    let gutters_of = |selected: &[&Line]| -> Vec<(f32, f32)> {
        let mut common = vec![(left, right)];
        for line in selected {
            let ink = merge_intervals(
                line.words
                    .iter()
                    .map(|id| {
                        let b = ctx.word(*id).bbox;
                        (b.x, b.right())
                    })
                    .collect(),
                0.0,
            );
            common = intersect(&common, &free_of(&ink, left, right), min);
        }
        let ink_left = selected.iter().map(|l| l.bbox.x).fold(f32::MAX, f32::min);
        let ink_right = selected
            .iter()
            .map(|l| l.bbox.right())
            .fold(f32::MIN, f32::max);
        common
            .into_iter()
            .filter(|g| g.0 > ink_left && g.1 < ink_right)
            .collect()
    };
    let mut gutters = gutters_of(&lines);
    if gutters.is_empty() && band_lines.len() >= 2 {
        // Header cells that span several columns close the gutters; the body
        // alone still shows them.
        let body: Vec<&Line> = band_lines
            .iter()
            .skip_while(|b| b.is_empty())
            .skip(1)
            .flatten()
            .collect();
        if body.len() >= 2 {
            gutters = gutters_of(&body);
        }
    }
    let rule_ys: Vec<f32> = grid.row_edges.clone();
    if gutters.is_empty() {
        // A one-column table: a short header band, then short rows.
        let header = band_lines.iter().find(|b| !b.is_empty())?;
        let mut chars: Vec<f32> = lines.iter().map(|l| l.chars as f32).collect();
        let owned: Vec<Line> = lines.iter().map(|l| (*l).clone()).collect();
        if grid.row_edges.len() < 3
            || header.len() > 2
            || median(&mut chars) > 50.0
            || prose_like(&owned, size)
        {
            return None;
        }
        let cells: Vec<TableCell> = lines
            .iter()
            .enumerate()
            .map(|(row, line)| TableCell {
                row: row as u32,
                col: 0,
                rowspan: 1,
                colspan: 1,
                header: row < header.len(),
                words: line.words.clone(),
                rect: line.bbox,
            })
            .collect();
        return Some(TableNode {
            bbox: grid.bbox,
            rows: cells.len() as u32,
            cols: 1,
            cells,
            ruled: true,
            confidence: 0.8,
        });
    }
    let cuts: Vec<f32> = gutters.iter().map(|g| (g.0 + g.1) / 2.0).collect();
    let mut strip_words: Vec<Vec<u32>> = vec![Vec::new(); cuts.len() + 1];
    for &id in inside {
        let x = ctx.word(id).bbox.cx();
        strip_words[cuts.iter().filter(|c| **c < x).count()].push(id);
    }
    let strips: Vec<Vec<Line>> = strip_words.iter().map(|w| lines_from(ctx, w)).collect();
    // Rules that merely frame a page of running text are not a table.
    if strips.iter().any(|s| s.len() >= 8 && prose_like(s, size)) {
        return None;
    }
    let mut table = from_strips(ctx, &strips, &rule_ys)?;
    table.bbox = grid.bbox;
    table.ruled = true;
    table.confidence = table.confidence.max(0.8);
    Some(table)
}
