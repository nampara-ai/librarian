//! Compare `librarian parse` against a Nutrient `/extraction/parse` response.
//!
//! cargo run --release -p librarian-native --example layout_bench -- \
//!     --pdfium /path/to/libpdfium.so document.pdf nutrient.json [more.pdf more.json ...]
//!
//! Nutrient's output must include spatial elements with `includeWords: true`.
//! Scores measure agreement with Nutrient, not verified ground truth. Fixtures
//! stay outside the repository.
use anyhow::{Context, Result, bail};
use librarian_core::layout::{Document, DocumentInput, Role};
use librarian_native::parse;
use serde_json::Value;
use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    path::PathBuf,
};

#[derive(Clone)]
struct W {
    page: u32,
    norm: String,
    cx: f32,
    cy: f32,
    h: f32,
    role: &'static str,
    group: String,
    order: (u32, u32),
    text: String,
}

fn normalize(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars().flat_map(char::to_lowercase) {
        match c {
            '’' | '‘' | '′' => out.push('\''),
            '“' | '”' | '″' => out.push('"'),
            '–' | '—' | '‐' | '‑' => out.push('-'),
            'ﬁ' => out.push_str("fi"),
            'ﬂ' => out.push_str("fl"),
            'ﬀ' => out.push_str("ff"),
            'ﬃ' => out.push_str("ffi"),
            'ﬄ' => out.push_str("ffl"),
            '\u{ad}' => {}
            _ => out.push(c),
        }
    }
    out
}

fn our_role(role: Role) -> &'static str {
    match role {
        // Nutrient's Title labels are inconsistent; titles are scored as headings.
        Role::Title | Role::Heading => "SectionHeader",
        Role::Paragraph => "Text",
        Role::ListItem => "ListItem",
        Role::Table => "Table",
        Role::Code => "Code",
        Role::Caption => "Caption",
        Role::Header | Role::Footer | Role::PageNumber => "Footer",
        Role::Picture => "Picture",
    }
}

fn ours(input: &DocumentInput, document: &Document, scale: &HashMap<u32, (f32, f32)>) -> Vec<W> {
    let mut out = Vec::new();
    for element in &document.elements {
        let page = &input.pages[element.page as usize - 1];
        let (sx, sy) = scale.get(&element.page).copied().unwrap_or((1.0, 1.0));
        let mut push = |ids: &[u32], group: String, base: u32| {
            for (k, id) in ids.iter().enumerate() {
                let word = &page.words[*id as usize];
                out.push(W {
                    page: element.page,
                    norm: normalize(&word.text),
                    text: word.text.clone(),
                    cx: word.bbox.cx() * sx,
                    cy: word.bbox.cy() * sy,
                    h: word.bbox.h * sy,
                    role: our_role(element.role),
                    group: group.clone(),
                    order: (element.order, base + k as u32),
                });
            }
        };
        if element.cells.is_empty() {
            push(&element.words, element.id.clone(), 0);
        } else {
            let mut base = 0;
            for cell in &element.cells {
                push(
                    &cell.words,
                    format!("{}:{}:{}", element.id, cell.row, cell.col),
                    base,
                );
                base += cell.words.len() as u32;
            }
        }
    }
    out
}

type Reference = (Vec<W>, HashMap<u32, (f32, f32)>, BTreeMap<u32, String>);

