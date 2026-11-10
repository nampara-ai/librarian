//! Invariants that real documents broke at least once. Each test builds a
//! page from positioned words the way a miner would.
use super::*;

fn word(text: &str, x: f32, baseline: f32, size: f32) -> Word {
    let w = text.chars().count() as f32 * size * 0.5;
    Word {
        text: text.into(),
        bbox: Rect::new(x, baseline - size * 0.7, w, size * 0.7),
        size,
        baseline,
        confidence: 1.0,
        ..Default::default()
    }
}

/// Lay out `texts` as a justified line starting at `x`, quarter-em spaces.
fn line(words: &mut Vec<Word>, texts: &[&str], x: f32, baseline: f32, size: f32) {
    let mut cursor = x;
    for text in texts {
        let w = word(text, cursor, baseline, size);
        cursor = w.bbox.right() + size * 0.25;
        words.push(w);
    }
}

fn page(number: u32, words: Vec<Word>) -> PageInput {
    PageInput {
        number,
        width: 612.0,
        height: 792.0,
        text_chars: 500,
        words,
        ..Default::default()
    }
}

fn document(pages: Vec<PageInput>) -> Document {
    reconstruct(&DocumentInput {
        format: "test".into(),
        fonts: vec![Font::default()],
        pages,
        outline: Vec::new(),
    })
}

fn body_text(document: &Document, page: u32) -> String {
    document
        .elements
        .iter()
        .filter(|e| e.page == page && !e.role.furniture())
        .map(|e| e.text.as_str())
        .collect::<Vec<_>>()
        .join(" ")
}

/// Two columns whose baselines do not line up, with one line long enough to
/// poke into the gutter's neighbourhood: each column reads top to bottom, left
/// before right, and nothing is lost or duplicated.
#[test]
fn offset_columns_read_down_each_column() {
    let mut words = Vec::new();
    for row in 0..14 {
        let left: Vec<String> = (0..6).map(|k| format!("l{row}w{k}")).collect();
        let right: Vec<String> = (0..6).map(|k| format!("r{row}w{k}")).collect();
        let left: Vec<&str> = left.iter().map(String::as_str).collect();
        let right: Vec<&str> = right.iter().map(String::as_str).collect();
        line(&mut words, &left, 72.0, 100.0 + row as f32 * 12.0, 10.0);
        // The right column sits on a different baseline grid.
        line(&mut words, &right, 330.0, 104.0 + row as f32 * 12.6, 10.0);
    }
    let document = document(vec![page(1, words)]);
    assert!(document.stats.accounted(), "{:?}", document.stats);
    let text = body_text(&document, 1);
    let left_end = text.find("l13w5").expect("left column present");
    let right_start = text.find("r0w0").expect("right column present");
    assert!(
        left_end < right_start,
        "left column must precede the right: {text}"
    );
    assert!(!document.elements.iter().any(|e| e.role == Role::Table));
}

/// A hyphen at a line end is a compound unless the document shows otherwise.
/// Characters are accounted for either way.
#[test]
fn line_end_hyphens_follow_the_documents_own_vocabulary() {
    let mut words = Vec::new();
    line(
        &mut words,
        &["a", "very", "tight", "four-"],
        72.0,
        100.0,
        10.0,
    );
    line(
        &mut words,
        &["day", "window", "for", "the", "interna-"],
        72.0,
        112.0,
        10.0,
    );
    line(
        &mut words,
        &["tional", "edition", "of", "the", "guide."],
        72.0,
        124.0,
        10.0,
    );
    // Elsewhere the document spells the broken word out in full.
    line(
        &mut words,
        &["An", "international", "audience", "reads", "it."],
        72.0,
        160.0,
        10.0,
    );
    let document = document(vec![page(1, words)]);
    let text = body_text(&document, 1);
    assert!(
        text.contains("four-day"),
        "compound must keep its hyphen: {text}"
    );
    assert!(text.contains("the international edition"), "{text}");
    assert_eq!(document.stats.hyphens_removed, 1);
    assert!(document.stats.accounted(), "{:?}", document.stats);
}

/// Borders drawn one cell at a time, with thin spacer bands between rows:
/// the grid must keep its rows and columns instead of merging through the
/// spacers, and two stacked tables must stay separate.
#[test]
fn piecewise_grid_with_spacer_bands_keeps_cells() {
    let xs = [100.0f32, 220.0, 340.0, 460.0];
    let mut rules = Vec::new();
    let mut words = Vec::new();
    let table = |top: f32, rows: usize, tag: &str, rules: &mut Vec<Rect>, words: &mut Vec<Word>| {
        for r in 0..=rows {
            let y = top + r as f32 * 30.0;
            for track in xs.windows(2) {
                rules.push(Rect::new(track[0], y, track[1] - track[0], 0.0));
            }
        }
        for r in 0..rows {
            let y = top + r as f32 * 30.0;
            for x in xs {
                // A 5 pt spacer piece, then the rest of the row's border.
                rules.push(Rect::new(x, y, 0.0, 5.0));
                rules.push(Rect::new(x, y + 5.0, 0.0, 25.0));
            }
            for (c, x) in xs.iter().take(3).enumerate() {
                words.push(word(&format!("{tag}{r}{c}"), x + 6.0, y + 20.0, 10.0));
            }
        }
    };
    table(100.0, 4, "a", &mut rules, &mut words);
    table(300.0, 3, "b", &mut rules, &mut words);
    let mut input = page(1, words);
    input.rules = rules;
    let document = document(vec![input]);
    let tables: Vec<&Element> = document
        .elements
        .iter()
        .filter(|e| e.role == Role::Table)
        .collect();
    assert_eq!(tables.len(), 2, "stacked tables stay separate");
    assert_eq!((tables[0].rows, tables[0].cols), (4, 3));
    assert_eq!((tables[1].rows, tables[1].cols), (3, 3));
    assert!(
        tables[0]
            .cells
            .iter()
            .all(|c| c.rowspan == 1 && c.colspan == 1)
    );
    assert_eq!(tables[0].cells[4].text, "a11");
    assert!(document.stats.accounted());
}

/// Text placed outside the page box, drawn invisibly, or rotated stays in the
/// model but never enters the reading text.
#[test]
fn text_off_the_page_is_kept_out_of_the_reading_flow() {
    let mut words = Vec::new();
    line(
        &mut words,
        &["Visible", "body", "text", "here."],
        72.0,
        100.0,
        10.0,
    );
    line(
        &mut words,
        &["offpage", "spread", "text"],
        -400.0,
        100.0,
        10.0,
    );
    let mut hidden = word("invisible", 72.0, 130.0, 10.0);
    hidden.hidden = true;
    words.push(hidden);
    let mut stamp = word("stamp", 20.0, 400.0, 10.0);
    stamp.rotated = true;
    words.push(stamp);
    let document = document(vec![page(1, words)]);
    let text = body_text(&document, 1);
    assert_eq!(text, "Visible body text here.");
    assert!(document.stats.accounted());
    assert!(markdown(&document, RenderOptions::default()).trim() == "Visible body text here.");
}
