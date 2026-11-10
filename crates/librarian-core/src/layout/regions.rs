//! Page segmentation by whitespace. A gutter is a vertical strip of white that
//! persists over consecutive rows; each gutter is then explained as separating
//! independent text columns, separating table columns, or as noise.
use super::Rect;
use super::geom::{cluster, free_of, intersect, median, merge_intervals, union_all};
use super::tables::{TableNode, from_strips, prose_like};
use super::text::{Ctx, Line, lines_from, list_marker, word_baseline, word_size};

pub(crate) enum Node {
    /// Words of one column of running text.
    Flow(Vec<u32>),
    Table(TableNode),
    /// A picture or vector figure with the words drawn inside it.
    Figure(Rect, Vec<u32>),
}

impl Node {
    fn bbox(&self, ctx: &Ctx) -> Rect {
        match self {
            Node::Flow(words) => union_all(words.iter().map(|id| ctx.word(*id).bbox)),
            Node::Table(table) => table.bbox,
            Node::Figure(rect, _) => *rect,
        }
    }
}

#[derive(Clone, Copy)]
enum Item {
    Word(u32),
    Atom(usize),
}

#[derive(Clone, Copy)]
struct Entry {
    item: Item,
    bbox: Rect,
}

struct Row {
    top: f32,
    bottom: f32,
    members: Vec<usize>,
    /// Merged ink intervals on the x axis.
    ink: Vec<(f32, f32)>,
}

struct Segmenter<'a, 'b> {
    ctx: &'a Ctx<'b>,
    atoms: Vec<Option<Node>>,
    /// y positions and extents of horizontal rules on the page.
    rules: &'a [Rect],
    out: Vec<Node>,
}

enum Kind {
    Columns,
    Table(TableNode),
    Reject,
}

pub(crate) fn segment(ctx: &Ctx, words: &[u32], atoms: Vec<Node>, rules: &[Rect]) -> Vec<Node> {
    let mut entries: Vec<Entry> = words
        .iter()
        .map(|id| Entry {
            item: Item::Word(*id),
            bbox: ctx.word(*id).bbox,
        })
        .collect();
    for (index, atom) in atoms.iter().enumerate() {
        entries.push(Entry {
            item: Item::Atom(index),
            bbox: atom.bbox(ctx),
        });
    }
    let mut segmenter = Segmenter {
        ctx,
        atoms: atoms.into_iter().map(Some).collect(),
        rules,
        out: Vec::new(),
    };
    segmenter.split(entries, 0);
    segmenter.out
}

struct Candidate {
    first: usize,
    last: usize,
    gap: (f32, f32),
    support: usize,
}

impl Segmenter<'_, '_> {
    /// Rows are text lines (words sharing a baseline) and atoms. Lines of
    /// neighbouring columns keep their own rows even when they overlap
    /// vertically, so one long line cannot hide a gutter beside the others.
    fn rows(&self, entries: &[Entry]) -> Vec<Row> {
        let mut words: Vec<(f32, f32, usize)> = Vec::new(); // baseline, size, entry
        let mut rows: Vec<Row> = Vec::new();
        let mut atoms: Vec<usize> = Vec::new();
        for (index, entry) in entries.iter().enumerate() {
            match entry.item {
                Item::Word(id) => {
                    let word = self.ctx.word(id);
                    words.push((word_baseline(word), word_size(word), index));
                }
                Item::Atom(_) => atoms.push(index),
            }
        }
        words.sort_by(|a, b| a.0.total_cmp(&b.0).then(a.2.cmp(&b.2)));
        let mut anchor = f32::MIN;
        for (baseline, size, index) in words {
            let bbox = entries[index].bbox;
            match rows.last_mut() {
                Some(row) if baseline - anchor <= 0.3 * size.max(1.0) => {
                    row.top = row.top.min(bbox.y);
                    row.bottom = row.bottom.max(bbox.bottom());
                    row.members.push(index);
                }
                _ => {
                    anchor = baseline;
                    rows.push(Row {
                        top: bbox.y,
                        bottom: bbox.bottom(),
                        members: vec![index],
                        ink: Vec::new(),
                    });
                }
            }
        }
        for row in &mut rows {
            let mut ink: Vec<(f32, f32)> = row
                .members
                .iter()
                .map(|i| (entries[*i].bbox.x, entries[*i].bbox.right()))
                .collect();
            // An atom blocks every line it stands beside.
            let middle = (row.top + row.bottom) / 2.0;
            for &atom in &atoms {
                let bbox = entries[atom].bbox;
                if middle >= bbox.y && middle <= bbox.bottom() {
                    ink.push((bbox.x, bbox.right()));
                }
            }
            row.ink = merge_intervals(ink, 0.0);
        }
        for atom in atoms {
            let bbox = entries[atom].bbox;
            rows.push(Row {
                top: bbox.y,
                bottom: bbox.bottom(),
                members: vec![atom],
                ink: vec![(bbox.x, bbox.right())],
            });
        }
        rows.sort_by(|a, b| (a.top + a.bottom).total_cmp(&(b.top + b.bottom)));
        rows
    }