fn theirs(json: &Value) -> Result<Reference> {
    let elements = json["output"]["elements"]
        .as_array()
        .context("Nutrient JSON has no output.elements")?;
    let mut out = Vec::new();
    let mut dims = HashMap::new();
    let mut routes = BTreeMap::new();
    if let Some(pages) = json["output"]["routing"].as_array() {
        for p in pages {
            if let (Some(n), Some(m)) = (p["page"].as_u64(), p["tier"].as_str()) {
                routes.insert(n as u32, m.to_owned());
            }
        }
    }
    for (index, element) in elements.iter().enumerate() {
        let page = element["page"]["pageNumber"].as_u64().unwrap_or(0) as u32;
        dims.insert(
            page,
            (
                element["page"]["width"].as_f64().unwrap_or(1.0) as f32,
                element["page"]["height"].as_f64().unwrap_or(1.0) as f32,
            ),
        );
        // Several elements can share a readingOrder; their array position breaks ties.
        let order = (element["readingOrder"].as_u64().unwrap_or(0) as u32)
            .saturating_mul(65_536)
            .saturating_add(index as u32);
        let id = element["id"].as_str().unwrap_or("").to_owned();
        let mut push = |words: &Value, role: &'static str, group: String, base: u32| -> u32 {
            let mut n = 0;
            for word in words.as_array().into_iter().flatten() {
                let b = &word["bounds"];
                let (x, y, w, h) = (
                    b["x"].as_f64().unwrap_or(0.0) as f32,
                    b["y"].as_f64().unwrap_or(0.0) as f32,
                    b["width"].as_f64().unwrap_or(0.0) as f32,
                    b["height"].as_f64().unwrap_or(0.0) as f32,
                );
                out.push(W {
                    page,
                    norm: normalize(word["text"].as_str().unwrap_or("")),
                    text: word["text"].as_str().unwrap_or("").to_owned(),
                    cx: x + w / 2.0,
                    cy: y + h / 2.0,
                    h,
                    role,
                    group: group.clone(),
                    order: (order, base + n),
                });
                n += 1;
            }
            n
        };
        match element["type"].as_str() {
            Some("table") => {
                // Nutrient serializes cells column by column; read them row by row.
                let mut cells: Vec<&Value> =
                    element["cells"].as_array().into_iter().flatten().collect();
                cells.sort_by_key(|c| {
                    (
                        c["row"].as_u64().unwrap_or(0),
                        c["column"].as_u64().unwrap_or(0),
                    )
                });
                let mut base = 0;
                for cell in cells {
                    let group = format!(
                        "{id}:{}:{}",
                        cell["row"].as_u64().unwrap_or(0),
                        cell["column"].as_u64().unwrap_or(0)
                    );
                    base += push(&cell["words"], "Table", group, base);
                }
            }
            Some("paragraph") => {
                let role = match element["role"].as_str().unwrap_or("Text") {
                    "Title" | "SectionHeader" => "SectionHeader",
                    "ListItem" => "ListItem",
                    "Code" => "Code",
                    "Caption" => "Caption",
                    "Footer" | "Header" | "PageHeader" | "PageFooter" => "Footer",
                    _ => "Text",
                };
                push(&element["words"], role, id, 0);
            }
            _ => {}
        }
    }
    Ok((out, dims, routes))
}

/// Page-local one-to-one matching on normalized text and nearest center.
fn matches(a: &[W], b: &[W]) -> Vec<(usize, usize)> {
    let mut index: HashMap<(u32, &str), Vec<usize>> = HashMap::new();
    for (j, w) in b.iter().enumerate() {
        index.entry((w.page, w.norm.as_str())).or_default().push(j);
    }
    let mut used = vec![false; b.len()];
    let mut pairs = Vec::new();
    for (i, w) in a.iter().enumerate() {
        let limit = (w.h * 3.0).max(30.0);
        let best = index
            .get(&(w.page, w.norm.as_str()))
            .into_iter()
            .flatten()
            .filter(|&&j| !used[j])
            .map(|&j| (j, (b[j].cx - w.cx).hypot(b[j].cy - w.cy)))
            .filter(|(_, d)| *d <= limit)
            .min_by(|x, y| x.1.total_cmp(&y.1));
        if let Some((j, _)) = best {
            used[j] = true;
            pairs.push((i, j));
        }
    }
    pairs
}

fn pairs_f1(pairs: &[(usize, usize)], a: &[W], b: &[W]) -> (f64, f64, f64) {
    let c2 = |n: u64| n * n.saturating_sub(1) / 2;
    let mut joint: HashMap<(&str, &str), u64> = HashMap::new();
    let mut ours: HashMap<&str, u64> = HashMap::new();
    let mut ref_: HashMap<&str, u64> = HashMap::new();
    for &(i, j) in pairs {
        *joint
            .entry((a[i].group.as_str(), b[j].group.as_str()))
            .or_default() += 1;
        *ours.entry(a[i].group.as_str()).or_default() += 1;
        *ref_.entry(b[j].group.as_str()).or_default() += 1;
    }
    let tp: u64 = joint.values().map(|n| c2(*n)).sum();
    let predicted: u64 = ours.values().map(|n| c2(*n)).sum();
    let actual: u64 = ref_.values().map(|n| c2(*n)).sum();
    let p = tp as f64 / predicted.max(1) as f64;
    let r = tp as f64 / actual.max(1) as f64;
    (p, r, 2.0 * p * r / (p + r).max(1e-12))
}

