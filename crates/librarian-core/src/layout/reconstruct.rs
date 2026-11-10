//! Orchestration: per-page segmentation, document-level passes (page furniture,
//! declared headings, heading levels, continuation) and exact accounting.
use super::blocks::{Draft, cell_text, figure_text, flow_blocks};
use super::geom::union_all;
use super::lattice::grids;
use super::regions::{Node, segment};
use super::tables::{TableNode, from_grid};
use super::text::{
    Ctx, DocStats, ends_with_break_hyphen, keep_hyphen, non_space_chars, sentence_end, terminal,
};
use super::*;
use std::collections::{BTreeMap, HashMap, HashSet};

fn element(page: u32, role: Role, bbox: Rect) -> Element {
    Element {
        id: String::new(),
        page,
        role,
        level: None,
        text: String::new(),
        bbox,
        order: 0,
        font_size: 0.0,
        bold: false,
        spans: Vec::new(),
        cells: Vec::new(),
        rows: 0,
        cols: 0,
        continues: false,
        tight: false,
        origin: Origin::Native,
        evidence: Evidence::Layout,
        confidence: 1.0,
        words: Vec::new(),
    }
}

#[derive(Default)]
struct Tally {
    hyphens_removed: u32,
    hyphens_kept: u32,
}

fn from_draft(page: u32, draft: Draft, tally: &mut Tally) -> Element {
    tally.hyphens_removed += draft.hyphens_removed;
    tally.hyphens_kept += draft.hyphens_kept;
    let mut e = element(page, draft.role, draft.bbox);
    e.text = draft.text;
    e.spans = draft.spans;
    e.font_size = draft.size;
    e.bold = draft.bold;
    e.words = draft.words;
    e
}

fn from_table(ctx: &Ctx, page: u32, table: TableNode, tally: &mut Tally) -> Element {
    let mut e = element(page, Role::Table, table.bbox);
    e.rows = table.rows;
    e.cols = table.cols;
    e.confidence = table.confidence;
    e.evidence = if table.ruled {
        Evidence::Ruling
    } else {
        Evidence::Layout
    };
    e.font_size = ctx.stats.body;
    let mut rows: BTreeMap<u32, Vec<String>> = BTreeMap::new();
    for cell in table.cells {
        // Rotated header text reads in the order it was written.
        let rotated = !cell.words.is_empty() && cell.words.iter().all(|id| ctx.word(*id).rotated);
        let composed = if rotated {
            let mut ids = cell.words.clone();
            ids.sort_unstable();
            super::text::Composed {
                text: ids
                    .iter()
                    .map(|id| ctx.word(*id).text.as_str())
                    .collect::<Vec<_>>()
                    .join(" "),
                ..Default::default()
            }
        } else {
            cell_text(ctx, &cell.words)
        };
        tally.hyphens_removed += composed.hyphens_removed;
        tally.hyphens_kept += composed.hyphens_kept;
        rows.entry(cell.row)
            .or_default()
            .push(composed.text.clone());
        e.words.extend(cell.words.iter().copied());
        e.cells.push(Cell {
            row: cell.row,
            col: cell.col,
            rowspan: cell.rowspan,
            colspan: cell.colspan,
            header: cell.header,
            text: composed.text,
            bbox: cell.rect,
            words: cell.words,
        });
    }
    e.text = rows
        .values()
        .map(|cells| cells.join(" | "))
        .collect::<Vec<_>>()
        .join("\n");
    e
}

/// Merge rectangles that overlap or touch into regions.
fn merge_rects(rects: &[Rect], slack: f32) -> Vec<Rect> {
    let mut regions: Vec<Rect> = Vec::new();
    for rect in rects
        .iter()
        .filter(|r| r.finite() && r.w > 0.0 && r.h > 0.0)
    {
        let mut merged = *rect;
        loop {
            let grown = merged.grow(slack);
            match regions
                .iter()
                .position(|r| grown.overlap_x(r) > 0.0 && grown.overlap_y(r) > 0.0)
            {
                Some(index) => merged = merged.union(&regions.swap_remove(index)),
                None => break,
            }
        }
        regions.push(merged);
    }
    regions
}