    /// Number of text lines (or their height equivalent) on one side of a gap.
    fn side_lines(
        &self,
        entries: &[Entry],
        rows: &[Row],
        left: bool,
        gap: (f32, f32),
        size: f32,
    ) -> usize {
        let mut baselines = Vec::new();
        let mut atom_lines = 0.0;
        for row in rows {
            for &i in &row.members {
                let entry = &entries[i];
                let on_side = if left {
                    entry.bbox.right() <= gap.0 + 0.5
                } else {
                    entry.bbox.x >= gap.1 - 0.5
                };
                if !on_side {
                    continue;
                }
                match entry.item {
                    Item::Word(id) => baselines.push(word_baseline(self.ctx.word(id))),
                    Item::Atom(_) => atom_lines += entry.bbox.h / (1.2 * size),
                }
            }
        }
        cluster(baselines, 0.3 * size).len() + atom_lines as usize
    }

    fn candidates(&self, entries: &[Entry], rows: &[Row], size: f32) -> Vec<Candidate> {
        let left = entries.iter().map(|e| e.bbox.x).fold(f32::MAX, f32::min);
        let right = entries
            .iter()
            .map(|e| e.bbox.right())
            .fold(f32::MIN, f32::max);
        let min = (0.75 * size).max(3.0);
        let free: Vec<Vec<(f32, f32)>> =
            rows.iter().map(|r| free_of(&r.ink, left, right)).collect();
        let mut found: Vec<Candidate> = Vec::new();
        for first in 0..rows.len() {
            // Margins count: a row with text on one side only still leaves
            // the gutter free. Whether a gap is interior is settled by support.
            let mut active: Vec<(f32, f32)> = free[first]
                .iter()
                .copied()
                .filter(|g| g.1 - g.0 >= min)
                .collect();
            // Only start where the row above closes the gap: maximal runs.
            // A gap wholly free in the row above was already tracked from there.
            if first > 0 {
                active.retain(|g| {
                    !free[first - 1]
                        .iter()
                        .any(|f| f.0 <= g.0 + 0.01 && f.1 >= g.1 - 0.01)
                });
            }
            let mut last = first;
            while !active.is_empty() {
                let next = if last + 1 < rows.len() {
                    intersect(&active, &free[last + 1], min)
                } else {
                    Vec::new()
                };
                for gap in &active {
                    let survives = next
                        .iter()
                        .any(|n| n.0 >= gap.0 - 0.01 && n.1 <= gap.1 + 0.01);
                    if !survives && gap.0 > left + 0.5 && gap.1 < right - 0.5 {
                        found.push(Candidate {
                            first,
                            last,
                            gap: *gap,
                            support: 0,
                        });
                    }
                }
                active = next;
                last += 1;
            }
        }
        // Rank by how many rows a gap spans, then measure real support.
        found.sort_by(|a, b| {
            (b.last - b.first)
                .cmp(&(a.last - a.first))
                .then((b.gap.1 - b.gap.0).total_cmp(&(a.gap.1 - a.gap.0)))
        });
        found.truncate(24);
        for candidate in &mut found {
            let run = &rows[candidate.first..=candidate.last];
            candidate.support = self
                .side_lines(entries, run, true, candidate.gap, size)
                .min(self.side_lines(entries, run, false, candidate.gap, size));
        }
        found.retain(|c| valid(c.gap.1 - c.gap.0, c.support, size));
        found.sort_by(|a, b| {
            b.support
                .cmp(&a.support)
                .then((b.gap.1 - b.gap.0).total_cmp(&(a.gap.1 - a.gap.0)))
        });
        found.truncate(10);
        found
    }