fn inversions(values: &mut [u64]) -> u64 {
    if values.len() < 2 {
        return 0;
    }
    let mid = values.len() / 2;
    let mut count = inversions(&mut values[..mid]) + inversions(&mut values[mid..]);
    let mut merged = Vec::with_capacity(values.len());
    let (mut i, mut j) = (0, mid);
    while i < mid && j < values.len() {
        if values[i] <= values[j] {
            merged.push(values[i]);
            i += 1;
        } else {
            count += (mid - i) as u64;
            merged.push(values[j]);
            j += 1;
        }
    }
    merged.extend_from_slice(&values[i..mid]);
    merged.extend_from_slice(&values[j..]);
    values.copy_from_slice(&merged);
    count
}

fn main() -> Result<()> {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let mut library = None;
    let explain = args.iter().any(|a| a == "--explain");
    args.retain(|a| a != "--explain");
    if let Some(i) = args.iter().position(|a| a == "--pdfium") {
        library = Some(PathBuf::from(
            args.get(i + 1).context("--pdfium needs a path")?,
        ));
        args.drain(i..=i + 1);
    }
    if args.is_empty() || !args.len().is_multiple_of(2) {
        bail!("usage: layout_bench [--pdfium LIB] doc.pdf nutrient.json [...]");
    }
    let engine = parse::pdfium(std::path::Path::new("."), library.as_deref())?;
    for pair in args.chunks(2) {
        let parsed = parse::parse(&engine, std::path::Path::new(&pair[0]))?;
        let json: Value = serde_json::from_slice(&std::fs::read(&pair[1])?)?;
        let (b, dims, routes) = theirs(&json)?;
        let scale: HashMap<u32, (f32, f32)> = parsed
            .input
            .pages
            .iter()
            .filter_map(|p| {
                dims.get(&p.number)
                    .map(|(w, h)| (p.number, (w / p.width, h / p.height)))
            })
            .collect();
        let a = ours(&parsed.input, &parsed.document, &scale);
        let pairs = matches(&a, &b);
        let (gp, gr, gf) = pairs_f1(&pairs, &a, &b);

        let mut roles: BTreeMap<&str, (u64, u64, u64)> = BTreeMap::new();
        for &(i, j) in &pairs {
            roles.entry(a[i].role).or_default().0 += 1;
            roles.entry(b[j].role).or_default().1 += 1;
            if a[i].role == b[j].role {
                roles.entry(a[i].role).or_default().2 += 1;
            }
        }
        let mut confusion: BTreeMap<(&str, &str), u64> = BTreeMap::new();
        for &(i, j) in &pairs {
            if a[i].role != b[j].role {
                *confusion.entry((a[i].role, b[j].role)).or_default() += 1;
            }
        }
        let mut by_page: BTreeMap<u32, Vec<(usize, usize)>> = BTreeMap::new();
        for &(i, j) in &pairs {
            by_page.entry(a[i].page).or_default().push((i, j));
        }
        let (mut inv, mut total) = (0u64, 0u64);
        let mut worst = Vec::new();
        for (page, mut list) in by_page {
            list.sort_by_key(|&(i, _)| a[i].order);
            let mut seq: Vec<u64> = list
                .iter()
                .map(|&(_, j)| ((b[j].order.0 as u64) << 32) | b[j].order.1 as u64)
                .collect();
            let n = seq.len() as u64;
            let k = inversions(&mut seq);
            inv += k;
            total += n * n.saturating_sub(1) / 2;
            worst.push((k as f64 / (n * n.saturating_sub(1) / 2).max(1) as f64, page));
        }
        worst.sort_by(|x, y| y.0.total_cmp(&x.0));
        let table_pages = |words: &[W]| -> BTreeSet<u32> {
            words
                .iter()
                .filter(|w| w.role == "Table")
                .map(|w| w.page)
                .collect()
        };
        let (ta, tb) = (table_pages(&a), table_pages(&b));
        let shared = ta.intersection(&tb).count();

        println!("## {}", pair[0]);
        println!();
        println!("| Measure | Value |\n| --- | --- |");
        println!(
            "| Parse time | {:.0} ms (open {:.0}, extract {:.0}, layout {:.0}) |",
            parsed.timing.open_ms + parsed.timing.extract_ms + parsed.timing.layout_ms,
            parsed.timing.open_ms,
            parsed.timing.extract_ms,
            parsed.timing.layout_ms
        );
        if let Some(ms) = json["metrics"]["processingTimeMs"].as_f64() {
            println!("| Nutrient time | {:.0} ms |", ms);
        }
        println!(
            "| Words ours / Nutrient / matched | {} / {} / {} |",
            a.len(),
            b.len(),
            pairs.len()
        );
        println!(
            "| Coverage of ours / of Nutrient | {:.2}% / {:.2}% |",
            100.0 * pairs.len() as f64 / a.len().max(1) as f64,
            100.0 * pairs.len() as f64 / b.len().max(1) as f64
        );
        println!(
            "| Grouping precision / recall / F1 | {:.2}% / {:.2}% / {:.2}% |",
            gp * 100.0,
            gr * 100.0,
            gf * 100.0
        );
        println!(
            "| Reading-order discordance | {:.3}% |",
            100.0 * inv as f64 / total.max(1) as f64
        );
        println!(
            "| Table pages ours / Nutrient / shared | {} / {} / {} |",
            ta.len(),
            tb.len(),
            shared
        );
        if !routes.is_empty() {
            let mut counts: BTreeMap<&str, u32> = BTreeMap::new();
            for m in routes.values() {
                *counts.entry(m.as_str()).or_default() += 1;
            }
            println!("| Nutrient routing | {counts:?} |");
        }
        println!("| Our routing | {:?} |", parsed.document.stats.tiers);
        println!();
        println!(
            "| Role | Ours | Nutrient | Precision | Recall |\n| --- | --- | --- | --- | --- |"
        );
        for (role, (p, r, both)) in &roles {
            println!(
                "| {role} | {p} | {r} | {:.1}% | {:.1}% |",
                100.0 * *both as f64 / (*p).max(1) as f64,
                100.0 * *both as f64 / (*r).max(1) as f64
            );
        }
        println!();
        let mut confusion: Vec<_> = confusion.into_iter().collect();
        confusion.sort_by_key(|y| std::cmp::Reverse(y.1));
        println!(
            "Top role confusions (ours -> Nutrient): {}",
            confusion
                .iter()
                .take(8)
                .map(|((x, y), n)| format!("{x}->{y} {n}"))
                .collect::<Vec<_>>()
                .join(", ")
        );
        if explain {
            for ((x, y), _) in confusion.iter().take(5) {
                let mut groups: BTreeMap<&str, Vec<usize>> = BTreeMap::new();
                for &(i, j) in &pairs {
                    if a[i].role == *x && b[j].role == *y {
                        groups.entry(b[j].group.as_str()).or_default().push(i);
                    }
                }
                println!("Confusion {x}->{y}: {} Nutrient groups", groups.len());
                for words in groups.values().take(5) {
                    let text: Vec<&str> =
                        words.iter().take(16).map(|&i| a[i].text.as_str()).collect();
                    println!("  p{}: {}", a[words[0]].page, text.join(" "));
                }
            }
            // Nutrient groups split across the most of our groups, and vice versa.
            for (label, from_ours) in [
                ("Nutrient group split by ours", false),
                ("Our group spanning Nutrient groups", true),
            ] {
                let mut spread: HashMap<&str, (BTreeSet<&str>, Vec<usize>)> = HashMap::new();
                for &(i, j) in &pairs {
                    let (key, other) = if from_ours {
                        (a[i].group.as_str(), b[j].group.as_str())
                    } else {
                        (b[j].group.as_str(), a[i].group.as_str())
                    };
                    let entry = spread.entry(key).or_default();
                    entry.0.insert(other);
                    entry.1.push(i);
                }
                let mut ranked: Vec<_> =
                    spread.into_iter().filter(|(_, v)| v.0.len() > 1).collect();
                ranked.sort_by_key(|y| std::cmp::Reverse(y.1.1.len()));
                println!("{label}: {} groups", ranked.len());
                for (_, (others, words)) in ranked.iter().take(6) {
                    let text: Vec<&str> =
                        words.iter().take(40).map(|&i| a[i].text.as_str()).collect();
                    println!(
                        "  p{} into {}: {}",
                        a[words[0]].page,
                        others.len(),
                        text.join(" ")
                    );
                }
            }
        }
        println!(
            "Worst reading-order pages: {}",
            worst
                .iter()
                .take(6)
                .map(|(r, p)| format!("p{p} {:.1}%", r * 100.0))
                .collect::<Vec<_>>()
                .join(", ")
        );
        println!();
    }
    Ok(())
}