struct PageResult {
    elements: Vec<Element>,
    /// Flow identity per element; elements of one column share a flow.
    flows: Vec<u32>,
    route: Route,
}

fn page_elements(ctx: &Ctx, tally: &mut Tally) -> PageResult {
    let page = ctx.page;
    let number = page.number;
    let page_area = (page.width * page.height).max(1.0);
    let total = page.words.len();
    let hidden_count = page.words.iter().filter(|w| w.hidden).count();
    // A page whose text is almost all invisible carries an OCR layer: that
    // text is the only reading there is, but it is not the author's.
    let ocr_layer = total >= 8 && hidden_count * 5 >= total * 4;
    // Text placed outside the page box is in the file but not on the page.
    let visible = Rect::new(0.0, 0.0, page.width, page.height).grow(2.0);
    let in_flow = |w: &Word| {
        !w.rotated
            && (!w.hidden || ocr_layer)
            && w.bbox.finite()
            && visible.contains_center(&w.bbox)
    };
    let mut available: Vec<bool> = page.words.iter().map(in_flow).collect();
    let mut taken = vec![false; total];
    let mut atoms: Vec<Node> = Vec::new();
    let mut uncertain_tables: Vec<Rect> = Vec::new();

    // 1. Pictures: raster images first, since they bound what a grid can be.
    let mut figures: Vec<Rect> = Vec::new();
    let mut backgrounds: Vec<Rect> = Vec::new();
    let images: Vec<Rect> = page
        .images
        .iter()
        .filter(|r| r.finite() && r.w >= 14.0 && r.h >= 14.0)
        .copied()
        .collect();
    for region in merge_rects(&images, 1.0) {
        if region.area() < 0.0015 * page_area {
            continue;
        }
        if region.area() >= 0.6 * page_area {
            backgrounds.push(region);
        } else {
            figures.push(region);
        }
    }

    // Type far below reading size, set in clusters, is the content of an
    // illustration (page thumbnails, dense diagrams), not text to read.
    let micro: Vec<Rect> = (0..total)
        .filter(|i| available[*i] && page.words[*i].size <= 0.55 * ctx.stats.body)
        .map(|i| page.words[i].bbox)
        .collect();
    let mut micro_regions: Vec<Rect> = Vec::new();
    for region in merge_rects(&micro, 1.2 * ctx.stats.body) {
        let count = micro.iter().filter(|m| region.contains_center(m)).count();
        if count >= 10 && region.area() >= 0.005 * page_area {
            micro_regions.push(region);
        }
    }
    for region in &micro_regions {
        match figures
            .iter()
            .position(|f| f.intersection_area(region) > 0.3 * region.area().min(f.area()))
        {
            Some(index) => figures[index] = figures[index].union(region),
            None => figures.push(*region),
        }
    }

    // Captions are the author's own statement of what a block is. A caption
    // word ("Figure", "Table") that starts a line, followed by a number.
    let mut captions: Vec<(Rect, bool)> = Vec::new(); // anchor box, is a figure
    for (index, word) in page.words.iter().enumerate() {
        let label = word.text.trim_end_matches(['.', ':']).to_lowercase();
        let figure = matches!(label.as_str(), "figure" | "fig" | "abbildung" | "abb");
        let table = matches!(label.as_str(), "table" | "tabelle" | "tab");
        if !(figure || table) || !available[index] {
            continue;
        }
        let numbered = page
            .words
            .get(index + 1)
            .is_some_and(|next| next.text.chars().next().is_some_and(|c| c.is_ascii_digit()));
        let starts_line = !page.words.iter().any(|other| {
            other.bbox.overlap_y(&word.bbox) > 0.5 * word.bbox.h
                && other.bbox.right() <= word.bbox.x
                && word.bbox.x - other.bbox.right() < 2.0 * word.size
        });
        if numbered && starts_line {
            captions.push((word.bbox, figure));
        }
    }
    let captioned_as_figure = |bbox: &Rect| -> bool {
        captions
            .iter()
            .filter(|(anchor, _)| {
                anchor.x < bbox.right() && anchor.right() > bbox.x - ctx.stats.body
            })
            .filter_map(|(anchor, figure)| {
                let below = anchor.y - bbox.bottom();
                let above = bbox.y - anchor.bottom();
                let distance = if below >= -2.0 { below } else { above };
                (0.0 - 2.0..=6.0 * ctx.stats.body)
                    .contains(&distance)
                    .then_some((distance, *figure))
            })
            .min_by(|a, b| a.0.total_cmp(&b.0))
            .is_some_and(|(_, figure)| figure)
    };

    // 2. Tables whose grid was drawn.
    let mut table_boxes: Vec<Rect> = Vec::new();
    for grid in grids(page) {
        // A grid the author captioned as a figure is a drawing of a table.
        if grid.bbox.area() < 0.25 * page_area && captioned_as_figure(&grid.bbox) {
            match figures
                .iter()
                .position(|f| f.intersection_area(&grid.bbox) > 0.0)
            {
                Some(index) => figures[index] = figures[index].union(&grid.bbox),
                None => figures.push(grid.bbox),
            }
            continue;
        }
        if table_boxes
            .iter()
            .any(|t| t.intersection_area(&grid.bbox) > 0.5 * grid.bbox.area())
        {
            continue;
        }
        // Lines drawn over a picture annotate it; they are not a table.
        if figures
            .iter()
            .any(|f| f.intersection_area(&grid.bbox) > 0.6 * grid.bbox.area())
        {
            continue;
        }
        // Inside a drawn grid, rotated text is cell content (vertical headers).
        let usable: Vec<bool> = (0..total)
            .map(|i| {
                let w = &page.words[i];
                available[i]
                    || (w.rotated
                        && !taken[i]
                        && w.bbox.finite()
                        && visible.contains_center(&w.bbox)
                        && (!w.hidden || ocr_layer))
            })
            .collect();
        if let Some(table) = from_grid(ctx, &grid, &usable) {
            for id in table.words() {
                available[id as usize] = false;
                taken[id as usize] = true;
            }
            table_boxes.push(table.bbox);
            atoms.push(Node::Table(table));
        }
    }

    // Vector figures, with the words drawn inside them. A single shape that
    // holds running text is a panel or frame around content, not a figure:
    // left in, it would glue everything it surrounds into one picture.
    let shapes: Vec<Rect> = page
        .drawings
        .iter()
        .filter(|d| {
            d.area() < 0.02 * page_area
                || (0..total)
                    .filter(|i| {
                        let w = &page.words[*i];
                        available[*i]
                            && w.size >= 0.7 * ctx.stats.body
                            && d.contains_center(&w.bbox)
                            && w.text.chars().any(char::is_alphabetic)
                    })
                    .count()
                    < 20
        })
        .copied()
        .collect();
    for region in merge_rects(&shapes, 4.0) {
        let share = region.area() / page_area;
        if !(0.01..0.7).contains(&share) || region.w < 30.0 || region.h < 20.0 {
            continue;
        }
        if table_boxes
            .iter()
            .any(|t| t.intersection_area(&region) > 0.5 * region.area())
        {
            continue;
        }
        match figures
            .iter()
            .position(|f| f.intersection_area(&region) > 0.3 * region.area().min(f.area()))
        {
            Some(index) => figures[index] = figures[index].union(&region),
            None => figures.push(region),
        }
    }
    let mut meaningful: Vec<Rect> = Vec::new();
    for region in figures {
        let mut inside: Vec<u32> = (0..total as u32)
            .filter(|id| available[*id as usize] && region.contains_center(&ctx.word(*id).bbox))
            .collect();
        // A figure's edge never cuts a line of text: a word whose neighbour
        // on the same line lies outside belongs to that line, not the figure.
        let mut beside: Vec<u32> = (0..total as u32)
            .filter(|id| {
                let b = &ctx.word(*id).bbox;
                available[*id as usize]
                    && !region.contains_center(b)
                    && b.bottom() > region.y
                    && b.y < region.bottom()
            })
            .collect();
        loop {
            let leaving: Vec<u32> = inside
                .iter()
                .copied()
                .filter(|id| {
                    let w = ctx.word(*id);
                    beside.iter().any(|other| {
                        let o = ctx.word(*other);
                        let gap = (w.bbox.x - o.bbox.right()).max(o.bbox.x - w.bbox.right());
                        gap < 0.8 * w.size.max(o.size)
                            && (w.size - o.size).abs() <= 0.12 * w.size
                            && w.bbox.overlap_y(&o.bbox) > 0.6 * w.bbox.h.min(o.bbox.h)
                    })
                })
                .collect();
            if leaving.is_empty() {
                break;
            }
            inside.retain(|id| !leaving.contains(id));
            beside = leaving;
        }
        let ink: f32 = inside.iter().map(|id| ctx.word(*id).bbox.area()).sum();
        let illustration = micro_regions
            .iter()
            .any(|m| m.intersection_area(&region) > 0.5 * m.area());
        // Dense text over a shape is a framed or tinted text box, not a figure.
        if inside.len() > 25 && ink > 0.12 * region.area() && !illustration {
            continue;
        }
        // A line or two of ordinary text on a low band is a heading or label
        // printed over decoration; the text belongs to the reading flow.
        if !inside.is_empty() {
            let mut sizes: Vec<f32> = inside.iter().map(|id| ctx.word(*id).size).collect();
            let size = super::geom::median(&mut sizes).max(1.0);
            let lines = super::text::lines_from(ctx, &inside).len();
            if lines <= 2 && region.h <= 6.0 * size && size >= 0.85 * ctx.stats.body {
                continue;
            }
        }
        for &id in &inside {
            available[id as usize] = false;
        }
        if region.area() >= 0.03 * page_area {
            meaningful.push(region);
        }
        atoms.push(Node::Figure(region, inside));
    }

    // 3. Whitespace segmentation of everything else.
    let free: Vec<u32> = (0..total as u32)
        .filter(|id| available[*id as usize])
        .collect();
    let horizontal: Vec<Rect> = page
        .rules
        .iter()
        .filter(|r| r.h <= 1.5 && r.w >= 6.0)
        .copied()
        .collect();
    let nodes = segment(ctx, &free, atoms, &horizontal);

    let mut elements: Vec<Element> = Vec::new();
    let mut flows: Vec<u32> = Vec::new();
    for background in &backgrounds {
        elements.push(element(number, Role::Picture, *background));
        flows.push(u32::MAX);
    }
    for (flow, node) in nodes.into_iter().enumerate() {
        match node {
            Node::Flow(words) => {
                for draft in flow_blocks(ctx, &words) {
                    elements.push(from_draft(number, draft, tally));
                    flows.push(flow as u32);
                }
            }
            Node::Table(table) => {
                if !table.ruled && table.confidence < 0.6 {
                    uncertain_tables.push(table.bbox);
                }
                elements.push(from_table(ctx, number, table, tally));
                flows.push(flow as u32);
            }
            Node::Figure(rect, words) => {
                let mut e = element(number, Role::Picture, rect);
                let composed = figure_text(ctx, &words);
                tally.hyphens_kept += composed.hyphens_kept;
                tally.hyphens_removed += composed.hyphens_removed;
                e.text = composed.text;
                let mut sizes: Vec<f32> = words.iter().map(|id| ctx.word(*id).size).collect();
                e.font_size = super::geom::median(&mut sizes);
                e.words = words;
                elements.push(e);
                flows.push(flow as u32);
            }
        }
    }
    // 4. Words outside the reading flow stay in the model, out of the text.
    let leftover: Vec<u32> = (0..total as u32)
        .filter(|id| !in_flow(&page.words[*id as usize]) && !taken[*id as usize])
        .collect();
    if !leftover.is_empty() {
        let mut e = element(
            number,
            Role::Hidden,
            union_all(
                leftover
                    .iter()
                    .map(|id| ctx.word(*id).bbox)
                    .filter(Rect::finite),
            ),
        );
        e.text = leftover
            .iter()
            .map(|id| ctx.word(*id).text.as_str())
            .collect::<Vec<_>>()
            .join(" ");
        e.words = leftover;
        elements.push(e);
        flows.push(u32::MAX);
    }
    if ocr_layer {
        for e in &mut elements {
            e.origin = Origin::OcrLayer;
            e.confidence = e.confidence.min(0.8);
        }
    }

    // 5. Routing: where would intelligence earn its cost on this page?
    let alnum: usize = page
        .words
        .iter()
        .map(|w| w.text.chars().filter(|c| c.is_alphanumeric()).count())
        .sum();
    // Private-use code points are what a font without a Unicode mapping
    // yields. A few are symbol bullets; many mean the text cannot be trusted.
    let unmapped: usize = page
        .words
        .iter()
        .filter(|w| !w.hidden)
        .map(|w| {
            w.text
                .chars()
                .filter(|c| matches!(*c as u32, 0xE000..=0xF8FF | 0xF0000..=0x10FFFD))
                .count()
        })
        .sum();
    let mut tier = Tier::Deterministic;
    let mut reasons = Vec::new();
    let mut regions = Vec::new();
    let raise = |tier: &mut Tier, to: Tier| {
        if *tier < to {
            *tier = to;
        }
    };
    if alnum < 16 {
        raise(&mut tier, Tier::Ocr);
        reasons.push(if backgrounds.is_empty() && meaningful.is_empty() {
            "no usable text layer".to_owned()
        } else {
            "scanned or image-only page: no usable text layer".to_owned()
        });
    } else if page.bad_chars * 50 > page.text_chars.max(1) {
        raise(&mut tier, Tier::Ocr);
        reasons.push("text layer contains undecodable characters".to_owned());
    } else if unmapped * 5 > alnum + unmapped {
        raise(&mut tier, Tier::Ocr);
        reasons.push("much of the text uses a font with no Unicode mapping".to_owned());
    } else if ocr_layer {
        raise(&mut tier, Tier::Review);
        reasons.push("text comes from an embedded OCR layer of unknown quality".to_owned());
    }
    if !meaningful.is_empty() {
        raise(&mut tier, Tier::Vision);
        reasons.push(format!(
            "{} figure(s) may carry information not in the text layer",
            meaningful.len()
        ));
        regions.extend(meaningful.iter().copied());
    }
    if !uncertain_tables.is_empty() {
        raise(&mut tier, Tier::Review);
        reasons.push("sparse table grid: cell structure needs confirmation".to_owned());
        regions.extend(uncertain_tables.iter().copied());
    }
    if reasons.is_empty() {
        reasons.push("native text layer reconstructed deterministically".to_owned());
    }
    PageResult {
        elements,
        flows,
        route: Route {
            page: number,
            tier,
            reasons,
            regions,
        },
    }
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

/// The printed number of a folio ("12", "Page 12", "12 of 80"), if the text is one.
fn folio_value(text: &str) -> Option<i64> {
    let t = text.trim();
    if t.is_empty() || t.chars().count() > 14 {
        return None;
    }
    let lower = t.to_lowercase();
    let body = lower.strip_prefix("page ").unwrap_or(&lower);
    let first = body.split(" of ").next().unwrap_or(body).trim();
    let digits = first.trim_matches(|c: char| " -–—|·.".contains(c));
    if !digits.is_empty() && digits.chars().all(|c| c.is_ascii_digit()) {
        return digits.parse().ok();
    }
    None
}

fn roman_folio(text: &str) -> bool {
    let t = text.trim();
    !t.is_empty() && t.chars().count() <= 6 && t.chars().all(|c| "ivxlcdm".contains(c))
}

fn normalized(text: &str) -> String {
    text.chars()
        .filter(|c| c.is_alphanumeric())
        .flat_map(char::to_lowercase)
        .collect()
}

pub fn reconstruct(input: &DocumentInput) -> Document {
    let stats = DocStats::measure(input);
    let body = stats.body;
    let mut tally = Tally::default();
    let mut elements: Vec<Element> = Vec::new();
    let mut flows: Vec<(u32, u32)> = Vec::new();
    let mut routing = Vec::new();
    for page in &input.pages {
        let ctx = Ctx {
            page,
            fonts: &input.fonts,
            stats: &stats,
        };
        let result = page_elements(&ctx, &mut tally);
        for (index, (mut e, flow)) in result.elements.into_iter().zip(result.flows).enumerate() {
            e.id = format!("p{}-e{index}", page.number);
            elements.push(e);
            flows.push((page.number, flow));
        }
        routing.push(result.route);
    }
    let heights: HashMap<u32, f32> = input.pages.iter().map(|p| (p.number, p.height)).collect();
    let page_count = input.pages.len();

    // Page furniture: margin text that recurs across pages, and folios whose
    // printed number keeps a constant offset from the page index.
    let band = |e: &Element| -> Option<bool> {
        let height = heights.get(&e.page).copied().unwrap_or(792.0);
        if e.bbox.bottom() < 0.11 * height {
            Some(true)
        } else if e.bbox.y > 0.89 * height {
            Some(false)
        } else {
            None
        }
    };
    let candidate = |e: &Element| {
        matches!(
            e.role,
            Role::Paragraph | Role::Heading | Role::ListItem | Role::Footnote | Role::Caption
        ) && e.text.chars().count() <= 160
            && band(e).is_some()
    };
    let mut recurrence: HashMap<String, HashSet<u32>> = HashMap::new();
    let mut offsets: HashMap<i64, usize> = HashMap::new();
    for e in elements.iter().filter(|e| candidate(e)) {
        match folio_value(&e.text) {
            Some(value) => *offsets.entry(value - e.page as i64).or_default() += 1,
            None if roman_folio(&e.text) => {}
            None => {
                recurrence
                    .entry(furniture_key(&e.text))
                    .or_default()
                    .insert(e.page);
            }
        }
    }
    let threshold = 3.max(page_count / 10);
    let folio_offset = offsets
        .into_iter()
        .filter(|(_, count)| *count >= 3)
        .max_by_key(|(offset, count)| (*count, -offset.abs()))
        .map(|(offset, _)| offset);
    for e in elements.iter_mut() {
        if !candidate(e) {
            continue;
        }
        let top = band(e) == Some(true);
        let folio = match folio_value(&e.text) {
            Some(value) => {
                if page_count >= 3 {
                    Some(value - e.page as i64) == folio_offset
                } else {
                    // Too few pages to confirm an offset: a bare number alone
                    // at the very edge of the page is its folio.
                    e.text.trim().chars().all(|c| c.is_ascii_digit())
                }
            }
            None => roman_folio(&e.text) && page_count >= 3 && (e.page as usize) < 40,
        };
        // A short document cannot show recurrence. Its running head is still
        // recognisable: one small line at the very top carrying the folio.
        let running_head = page_count < 3
            && top
            && !e.text.contains('\n')
            && e.text.chars().count() <= 120
            && e.font_size <= 1.05 * body
            && {
                let tokens: Vec<&str> = e.text.split_whitespace().collect();
                let number = |t: &&str| {
                    !t.is_empty() && t.len() <= 4 && t.chars().all(|c| c.is_ascii_digit())
                };
                tokens.len() >= 2
                    && (tokens.first().is_some_and(number) != tokens.last().is_some_and(number))
            };
        if folio {
            e.role = Role::PageNumber;
        } else if running_head {
            e.role = Role::Header;
        } else if recurrence
            .get(&furniture_key(&e.text))
            .is_some_and(|pages| pages.len() >= threshold)
        {
            e.role = if top { Role::Header } else { Role::Footer };
        }
    }

    // Headings the author declared in the bookmarks outrank typography.
    for item in &input.outline {
        let want = normalized(&item.title);
        if want.len() < 3 {
            continue;
        }
        let best = elements
            .iter()
            .enumerate()
            .filter(|(_, e)| e.page == item.page && !e.role.furniture())
            .filter(|(_, e)| matches!(e.role, Role::Paragraph | Role::Heading | Role::ListItem))
            .filter(|(_, e)| {
                let have = normalized(&e.text);
                have == want
                    || (have.len() >= 4
                        && have.len() * 10 >= want.len() * 6
                        && (want.ends_with(&have) || want.starts_with(&have)))
                    || (want.len() * 10 >= have.len() * 7 && have.ends_with(&want))
            })
            .min_by(|a, b| {
                let distance = |e: &Element| item.y.map_or(0.0, |y| (e.bbox.y - y).abs());
                distance(a.1).total_cmp(&distance(b.1))
            })
            .map(|(index, _)| index);
        if let Some(index) = best {
            elements[index].role = Role::Heading;
            elements[index].level = Some(item.level.clamp(1, 6));
            elements[index].evidence = Evidence::Declared;
        }
    }
    let declared = elements
        .iter()
        .filter(|e| e.evidence == Evidence::Declared)
        .count();
    // A rich, multi-level outline is the author's own list of headings. A
    // body-size line it does not name, smaller than every heading it does
    // name, is a lead-in label.
    let floor = elements
        .iter()
        .filter(|e| e.evidence == Evidence::Declared)
        .map(|e| e.font_size)
        .fold(f32::INFINITY, f32::min);
    let deep = input.outline.iter().any(|o| o.level >= 2);
    if declared >= 8 && deep {
        for e in elements.iter_mut().filter(|e| {
            e.role == Role::Heading
                && e.evidence != Evidence::Declared
                && e.font_size <= 1.05 * body
                && e.font_size < 0.95 * floor
                && !text::section_number(&e.text)
        }) {
            e.role = Role::Paragraph;
            e.level = None;
        }
    }

    // Heading levels from recurring sizes; the largest rare size at the start
    // of the document is its title.
    let rounded = |size: f32| (size * 2.0).round() / 2.0;
    let mut uses: HashMap<i32, usize> = HashMap::new();
    for e in elements
        .iter()
        .filter(|e| e.role == Role::Heading && e.level.is_none())
    {
        if e.font_size >= 1.15 * body {
            *uses.entry((e.font_size * 2.0).round() as i32).or_default() += 1;
        }
    }
    let mut sizes: Vec<f32> = uses.keys().map(|k| *k as f32 / 2.0).collect();
    sizes.sort_by(|a, b| b.total_cmp(a));
    let largest = sizes.first().copied();
    let title_index = elements.iter().position(|e| {
        e.role == Role::Heading
            && e.level.is_none()
            && e.page <= 2
            && page_count >= 2
            && Some(rounded(e.font_size)) == largest
            && e.font_size >= 1.5 * body
            && uses
                .get(&((e.font_size * 2.0).round() as i32))
                .copied()
                .unwrap_or(0)
                <= 2
    });
    if let Some(index) = title_index {
        elements[index].role = Role::Title;
    }
    let title_size = title_index.map(|i| rounded(elements[i].font_size));
    let ranks: Vec<f32> = sizes
        .iter()
        .copied()
        .filter(|s| {
            let count = uses.get(&((s * 2.0).round() as i32)).copied().unwrap_or(0);
            !(Some(*s) == title_size && count <= 1) && (count >= 2 || sizes.len() <= 2)
        })
        .collect();
    let shift = u8::from(title_index.is_some() || declared > 0);
    for e in elements.iter_mut().filter(|e| e.role == Role::Heading) {
        if e.evidence == Evidence::Declared {
            continue;
        }
        let rank = if e.font_size >= 1.15 * body {
            ranks
                .iter()
                .position(|s| *s <= rounded(e.font_size))
                .unwrap_or(ranks.len().saturating_sub(1))
        } else {
            ranks.len()
        };
        e.level = Some((rank as u8 + 1 + shift).min(6));
    }

    // A paragraph cut by a column or page break continues in the next one.
    let body_indices: Vec<usize> = (0..elements.len())
        .filter(|&i| !elements[i].role.furniture())
        .collect();
    for pair in body_indices.windows(2) {
        let (a, b) = (pair[0], pair[1]);
        if flows[a] == flows[b] || flows[a].1 == u32::MAX || flows[b].1 == u32::MAX {
            continue;
        }
        let (first, second) = (&elements[a], &elements[b]);
        if first.role == Role::Paragraph
            && second.role == Role::Paragraph
            && (first.font_size - second.font_size).abs() <= 0.5
            && !terminal(&first.text)
            && !sentence_end(&first.text)
            && first.text.chars().count() >= 40
            && (second.text.chars().next().is_some_and(char::is_lowercase)
                || first.text.ends_with([',', '-']))
        {
            let last = first.text.split_whitespace().next_back().unwrap_or("");
            let next = second.text.split_whitespace().next().unwrap_or("");
            if ends_with_break_hyphen(last) {
                if keep_hyphen(&stats, last, false, next) {
                    tally.hyphens_kept += 1;
                } else {
                    elements[a].text.pop();
                    let len = elements[a].text.len() as u32;
                    for span in &mut elements[a].spans {
                        span.end = span.end.min(len);
                        span.start = span.start.min(span.end);
                    }
                    tally.hyphens_removed += 1;
                }
                elements[a].tight = true;
            }
            elements[a].continues = true;
        }
    }

    // A table that fills the end of one page and resumes at the top of the
    // next with the same columns is one table.
    for pair in body_indices.windows(2) {
        let (a, b) = (pair[0], pair[1]);
        let (first, second) = (&elements[a], &elements[b]);
        if first.role == Role::Table
            && second.role == Role::Table
            && second.page == first.page + 1
            && first.cols == second.cols
            && first.cols >= 2
            && (first.bbox.x - second.bbox.x).abs() <= 0.04 * first.bbox.w.max(1.0) + 6.0
            && (first.bbox.w - second.bbox.w).abs() <= 0.1 * first.bbox.w.max(1.0)
        {
            elements[a].continues = true;
        }
    }

    // Exact accounting: every word once, every character once.
    let mut seen: Vec<Vec<u8>> = input
        .pages
        .iter()
        .map(|p| vec![0u8; p.words.len()])
        .collect();
    let page_index: HashMap<u32, usize> = input
        .pages
        .iter()
        .enumerate()
        .map(|(i, p)| (p.number, i))
        .collect();
    let mut chars_out = 0u64;
    for e in &elements {
        let Some(&page) = page_index.get(&e.page) else {
            continue;
        };
        let refs: Box<dyn Iterator<Item = &u32>> = if e.cells.is_empty() {
            chars_out += non_space_chars(&e.text);
            Box::new(e.words.iter())
        } else {
            chars_out += e
                .cells
                .iter()
                .map(|c| non_space_chars(&c.text))
                .sum::<u64>();
            Box::new(e.cells.iter().flat_map(|c| c.words.iter()))
        };
        for &word in refs {
            if let Some(count) = seen[page].get_mut(word as usize) {
                *count = count.saturating_add(1);
            }
        }
    }
    let chars_in: u64 = input
        .pages
        .iter()
        .flat_map(|p| p.words.iter())
        .map(|w| non_space_chars(&w.text))
        .sum();

    for (order, e) in elements.iter_mut().enumerate() {
        e.order = order as u32;
    }
    let mut roles = BTreeMap::new();
    for e in &elements {
        *roles.entry(e.role.name().to_owned()).or_insert(0u32) += 1;
    }
    let mut tiers = BTreeMap::new();
    for route in &routing {
        *tiers.entry(route.tier.name().to_owned()).or_insert(0u32) += 1;
    }
    let pages = input
        .pages
        .iter()
        .zip(&routing)
        .map(|(p, r)| PageSummary {
            number: p.number,
            width: p.width,
            height: p.height,
            words: p.words.len() as u32,
            tier: r.tier,
        })
        .collect();
    Document {
        schema: SCHEMA.to_owned(),
        format: input.format.clone(),
        stats: Stats {
            pages: page_count as u32,
            words: input.pages.iter().map(|p| p.words.len() as u32).sum(),
            elements: elements.len() as u32,
            body_font_size: body,
            line_pitch: stats.pitch,
            unassigned_words: seen.iter().flatten().filter(|c| **c == 0).count() as u32,
            duplicated_words: seen.iter().flatten().filter(|c| **c > 1).count() as u32,
            chars_in,
            chars_out,
            hyphens_removed: tally.hyphens_removed,
            hyphens_kept: tally.hyphens_kept,
            roles,
            tiers,
        },
        pages,
        elements,
        routing,
    }
}