    fn split(&mut self, entries: Vec<Entry>, depth: usize) {
        if entries.is_empty() {
            return;
        }
        if entries.len() < 2 || depth > 24 {
            self.leaf(entries);
            return;
        }
        let mut sizes: Vec<f32> = entries
            .iter()
            .filter_map(|e| match e.item {
                Item::Word(id) => Some(word_size(self.ctx.word(id))),
                Item::Atom(_) => None,
            })
            .collect();
        let size = if sizes.is_empty() {
            self.ctx.stats.body
        } else {
            median(&mut sizes)
        };
        let rows = self.rows(&entries);
        for candidate in self.candidates(&entries, &rows, size) {
            let run = &rows[candidate.first..=candidate.last];
            // Every gutter shared by the run, not only the one that ranked first.
            let left = entries.iter().map(|e| e.bbox.x).fold(f32::MAX, f32::min);
            let right = entries
                .iter()
                .map(|e| e.bbox.right())
                .fold(f32::MIN, f32::max);
            let min = (0.75 * size).max(3.0);
            let mut common = free_of(&run[0].ink, left, right);
            for row in &run[1..] {
                common = intersect(&common, &free_of(&row.ink, left, right), min);
            }
            let gutters: Vec<(f32, f32)> = common
                .into_iter()
                .filter(|g| g.1 - g.0 >= min && g.0 > left + 0.5 && g.1 < right - 0.5)
                .filter(|g| {
                    let support = self
                        .side_lines(&entries, run, true, *g, size)
                        .min(self.side_lines(&entries, run, false, *g, size));
                    valid(g.1 - g.0, support, size)
                })
                .collect();
            if gutters.is_empty() {
                continue;
            }
            let cuts: Vec<f32> = gutters.iter().map(|g| (g.0 + g.1) / 2.0).collect();
            let mut strips: Vec<Vec<Entry>> = vec![Vec::new(); cuts.len() + 1];
            for row in run {
                for &i in &row.members {
                    let entry = entries[i];
                    strips[cuts.iter().filter(|c| **c < entry.bbox.cx()).count()].push(entry);
                }
            }
            if strips.iter().any(Vec::is_empty) {
                continue;
            }
            let narrowest = gutters.iter().map(|g| g.1 - g.0).fold(f32::MAX, f32::min);
            match self.classify(&strips, narrowest, size) {
                Kind::Reject => continue,
                Kind::Columns => {
                    let (before, after) = outside(&entries, &rows, candidate.first, candidate.last);
                    self.split(before, depth + 1);
                    for strip in strips {
                        self.split(strip, depth + 1);
                    }
                    self.split(after, depth + 1);
                    return;
                }
                Kind::Table(table) => {
                    let (mut before, mut after) =
                        outside(&entries, &rows, candidate.first, candidate.last);
                    // Words of the run that the table did not take (trimmed
                    // rows above and below) go back to their side.
                    let taken: std::collections::HashSet<u32> = table.words().collect();
                    for row in run {
                        for &i in &row.members {
                            let entry = entries[i];
                            if let Item::Word(id) = entry.item
                                && !taken.contains(&id)
                            {
                                if entry.bbox.cy() < table.bbox.cy() {
                                    before.push(entry);
                                } else {
                                    after.push(entry);
                                }
                            }
                        }
                    }
                    self.split(before, depth + 1);
                    self.out.push(Node::Table(table));
                    self.split(after, depth + 1);
                    return;
                }
            }
        }
        self.leaf(entries);
    }

