//! Table grids recovered from lines drawn in the file. Where an author drew a
//! grid, cell boundaries are a fact of the file, not an inference.
use super::geom::{cluster, median, merge_intervals};
use super::{PageInput, Rect};

#[derive(Debug, Clone)]
pub(crate) struct GridCell {
    pub row: u32,
    pub col: u32,
    pub rowspan: u32,
    pub colspan: u32,
    pub rect: Rect,
}

#[derive(Debug, Clone)]
pub(crate) struct Grid {
    pub bbox: Rect,
    pub rows: u32,
    pub cols: u32,
    pub cells: Vec<GridCell>,
    /// The grid has no interior vertical lines: columns must come from text.
    pub rows_only: bool,
    pub row_edges: Vec<f32>,
}

/// One merged straight line: fixed coordinate plus an extent on the other axis.
#[derive(Debug, Clone, Copy)]
struct Stroke {
    at: f32,
    from: f32,
    to: f32,
}

const SNAP: f32 = 2.5;

/// Merged strokes plus the interior points where they were drawn in separate
/// pieces. Borders drawn one cell at a time break exactly at cell corners.
fn strokes(rules: &[Rect], horizontal: bool) -> (Vec<Stroke>, Vec<(f32, f32)>) {
    let mut raw: Vec<Stroke> = rules
        .iter()
        .filter(|r| r.finite())
        .filter_map(|r| {
            if horizontal && r.h <= 1.5 && r.w >= 1.0 {
                Some(Stroke {
                    at: r.cy(),
                    from: r.x,
                    to: r.right(),
                })
            } else if !horizontal && r.w <= 1.5 && r.h >= 1.0 {
                Some(Stroke {
                    at: r.cx(),
                    from: r.y,
                    to: r.bottom(),
                })
            } else {
                None
            }
        })
        .collect();
    raw.sort_by(|a, b| a.at.total_cmp(&b.at));
    let mut merged = Vec::new();
    let mut breaks = Vec::new();
    let mut start = 0;
    while start < raw.len() {
        let mut end = start + 1;
        while end < raw.len() && raw[end].at - raw[end - 1].at <= 1.2 {
            end += 1;
        }
        let at = raw[start..end].iter().map(|s| s.at).sum::<f32>() / (end - start) as f32;
        for (from, to) in merge_intervals(
            raw[start..end].iter().map(|s| (s.from, s.to)).collect(),
            SNAP,
        ) {
            if to - from < 6.0 {
                continue;
            }
            merged.push(Stroke { at, from, to });
            for piece in &raw[start..end] {
                for point in [piece.from, piece.to] {
                    if point > from + 2.0 && point < to - 2.0 {
                        breaks.push((at, point));
                    }
                }
            }
        }
        start = end;
    }
    (merged, breaks)
}

/// Positions where at least two parallel strokes break: implied crossing edges.
/// `own` are the strokes that break, `across` the real strokes that cross them.
fn implied(breaks: &[(f32, f32)], own: &[Stroke], across: &[Stroke]) -> Vec<(f32, f32, f32)> {
    // (position, lowest supporting stroke, highest supporting stroke)
    let mut sorted: Vec<(f32, f32)> = breaks.to_vec();
    sorted.sort_by(|a, b| a.1.total_cmp(&b.1));
    let mut out = Vec::new();
    let mut start = 0;
    while start < sorted.len() {
        let mut end = start + 1;
        while end < sorted.len() && sorted[end].1 - sorted[end - 1].1 <= 1.5 {
            end += 1;
        }
        let group = &sorted[start..end];
        let position = group.iter().map(|g| g.1).sum::<f32>() / group.len() as f32;
        // Each supporter is the stroke that breaks here.
        let supporters: Vec<Stroke> = cluster(group.iter().map(|g| g.0).collect(), 1.5)
            .into_iter()
            .filter_map(|(at, _)| {
                own.iter()
                    .find(|s| (s.at - at).abs() <= 1.5 && s.from < position && s.to > position)
                    .copied()
            })
            .collect();
        // Two borders belong to one grid when a drawn line crosses both, or
        // when they run side by side over the same extent. Anything else is
        // a neighbouring table or drawing that happens to break at this point.
        let mut gaps: Vec<f32> = supporters.windows(2).map(|w| w[1].at - w[0].at).collect();
        let limit = (2.5 * median(&mut gaps)).max(40.0);
        let joined = |a: &Stroke, b: &Stroke| {
            let crossed = across.iter().any(|c| {
                c.from <= a.at + SNAP
                    && c.to >= b.at - SNAP
                    && c.at >= a.from.max(b.from) - SNAP
                    && c.at <= a.to.min(b.to) + SNAP
            });
            let together = (a.from - b.from).abs() <= 2.0 * SNAP
                && (a.to - b.to).abs() <= 2.0 * SNAP
                && b.at - a.at <= limit;
            crossed || together
        };
        let mut run_start = 0;
        for index in 1..=supporters.len() {
            let ends =
                index == supporters.len() || !joined(&supporters[index - 1], &supporters[index]);
            if ends {
                if index - run_start >= 2 {
                    out.push((position, supporters[run_start].at, supporters[index - 1].at));
                }
                run_start = index;
            }
        }
        start = end;
    }
    out
}

