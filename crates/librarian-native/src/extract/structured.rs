use super::*;
use std::io::BufRead;

pub(super) fn delimited(
    library: &Library,
    job: i64,
    document: &str,
    path: &Path,
    delimiter: u8,
) -> Result<()> {
    let mut reader = csv::ReaderBuilder::new()
        .delimiter(delimiter)
        .has_headers(false)
        .flexible(true)
        .from_path(path)?;
    let mut records = reader.records();
    let mut ordinal = 0;
    loop {
        let mut rows = Vec::new();
        let mut bytes = 0;
        for record in records.by_ref().take(256) {
            let row = record?;
            bytes += row.as_slice().len();
            if row.len() > 1000 || row.as_slice().len() > 8 * 1024 * 1024 {
                bail!("Delimited record exceeds column or size limit");
            }
            rows.push(row);
            if bytes > 256 * 1024 {
                break;
            }
        }
        if rows.is_empty() {
            break;
        }
        let columns = rows.iter().map(csv::StringRecord::len).max().unwrap_or(0);
        let raw = rows
            .iter()
            .map(|r| r.iter().collect::<Vec<_>>().join("\t"))
            .collect::<Vec<_>>()
            .join("\n");
        let cells = rows
            .iter()
            .enumerate()
            .flat_map(|(r, row)| {
                (0..columns).map(move |c| Cell {
                    row: r as u32,
                    column: c as u32,
                    row_span: 1,
                    column_span: 1,
                    header: false,
                    text: row.get(c).unwrap_or("").into(),
                })
            })
            .collect();
        let block = segment(
            &format!("records-{ordinal}"),
            0,
            0,
            raw.len(),
            Content::Table {
                rows: rows.len() as u32,
                columns: columns as u32,
                cells,
                caption: String::new(),
            },
        );
        persist(
            library,
            job,
            document,
            ordinal,
            &Observation {
                page: 0,
                method: "delimited-records".into(),
                raw_text: raw,
                blocks: vec![block],
                visuals: vec![],
                findings: vec![],
            },
        )?;
        ordinal += 1;
    }
    Ok(())
}

pub(super) fn subtitles(
    library: &Library,
    job: i64,
    document: &str,
    path: &Path,
    config: &Config,
) -> Result<()> {
    let mut reader = BufReader::new(File::open(path)?);
    let mut cue = String::new();
    let mut ordinal = 0;
    loop {
        let mut line = String::new();
        let n = reader.by_ref().take(1024 * 1024 + 1).read_line(&mut line)?;
        if n > 1024 * 1024 {
            bail!("Subtitle line exceeds 1 MiB");
        }
        if n == 0 || line.trim().is_empty() {
            if !cue.trim().is_empty() {
                let id = format!("cue-{ordinal}");
                let lines: Vec<_> = cue.lines().collect();
                let mut blocks = Vec::new();
                if let Some(timing) = lines.iter().position(|s| s.contains(" --> ")) {
                    let label = lines[..=timing].join(" · ");
                    blocks.push(segment(
                        &format!("{id}-time"),
                        0,
                        0,
                        label.len(),
                        Content::Markdown {
                            text: format!("**{}**", label.replace('*', "\\*")),
                        },
                    ));
                    let text = lines[timing + 1..].join("\n");
                    blocks.extend(plain_blocks(&text, &id, 0, config.processing.chunk_bytes));
                } else {
                    blocks.push(segment(
                        &id,
                        0,
                        0,
                        cue.len(),
                        Content::Code {
                            language: String::new(),
                            text: cue.trim().into(),
                        },
                    ));
                }
                persist(
                    library,
                    job,
                    document,
                    ordinal,
                    &Observation {
                        page: 0,
                        method: "subtitle-cue".into(),
                        raw_text: std::mem::take(&mut cue),
                        blocks,
                        visuals: vec![],
                        findings: vec![],
                    },
                )?;
                ordinal += 1;
            }
            if n == 0 {
                break;
            }
        } else {
            cue.push_str(&line);
            if cue.len() > 8 * 1024 * 1024 {
                bail!("Subtitle cue exceeds 8 MiB");
            }
        }
    }
    Ok(())
}

fn image_page(
    library: &Library,
    job: i64,
    document: &str,
    ordinal: u64,
    image: image::DynamicImage,
) -> Result<()> {
    let asset = save_image(library, &image)?;
    persist(
        library,
        job,
        document,
        ordinal,
        &Observation {
            page: u32::try_from(ordinal + 1)?,
            method: "image-frame".into(),
            raw_text: String::new(),
            blocks: vec![],
            visuals: vec![Visual {
                asset,
                purpose: "page".into(),
                replace_page: true,
                after_block: 0,
            }],
            findings: vec![],
        },
    )
}