    /// One column: text and atoms in top-to-bottom order.
    fn leaf(&mut self, entries: Vec<Entry>) {
        let rows = self.rows(&entries);
        let mut pending: Vec<u32> = Vec::new();
        for row in rows {
            let mut atoms: Vec<(f32, usize)> = Vec::new();
            let mut words: Vec<u32> = Vec::new();
            for &i in &row.members {
                match entries[i].item {
                    Item::Word(id) => words.push(id),
                    Item::Atom(index) => atoms.push((entries[i].bbox.x, index)),
                }
            }
            if atoms.is_empty() {
                pending.extend(words);
                continue;
            }
            if !pending.is_empty() {
                self.out.push(Node::Flow(std::mem::take(&mut pending)));
            }
            atoms.sort_by(|a, b| a.0.total_cmp(&b.0));
            for (_, index) in atoms {
                if let Some(node) = self.atoms[index].take() {
                    self.out.push(node);
                }
            }
            pending = words;
        }
        if !pending.is_empty() {
            self.out.push(Node::Flow(pending));
        }
    }

    fn classify(&self, strips: &[Vec<Entry>], narrowest: f32, size: f32) -> Kind {
        let ctx = self.ctx;
        let mut lines: Vec<Vec<Line>> = Vec::with_capacity(strips.len());
        let mut atomic = Vec::with_capacity(strips.len());
        for strip in strips {
            let words: Vec<u32> = strip
                .iter()
                .filter_map(|e| match e.item {
                    Item::Word(id) => Some(id),
                    Item::Atom(_) => None,
                })
                .collect();
            atomic.push(strip.iter().any(|e| matches!(e.item, Item::Atom(_))));
            lines.push(lines_from(ctx, &words));
        }
        let prose: Vec<bool> = lines.iter().map(|l| prose_like(l, size)).collect();
        let wide = narrowest >= 1.0 * size;
        let any_atom = atomic.iter().any(|a| *a);
        if (0..strips.len()).all(|k| prose[k] || atomic[k]) {
            return if wide { Kind::Columns } else { Kind::Reject };
        }
        if any_atom {
            // A picture beside text: independent columns when clearly apart.
            return if narrowest >= 1.5 * size {
                Kind::Columns
            } else {
                Kind::Reject
            };
        }
        // A strip of bullets or item numbers beside its text is a list.
        if strips.len() == 2 && !lines[0].is_empty() {
            let markers = lines[0].iter().filter(|l| marker_only(ctx, l)).count();
            if markers * 5 >= lines[0].len() * 3 {
                return Kind::Reject;
            }
        }
        // A contents list (titles, then page numbers) reads as lines, not cells.
        if strips.len() <= 3
            && lines.last().is_some_and(|l| {
                l.len() >= 3
                    && l.iter().all(|line| {
                        let text = line.text(ctx);
                        text.chars().count() <= 5
                            && text
                                .chars()
                                .all(|c| c.is_ascii_digit() || "ivxlcIVXLC".contains(c))
                    })
            })
        {
            return Kind::Reject;
        }
        let baselines = cluster(
            lines.iter().flatten().map(|l| l.baseline).collect(),
            0.3 * size,
        );
        let shared = |baseline: f32, skip: usize| {
            lines.iter().enumerate().any(|(k, strip)| {
                k != skip
                    && strip
                        .iter()
                        .any(|l| (l.baseline - baseline).abs() <= 0.3 * size)
            })
        };
        let aligned_rows = baselines
            .iter()
            .filter(|(b, _)| {
                lines
                    .iter()
                    .filter(|strip| strip.iter().any(|l| (l.baseline - b).abs() <= 0.3 * size))
                    .count()
                    >= 2
            })
            .count();
        let bbox = union_all(lines.iter().flatten().map(|l| l.bbox));
        let ruled = self.framed(&bbox, size);
        let cell_strips_aligned = (0..strips.len()).filter(|k| !prose[*k]).all(|k| {
            let hits = lines[k].iter().filter(|l| shared(l.baseline, k)).count();
            hits * 5 >= lines[k].len() * 3
        });
        let any_prose = prose.iter().any(|p| *p);
        let table_like = if any_prose {
            cell_strips_aligned && aligned_rows >= 2
        } else {
            aligned_rows >= 3 || (aligned_rows >= 2 && (strips.len() >= 3 || ruled))
        };
        if table_like {
            let trimmed = trim(&lines, size);
            if trimmed.iter().filter(|s| !s.is_empty()).count() >= 2
                && trimmed.iter().all(|s| !s.is_empty())
            {
                let bbox = union_all(trimmed.iter().flatten().map(|l| l.bbox));
                let rule_ys: Vec<f32> = self
                    .rules
                    .iter()
                    .filter(|r| r.h <= 1.5 && r.overlap_x(&bbox) >= 0.5 * bbox.w)
                    .filter(|r| r.cy() > bbox.y && r.cy() < bbox.bottom())
                    .map(Rect::cy)
                    .collect();
                if let Some(table) = from_strips(ctx, &trimmed, &rule_ys) {
                    return Kind::Table(table);
                }
            }
            return Kind::Reject;
        }
        if any_prose && wide && lines.iter().all(|l| l.len() >= 2) {
            // Body text with a side note or margin column.
            return Kind::Columns;
        }
        Kind::Reject
    }

