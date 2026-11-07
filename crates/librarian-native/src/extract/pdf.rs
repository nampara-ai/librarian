use super::*;
use pdfium_render::prelude::*;

pub(super) fn extract(
    library: &Library,
    job: i64,
    document: &str,
    path: &Path,
    config: &Config,
) -> Result<()> {
    let name = Pdfium::pdfium_platform_library_name();
    let lib = crate::assets::locate(
        &library.root.join("engines"),
        "pdfium",
        &name.to_string_lossy(),
    )?;
    let pdfium =
        Pdfium::new(Pdfium::bind_to_library(lib).context("Cannot load pinned PDFium library")?);
    let pdf = pdfium
        .load_pdf_from_file(path, None)
        .context("Cannot open PDF (encrypted documents require an unencrypted copy)")?;
    let total = pdf.pages().len();
    library.progress(job, "extracting and rendering PDF", 0, total as u64)?;
    let key = |text: &str| {
        let text = text.split_whitespace().collect::<Vec<_>>().join(" ");
        if text
            .chars()
            .all(|c| c.is_ascii_digit() || " -–—".contains(c))
        {
            "<page-number>".to_owned()
        } else {
            text
        }
    };
    let mut furniture: BTreeMap<String, std::collections::BTreeSet<i32>> = BTreeMap::new();
    if config.processing.remove_page_furniture && total >= 3 {
        for index in 0..total {
            if !library.active(job)? {
                bail!("Job paused or cancelled");
            }
            let page = pdf.pages().get(index)?;
            let height = page.height().value;
            for segment in page.text()?.segments().iter() {
                let bounds = segment.bounds();
                let value = key(&segment.text());
                if !value.is_empty()
                    && value.len() <= 120
                    && (bounds.bottom().value > height * 0.92 || bounds.top().value < height * 0.08)
                {
                    furniture.entry(value).or_default().insert(index);
                }
            }
        }
        furniture.retain(|_, pages| pages.len() >= 3 && pages.len() * 5 >= total as usize * 2);
    }
    for index in 0..total {
        if !library.active(job)? {
            bail!("Job paused or cancelled");
        }
        if observed(library, document, index as u64)? {
            continue;
        }
        let page = pdf.pages().get(index)?;
        let id = format!("page-{}", index + 1);
        let text = page.text()?;
        let raw = text.all();
        let number = index as u32 + 1;
        let replacement = raw
            .chars()
            .filter(|c| *c == '\u{fffd}' || (*c == '\0'))
            .count();
        let usable = raw.chars().filter(|c| c.is_alphanumeric()).count() > 16
            && replacement * 100 < raw.chars().count().max(1);
        let mut start = 0;
        let mut end = raw.len();
        if !furniture.is_empty() {
            let mut margin_keys = std::collections::BTreeSet::new();
            for segment in text.segments().iter() {
                let bounds = segment.bounds();
                if bounds.bottom().value > page.height().value * 0.92
                    || bounds.top().value < page.height().value * 0.08
                {
                    margin_keys.insert(key(&segment.text()));
                }
            }
            for line in raw.split_inclusive('\n').take(3) {
                let value = key(line);
                if value.is_empty()
                    || (furniture.contains_key(&value) && margin_keys.contains(&value))
                {
                    start += line.len();
                } else {
                    break;
                }
            }
            for line in raw[start..].split_inclusive('\n').rev().take(3) {
                let value = key(line);
                if value.is_empty()
                    || (furniture.contains_key(&value) && margin_keys.contains(&value))
                {
                    end -= line.len();
                } else {
                    break;
                }
            }
            if start >= end {
                start = 0;
                end = raw.len();
            }
        }
        let mut blocks = plain_blocks(&raw[start..end], &id, number, config.processing.chunk_bytes);
        if start > 0 || end < raw.len() {
            for block in &mut blocks {
                for evidence in &mut block.evidence {
                    evidence.start += start as u32;
                    evidence.end += start as u32;
                }
            }
            library.event(job,"exclusion",&serde_json::to_string(&serde_json::json!({"page":number,"reason":"repeated_margin_furniture","kept_source_range":[start,end]}))?)?;
        }
        let mut visuals = Vec::new();
        let mut findings = Vec::new();
        let paths = page
            .objects()
            .iter()
            .filter(|o| o.as_path_object().is_some())
            .count();
        let mut boxes: Vec<_> = text
            .segments()
            .iter()
            .map(|s| {
                let r = s.bounds();
                (
                    r.left().value,
                    r.bottom().value,
                    r.right().value,
                    r.top().value,
                    s.text(),
                )
            })
            .collect();
        boxes.sort_by(|a, b| b.3.total_cmp(&a.3).then_with(|| a.0.total_cmp(&b.0)));
        let width = page.width().value;
        let left = boxes
            .iter()
            .filter(|b| b.2 < width * 0.53 && !b.4.trim().is_empty())
            .count();
        let right = boxes
            .iter()
            .filter(|b| b.0 > width * 0.47 && !b.4.trim().is_empty())
            .count();
        let complex = paths > 12 || (left > 4 && right > 4);
        if config.processing.pipeline == "editorial" || !usable || complex {
            let bitmap = page.render_with_config(
                &PdfRenderConfig::new()
                    .set_target_width(config.processing.render_width as i32)
                    .set_maximum_height((config.processing.render_width * 2) as i32),
            )?;
            let asset = save_image(library, &bitmap.as_image()?)?;
            visuals.push(Visual {
                asset,
                purpose: if complex { "complex_page" } else { "page" }.into(),
                replace_page: true,
                after_block: 0,
            });
            if !usable {
                blocks.clear();
            }
            if complex && !config.processing.vision {
                findings.push(
                    "Complex page layout needs visual interpretation; native text retained".into(),
                );
            }
        } else {
            for object in page.objects().iter() {
                if let Some(image) = object.as_image_object() {
                    if image.width().unwrap_or(0) < 64 || image.height().unwrap_or(0) < 64 {
                        continue;
                    }
                    match image.get_processed_image(&pdf) {
                        Ok(image) => {
                            let asset = save_image(library, &image)?;
                            visuals.push(Visual {
                                asset,
                                purpose: "figure".into(),
                                replace_page: false,
                                after_block: blocks.len(),
                            });
                        }
                        Err(_) => findings.push(
                            "An embedded image could not be extracted; original PDF retained"
                                .into(),
                        ),
                    }
                }
            }
        }
        if raw.trim().is_empty() && visuals.is_empty() {
            findings.push("Page has no recognized content".into());
        }
        for block in &mut blocks {
            block.findings.extend(findings.clone());
        }
        let observation = Observation {
            page: number,
            method: "pdfium-8066".into(),
            raw_text: raw,
            blocks,
            visuals,
            findings,
        };
        persist(library, job, document, index as u64, &observation)?;
        library.progress(job, "extracting", index as u64 + 1, total as u64)?;
        // Geometry is separate evidence, never substituted for the original text.
        library.db.execute(
            "INSERT OR IGNORE INTO events(job,level,message,created) VALUES(?,'geometry',?,?)",
            params![
                job,
                serde_json::to_string(&serde_json::json!({"page":number,"regions":boxes}))?,
                crate::store::now()
            ],
        )?;
    }
    Ok(())
}