pub(super) fn gif(library: &Library, job: i64, document: &str, path: &Path) -> Result<()> {
    use image::{AnimationDecoder, ImageDecoder};
    let mut decoder = image::codecs::gif::GifDecoder::new(BufReader::new(File::open(path)?))?;
    let mut limits = image::Limits::default();
    limits.max_image_width = Some(16000);
    limits.max_image_height = Some(16000);
    limits.max_alloc = Some(512 * 1024 * 1024);
    decoder.set_limits(limits)?;
    for (ordinal, frame) in decoder.into_frames().enumerate() {
        if !library.active(job)? {
            bail!("Job paused or cancelled");
        }
        if ordinal >= 100000 {
            bail!("Animation exceeds frame limit");
        }
        if !observed(library, document, ordinal as u64)? {
            image_page(
                library,
                job,
                document,
                ordinal as u64,
                image::DynamicImage::ImageRgba8(frame?.into_buffer()),
            )?;
        }
    }
    Ok(())
}

pub(super) fn tiff(library: &Library, job: i64, document: &str, path: &Path) -> Result<()> {
    use tiff::{
        ColorType,
        decoder::{Decoder, DecodingResult},
    };
    let mut decoder = Decoder::new(BufReader::new(File::open(path)?))?;
    let mut ordinal = 0;
    loop {
        if !library.active(job)? {
            bail!("Job paused or cancelled");
        }
        let (width, height) = decoder.dimensions()?;
        if width == 0
            || height == 0
            || width > 16000
            || height > 16000
            || u64::from(width) * u64::from(height) > 100_000_000
        {
            bail!("TIFF frame exceeds pixel limit");
        }
        if !observed(library, document, ordinal)? {
            let color = decoder.colortype()?;
            let data = decoder.read_image()?;
            let image = match (color, data) {
                (ColorType::Gray(1), DecodingResult::U8(data)) => {
                    let stride = width.div_ceil(8) as usize;
                    let mut pixels = Vec::with_capacity(width as usize * height as usize);
                    for row in data.chunks(stride).take(height as usize) {
                        for x in 0..width as usize {
                            pixels.push(if row[x / 8] & (128 >> (x % 8)) != 0 {
                                255
                            } else {
                                0
                            });
                        }
                    }
                    image::DynamicImage::ImageLuma8(
                        image::ImageBuffer::from_raw(width, height, pixels)
                            .context("Invalid TIFF bitmap")?,
                    )
                }
                (ColorType::Gray(8), DecodingResult::U8(d)) => image::DynamicImage::ImageLuma8(
                    image::ImageBuffer::from_raw(width, height, d)
                        .context("Invalid TIFF pixels")?,
                ),
                (ColorType::GrayA(8), DecodingResult::U8(d)) => image::DynamicImage::ImageLumaA8(
                    image::ImageBuffer::from_raw(width, height, d)
                        .context("Invalid TIFF pixels")?,
                ),
                (ColorType::RGB(8), DecodingResult::U8(d)) => image::DynamicImage::ImageRgb8(
                    image::ImageBuffer::from_raw(width, height, d)
                        .context("Invalid TIFF pixels")?,
                ),
                (ColorType::RGBA(8), DecodingResult::U8(d)) => image::DynamicImage::ImageRgba8(
                    image::ImageBuffer::from_raw(width, height, d)
                        .context("Invalid TIFF pixels")?,
                ),
                (ColorType::Gray(16), DecodingResult::U16(d)) => image::DynamicImage::ImageLuma16(
                    image::ImageBuffer::from_raw(width, height, d)
                        .context("Invalid TIFF pixels")?,
                ),
                (ColorType::RGB(16), DecodingResult::U16(d)) => image::DynamicImage::ImageRgb16(
                    image::ImageBuffer::from_raw(width, height, d)
                        .context("Invalid TIFF pixels")?,
                ),
                (ColorType::RGBA(16), DecodingResult::U16(d)) => image::DynamicImage::ImageRgba16(
                    image::ImageBuffer::from_raw(width, height, d)
                        .context("Invalid TIFF pixels")?,
                ),
                (ColorType::CMYK(8), DecodingResult::U8(d)) => {
                    let rgb = d
                        .chunks_exact(4)
                        .flat_map(|c| {
                            (0..3).map(move |i| {
                                (((255 - u16::from(c[i])) * (255 - u16::from(c[3])) + 127) / 255)
                                    as u8
                            })
                        })
                        .collect();
                    image::DynamicImage::ImageRgb8(
                        image::ImageBuffer::from_raw(width, height, rgb)
                            .context("Invalid TIFF CMYK pixels")?,
                    )
                }
                (color, _) => bail!(
                    "Unsupported TIFF color encoding {color:?} on page {}; original retained",
                    ordinal + 1
                ),
            };
            image_page(library, job, document, ordinal, image)?;
        }
        ordinal += 1;
        if !decoder.more_images() {
            break;
        }
        if ordinal >= 100000 {
            bail!("TIFF exceeds page limit");
        }
        decoder.next_image()?;
    }
    Ok(())
}