    /// Horizontal rules directly above and below a region suggest a table.
    fn framed(&self, bbox: &Rect, size: f32) -> bool {
        let near = |y: f32| {
            self.rules.iter().any(|r| {
                r.h <= 1.5 && r.overlap_x(bbox) >= 0.6 * bbox.w && (r.cy() - y).abs() <= 2.0 * size
            })
        };
        near(bbox.y) && near(bbox.bottom())
    }
}

fn valid(width: f32, support: usize, size: f32) -> bool {
    let em = width / size.max(1.0);
    (em >= 2.0 && support >= 2) || (em >= 1.0 && support >= 3) || (em >= 0.75 && support >= 5)
}

fn outside(entries: &[Entry], rows: &[Row], first: usize, last: usize) -> (Vec<Entry>, Vec<Entry>) {
    let collect = |range: &[Row]| -> Vec<Entry> {
        range
            .iter()
            .flat_map(|r| r.members.iter().map(|i| entries[*i]))
            .collect()
    };
    (collect(&rows[..first]), collect(&rows[last + 1..]))
}

fn marker_only(ctx: &Ctx, line: &Line) -> bool {
    let text = line.text(ctx);
    let trimmed = text.trim();
    trimmed.chars().count() <= 4
        && (list_marker(&format!("{trimmed} x")).is_some() || trimmed.chars().count() == 1)
}

/// Drop leading and trailing baselines that only one strip uses: they are text
/// above or below the table that happens to leave the gutters free.
fn trim(lines: &[Vec<Line>], size: f32) -> Vec<Vec<Line>> {
    let baselines = cluster(
        lines.iter().flatten().map(|l| l.baseline).collect(),
        0.3 * size,
    );
    let users = |baseline: f32| {
        lines
            .iter()
            .filter(|strip| {
                strip
                    .iter()
                    .any(|l| (l.baseline - baseline).abs() <= 0.3 * size)
            })
            .count()
    };
    let first = baselines.iter().position(|(b, _)| users(*b) >= 2);
    let last = baselines.iter().rposition(|(b, _)| users(*b) >= 2);
    let (Some(first), Some(last)) = (first, last) else {
        return lines.to_vec();
    };
    let (top, bottom) = (
        baselines[first].0 - 0.3 * size,
        baselines[last].0 + 0.3 * size,
    );
    // Wrapped lines of the last row belong to it: extend down while the pitch
    // stays tight.
    let mut bottom = bottom;
    let mut pitch = if last > first {
        (baselines[last].0 - baselines[first].0) / (last - first) as f32
    } else {
        1.4 * size
    };
    pitch = pitch.min(1.6 * size);
    for (baseline, _) in &baselines[last + 1..] {
        if *baseline - bottom <= pitch {
            bottom = *baseline + 0.3 * size;
        } else {
            break;
        }
    }
    lines
        .iter()
        .map(|strip| {
            strip
                .iter()
                .filter(|l| l.baseline >= top && l.baseline <= bottom)
                .cloned()
                .collect()
        })
        .collect()
}