fn find(parent: &mut [usize], mut node: usize) -> usize {
    while parent[node] != node {
        parent[node] = parent[parent[node]];
        node = parent[node];
    }
    node
}

fn covered(strokes: &[Stroke], at: f32, from: f32, to: f32) -> bool {
    let need = (to - from) * 0.6;
    let mut have = 0.0;
    for stroke in strokes.iter().filter(|s| (s.at - at).abs() <= SNAP + 0.5) {
        have += (stroke.to.min(to) - stroke.from.max(from)).max(0.0);
    }
    have >= need
}

/// Snap sorted coordinates so double borders collapse into one edge.
fn edges(values: Vec<f32>) -> Vec<f32> {
    cluster(values, SNAP + 0.5)
        .into_iter()
        .map(|(mean, _)| mean)
        .collect()
}

pub(crate) fn grids(page: &PageInput) -> Vec<Grid> {
    let (mut horizontal, h_breaks) = strokes(&page.rules, true);
    let (mut vertical, v_breaks) = strokes(&page.rules, false);
    if horizontal.len() + vertical.len() > 4000 {
        return Vec::new();
    }
    // Vertical borders that break at the same heights imply row edges between
    // them, and likewise for horizontal borders and column edges.
    if h_breaks.len() + v_breaks.len() <= 2000 {
        let rows = implied(&v_breaks, &vertical, &horizontal);
        let columns = implied(&h_breaks, &horizontal, &vertical);
        for (y, left, right) in rows {
            horizontal.push(Stroke {
                at: y,
                from: left,
                to: right,
            });
        }
        for (x, top, bottom) in columns {
            vertical.push(Stroke {
                at: x,
                from: top,
                to: bottom,
            });
        }
    }
    if horizontal.len() < 2 {
        return Vec::new();
    }
    let (nh, nv) = (horizontal.len(), vertical.len());
    let mut parent: Vec<usize> = (0..nh + nv).collect();
    let mut crossed = vec![false; nh];
    for (i, h) in horizontal.iter().enumerate() {
        for (j, v) in vertical.iter().enumerate() {
            let crosses = v.at >= h.from - SNAP
                && v.at <= h.to + SNAP
                && h.at >= v.from - SNAP
                && h.at <= v.to + SNAP;
            if crosses {
                crossed[i] = true;
                let (a, b) = (find(&mut parent, i), find(&mut parent, nh + j));
                parent[a] = b;
            }
        }
    }
    // Horizontal rules with the same extent stacked above each other belong
    // together even without vertical lines (booktabs-style tables).
    for i in 0..nh {
        for j in i + 1..nh {
            let (a, b) = (&horizontal[i], &horizontal[j]);
            let same = (a.from - b.from).abs() <= 3.0 && (a.to - b.to).abs() <= 3.0;
            if same && a.to - a.from >= 40.0 && !crossed[i] && !crossed[j] {
                let (ra, rb) = (find(&mut parent, i), find(&mut parent, j));
                parent[ra] = rb;
            }
        }
    }
    let mut groups: std::collections::BTreeMap<usize, (Vec<Stroke>, Vec<Stroke>)> =
        std::collections::BTreeMap::new();
    for (i, stroke) in horizontal.iter().enumerate() {
        let root = find(&mut parent, i);
        groups.entry(root).or_default().0.push(*stroke);
    }
    for (j, stroke) in vertical.iter().enumerate() {
        let root = find(&mut parent, nh + j);
        groups.entry(root).or_default().1.push(*stroke);
    }
    let mut out = Vec::new();
    for (hs, vs) in groups.into_values() {
        if hs.len() < 2 {
            continue;
        }
        let mut hs = hs;
        let left = hs.iter().map(|s| s.from).fold(f32::MAX, f32::min);
        let right = hs.iter().map(|s| s.to).fold(f32::MIN, f32::max);
        let mut top = hs.iter().map(|s| s.at).fold(f32::MAX, f32::min);
        let mut bottom = hs.iter().map(|s| s.at).fold(f32::MIN, f32::max);
        // Borders that run past the outermost drawn line to a common end close
        // a row whose own edge was not drawn as a line (a shaded header band).
        for above in [true, false] {
            let ends: Vec<f32> = vs
                .iter()
                .map(|v| if above { v.from } else { v.to })
                .filter(|e| {
                    if above {
                        *e < top - 6.0
                    } else {
                        *e > bottom + 6.0
                    }
                })
                .collect();
            let shared = cluster(ends, SNAP)
                .into_iter()
                .filter(|(_, count)| *count >= 3)
                .map(|(at, _)| at)
                .fold(None, |best: Option<f32>, at| match best {
                    Some(b) if above == (b < at) => Some(b),
                    _ => Some(at),
                });
            if let Some(at) = shared {
                let touching =
                    |v: &&Stroke| ((if above { v.from } else { v.to }) - at).abs() <= SNAP + 0.5;
                let from = vs
                    .iter()
                    .filter(touching)
                    .map(|v| v.at)
                    .fold(f32::MAX, f32::min);
                let to = vs
                    .iter()
                    .filter(touching)
                    .map(|v| v.at)
                    .fold(f32::MIN, f32::max);
                hs.push(Stroke { at, from, to });
                if above {
                    top = at;
                } else {
                    bottom = at;
                }
            }
        }
        if right - left < 20.0 || bottom - top < 6.0 {
            continue;
        }
        // Implied row edges run between the outermost vertical borders that
        // support them; outer columns without a border still share the row.
        let hs: Vec<Stroke> = hs
            .into_iter()
            .map(|mut s| {
                if s.from - left <= 0.5 * (right - left) && s.from > left {
                    let lone = !vs.iter().any(|v| v.at < s.from - SNAP);
                    if lone {
                        s.from = left;
                    }
                }
                if right - s.to <= 0.5 * (right - left) && s.to < right {
                    let lone = !vs.iter().any(|v| v.at > s.to + SNAP);
                    if lone {
                        s.to = right;
                    }
                }
                s
            })
            .collect();
        let mut xs: Vec<f32> = vs.iter().map(|s| s.at).collect();
        xs.push(left);
        xs.push(right);
        let xs = edges(xs);
        let mut ys: Vec<f32> = hs.iter().map(|s| s.at).collect();
        for v in &vs {
            ys.push(v.from);
            ys.push(v.to);
        }
        let ys: Vec<f32> = edges(ys)
            .into_iter()
            .filter(|y| *y >= top - SNAP && *y <= bottom + SNAP)
            .collect();
        if xs.len() < 2 || ys.len() < 2 || xs.len() > 60 || ys.len() > 400 {
            continue;
        }
        let (rows, cols) = (ys.len() - 1, xs.len() - 1);
        // Cells without a separating line between them are one spanning cell.
        let mut cell_parent: Vec<usize> = (0..rows * cols).collect();
        for (r, band) in ys.windows(2).enumerate() {
            for (c, x) in xs.iter().enumerate().take(cols).skip(1) {
                if !covered(&vs, *x, band[0], band[1]) {
                    let (a, b) = (
                        find(&mut cell_parent, r * cols + c - 1),
                        find(&mut cell_parent, r * cols + c),
                    );
                    cell_parent[a] = b;
                }
            }
        }
        for (r, y) in ys.iter().enumerate().take(rows).skip(1) {
            for (c, track) in xs.windows(2).enumerate() {
                if !covered(&hs, *y, track[0], track[1]) {
                    let (a, b) = (
                        find(&mut cell_parent, (r - 1) * cols + c),
                        find(&mut cell_parent, r * cols + c),
                    );
                    cell_parent[a] = b;
                }
            }
        }
        let mut spans: std::collections::BTreeMap<usize, (usize, usize, usize, usize)> =
            std::collections::BTreeMap::new();
        for r in 0..rows {
            for c in 0..cols {
                let root = find(&mut cell_parent, r * cols + c);
                let entry = spans.entry(root).or_insert((r, c, r, c));
                entry.0 = entry.0.min(r);
                entry.1 = entry.1.min(c);
                entry.2 = entry.2.max(r);
                entry.3 = entry.3.max(c);
            }
        }
        let mut cells: Vec<GridCell> = spans
            .into_values()
            .map(|(r0, c0, r1, c1)| GridCell {
                row: r0 as u32,
                col: c0 as u32,
                rowspan: (r1 - r0 + 1) as u32,
                colspan: (c1 - c0 + 1) as u32,
                rect: Rect::from_edges(xs[c0], ys[r0], xs[c1 + 1], ys[r1 + 1]),
            })
            .collect();
        cells.sort_by_key(|c| (c.row, c.col));
        let rows_only = cols > 1
            && cells
                .iter()
                .all(|c| c.col == 0 && c.colspan as usize == cols)
            || cols == 1;
        out.push(Grid {
            bbox: Rect::from_edges(left, top, right, bottom),
            rows: rows as u32,
            cols: cols as u32,
            cells,
            rows_only,
            row_edges: ys,
        });
    }
    out.sort_by(|a, b| a.bbox.y.total_cmp(&b.bbox.y));
    out
}
