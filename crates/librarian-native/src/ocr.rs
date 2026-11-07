use anyhow::{Context, Result, bail};
use image::{DynamicImage, RgbImage, imageops::FilterType};
use ort::{environment::Environment, session::Session, value::Tensor};
use serde::{Deserialize, Serialize};
use std::{collections::VecDeque, path::Path};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OcrLine {
    pub text: String,
    pub score: f32,
    pub polygon: [[f32; 2]; 4],
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OcrResult {
    pub lines: Vec<OcrLine>,
    pub findings: Vec<String>,
    pub engine: String,
}

pub struct Paddle {
    _environment: Environment,
    det: Session,
    rec: Session,
    alphabet: Vec<String>,
    height: usize,
    width: usize,
}
impl Paddle {
    pub fn load(engines: &Path) -> Result<Self> {
        let runtime = crate::assets::locate(engines, "onnxruntime", crate::assets::runtime_name())?;
        let det_path = crate::assets::locate(engines, "det", "inference.onnx")?;
        let rec_path = crate::assets::locate(engines, "rec", "inference.onnx")?;
        let config_path = crate::assets::locate(engines, "rec", "inference.yml")?;
        let config: serde_yaml::Value =
            serde_yaml::from_str(&std::fs::read_to_string(config_path)?)?;
        let alphabet = config["PostProcess"]["character_dict"]
            .as_sequence()
            .context("Recognition model has no character dictionary")?
            .iter()
            .map(|v| {
                v.as_str()
                    .map(str::to_owned)
                    .context("Invalid OCR dictionary entry")
            })
            .collect::<Result<Vec<_>>>()?;
        let shape = config["PreProcess"]["transform_ops"]
            .as_sequence()
            .context("Missing OCR preprocessing")?
            .iter()
            .find_map(|v| v["RecResizeImg"]["image_shape"].as_sequence())
            .context("Missing recognition image shape")?;
        let height = shape
            .get(1)
            .and_then(|n| n.as_u64())
            .context("Missing recognition height")? as usize;
        let width = shape
            .get(2)
            .and_then(|n| n.as_u64())
            .context("Missing recognition width")? as usize;
        if height > 256 || width > 3200 {
            bail!("Unsupported recognition tensor dimensions");
        }
        let environment = ort::init_from(runtime)?.with_telemetry(false).build()?;
        let threads = std::thread::available_parallelism()
            .map(usize::from)
            .unwrap_or(2)
            .min(4);
        let det = Session::builder(&environment)?
            .with_intra_threads(threads)
            .map_err(|e| anyhow::anyhow!(e.to_string()))?
            .commit_from_file(det_path)?;
        let rec = Session::builder(&environment)?
            .with_intra_threads(threads)
            .map_err(|e| anyhow::anyhow!(e.to_string()))?
            .commit_from_file(rec_path)?;
        let mut alphabet = alphabet;
        alphabet.push(" ".into());
        Ok(Self {
            _environment: environment,
            det,
            rec,
            alphabet,
            height,
            width,
        })
    }

    pub fn recognize(
        &mut self,
        image: &DynamicImage,
        mut checkpoint: impl FnMut() -> Result<()>,
    ) -> Result<OcrResult> {
        checkpoint()?;
        let rgb = image.to_rgb8();
        let scale = (960.0 / rgb.width().max(rgb.height()) as f32).min(1.0);
        let w = ((rgb.width() as f32 * scale / 32.0).round() as u32 * 32).max(32);
        let h = ((rgb.height() as f32 * scale / 32.0).round() as u32 * 32).max(32);
        let resized = image::imageops::resize(&rgb, w, h, FilterType::Triangle);
        let tensor = Tensor::from_array((
            [1usize, 3, h as usize, w as usize],
            normalize(
                &resized,
                [0.485, 0.456, 0.406],
                [0.229, 0.224, 0.225],
                w as usize,
            ),
        ))?;
        let output = self.det.run(ort::inputs![tensor])?;
        let (shape, scores) = output[0].try_extract_tensor::<f32>()?;
        if shape.len() < 2 {
            bail!("Unexpected OCR detection output rank");
        }
        let out_h = shape[shape.len() - 2] as usize;
        let out_w = shape[shape.len() - 1] as usize;
        if out_h == 0 || out_w == 0 || out_h * out_w > 4_000_000 || scores.len() != out_h * out_w {
            bail!("Unsupported OCR detection output shape");
        }
        let mut boxes = components(scores, out_w, out_h);
        boxes.sort_by(|a, b| {
            a[0][1]
                .total_cmp(&b[0][1])
                .then_with(|| a[0][0].total_cmp(&b[0][0]))
        });
        let mut lines = Vec::new();
        let mut findings = Vec::new();
        for mut polygon in boxes {
            checkpoint()?;
            for p in &mut polygon {
                p[0] *= rgb.width() as f32 / out_w as f32;
                p[1] *= rgb.height() as f32 / out_h as f32;
            }
            let crop = crop_quad(&rgb, polygon);
            if crop.width() < 2 || crop.height() < 2 {
                continue;
            }
            let ratio = crop.width() as f32 / crop.height() as f32;
            let resized_w = (self.height as f32 * ratio).ceil().max(1.0) as usize;
            if resized_w > 3200 {
                findings.push("OCR line exceeds recognition width; needs visual review".into());
                continue;
            }
            let rec_w = resized_w.max(self.width).min(3200);
            let resized = image::imageops::resize(
                &crop,
                resized_w as u32,
                self.height as u32,
                FilterType::Triangle,
            );
            let input = Tensor::from_array((
                [1usize, 3, self.height, rec_w],
                normalize(&resized, [0.5; 3], [0.5; 3], rec_w),
            ))?;
            let result = self.rec.run(ort::inputs![input])?;
            let (shape, values) = result[0].try_extract_tensor::<f32>()?;
            if shape.len() != 3 || shape[0] != 1 {
                bail!("Unsupported OCR recognition output shape");
            }
            let steps = shape[1] as usize;
            let classes = shape[2] as usize;
            if classes != self.alphabet.len() + 1 {
                bail!(
                    "OCR model/dictionary mismatch: {classes} classes, {} dictionary entries",
                    self.alphabet.len()
                );
            }
            let mut previous = 0;
            let mut text = String::new();
            let mut confidence = 0f32;
            let mut count = 0;
            for step in 0..steps {
                let row = &values[step * classes..(step + 1) * classes];
                let (index, score) = row
                    .iter()
                    .enumerate()
                    .max_by(|(_, a), (_, b)| a.total_cmp(b))
                    .context("Empty OCR result")?;
                if index != 0 && index != previous {
                    text.push_str(&self.alphabet[index - 1]);
                    confidence += *score;
                    count += 1;
                }
                previous = index;
            }
            let score = confidence / (count as f32).max(1.0);
            if text.trim().is_empty() {
                findings.push("Detected text region was not recognized".into());
                continue;
            }
            if score < 0.8 {
                findings.push("Low OCR recognition score; needs review".into());
            }
            lines.push(OcrLine {
                text,
                score,
                polygon,
            });
        }
        if lines.is_empty() {
            findings.push(
                "No text recognized; image may contain non-text content or require vision".into(),
            );
        }
        findings.sort();
        findings.dedup();
        Ok(OcrResult {
            lines,
            findings,
            engine: "PP-OCRv5_mobile/onnxruntime-1.22.0/cpu".into(),
        })
    }
}

fn normalize(image: &RgbImage, mean: [f32; 3], std: [f32; 3], width: usize) -> Vec<f32> {
    let h = image.height() as usize;
    let mut values = vec![0f32; 3 * h * width];
    for (x, y, p) in image.enumerate_pixels() {
        for c in 0..3 {
            values[c * h * width + y as usize * width + x as usize] =
                (f32::from(p[2 - c]) / 255.0 - mean[c]) / std[c];
        }
    }
    values
}

fn components(scores: &[f32], w: usize, h: usize) -> Vec<[[f32; 2]; 4]> {
    let mut seen = vec![false; scores.len()];
    let mut result = Vec::new();
    for start in 0..scores.len() {
        if seen[start] || scores[start] < 0.3 {
            continue;
        }
        let mut queue = VecDeque::from([start]);
        seen[start] = true;
        let mut points = Vec::new();
        let mut score = 0.0;
        while let Some(i) = queue.pop_front() {
            let x = i % w;
            let y = i / w;
            points.push((x as f32, y as f32));
            score += scores[i];
            for (nx, ny) in [
                (x.wrapping_sub(1), y),
                (x + 1, y),
                (x, y.wrapping_sub(1)),
                (x, y + 1),
            ] {
                if nx < w && ny < h {
                    let n = ny * w + nx;
                    if !seen[n] && scores[n] >= 0.3 {
                        seen[n] = true;
                        queue.push_back(n);
                    }
                }
            }
        }
        if points.len() < 8 || score / (points.len() as f32) < 0.6 {
            continue;
        }
        let n = points.len() as f32;
        let mx = points.iter().map(|p| p.0).sum::<f32>() / n;
        let my = points.iter().map(|p| p.1).sum::<f32>() / n;
        let xx = points.iter().map(|p| (p.0 - mx).powi(2)).sum::<f32>();
        let yy = points.iter().map(|p| (p.1 - my).powi(2)).sum::<f32>();
        let xy = points.iter().map(|p| (p.0 - mx) * (p.1 - my)).sum::<f32>();
        let theta = 0.5 * (2.0 * xy).atan2(xx - yy);
        let (sin, cos) = theta.sin_cos();
        let mut minx = f32::INFINITY;
        let mut maxx = f32::NEG_INFINITY;
        let mut miny = f32::INFINITY;
        let mut maxy = f32::NEG_INFINITY;
        for (x, y) in points {
            let rx = (x - mx) * cos + (y - my) * sin;
            let ry = -(x - mx) * sin + (y - my) * cos;
            minx = minx.min(rx);
            maxx = maxx.max(rx);
            miny = miny.min(ry);
            maxy = maxy.max(ry);
        }
        let width = (maxx - minx).max(1.0);
        let height = (maxy - miny).max(1.0);
        let expansion = width * height / (width + height);
        minx -= expansion;
        maxx += expansion;
        miny -= expansion;
        maxy += expansion;
        let mut polygon = [[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy]];
        for p in &mut polygon {
            let x = p[0];
            let y = p[1];
            p[0] = (mx + x * cos - y * sin).clamp(0.0, w as f32 - 1.0);
            p[1] = (my + x * sin + y * cos).clamp(0.0, h as f32 - 1.0);
        }
        result.push(polygon);
        if result.len() >= 1000 {
            break;
        }
    }
    result
}

fn crop_quad(image: &RgbImage, p: [[f32; 2]; 4]) -> RgbImage {
    let distance =
        |a: [f32; 2], b: [f32; 2]| ((a[0] - b[0]).powi(2) + (a[1] - b[1]).powi(2)).sqrt();
    let w = distance(p[0], p[1])
        .max(distance(p[3], p[2]))
        .ceil()
        .clamp(1.0, 16000.0) as u32;
    let h = distance(p[0], p[3])
        .max(distance(p[1], p[2]))
        .ceil()
        .clamp(1.0, 16000.0) as u32;
    let mut result = RgbImage::new(w, h);
    for (x, y, pixel) in result.enumerate_pixels_mut() {
        let u = x as f32 / (w - 1).max(1) as f32;
        let v = y as f32 / (h - 1).max(1) as f32;
        let sx = (p[0][0] * (1.0 - u) * (1.0 - v)
            + p[1][0] * u * (1.0 - v)
            + p[2][0] * u * v
            + p[3][0] * (1.0 - u) * v)
            .clamp(0.0, image.width() as f32 - 1.0);
        let sy = (p[0][1] * (1.0 - u) * (1.0 - v)
            + p[1][1] * u * (1.0 - v)
            + p[2][1] * u * v
            + p[3][1] * (1.0 - u) * v)
            .clamp(0.0, image.height() as f32 - 1.0);
        let x0 = sx.floor() as u32;
        let y0 = sy.floor() as u32;
        let x1 = (x0 + 1).min(image.width() - 1);
        let y1 = (y0 + 1).min(image.height() - 1);
        let dx = sx - x0 as f32;
        let dy = sy - y0 as f32;
        for c in 0..3 {
            pixel[c] = (f32::from(image[(x0, y0)][c]) * (1.0 - dx) * (1.0 - dy)
                + f32::from(image[(x1, y0)][c]) * dx * (1.0 - dy)
                + f32::from(image[(x0, y1)][c]) * (1.0 - dx) * dy
                + f32::from(image[(x1, y1)][c]) * dx * dy)
                .round() as u8;
        }
    }
    if h > w * 2 {
        image::imageops::rotate90(&result)
    } else {
        result
    }
}

pub async fn tesseract(image: &Path, language: &str) -> Result<OcrResult> {
    let mut command = tokio::process::Command::new("tesseract");
    command
        .arg(image)
        .args(["stdout", "-l", language, "tsv"])
        .kill_on_drop(true);
    let output = tokio::time::timeout(std::time::Duration::from_secs(120), command.output())
        .await
        .context("Tesseract timed out")??;
    if !output.status.success() {
        bail!("Tesseract failed; check installation and language data");
    }
    let text = String::from_utf8(output.stdout)?;
    let mut grouped: std::collections::BTreeMap<[u32; 4], OcrLine> =
        std::collections::BTreeMap::new();
    for row in text.lines().skip(1) {
        let c: Vec<_> = row.splitn(12, '\t').collect();
        if c.len() != 12 || c[0] != "5" || c[11].trim().is_empty() {
            continue;
        }
        let key = [c[1].parse()?, c[2].parse()?, c[3].parse()?, c[4].parse()?];
        let x = c[6].parse::<f32>()?;
        let y = c[7].parse::<f32>()?;
        let w = c[8].parse::<f32>()?;
        let h = c[9].parse::<f32>()?;
        let score = c[10].parse::<f32>()? / 100.0;
        let line = grouped.entry(key).or_insert(OcrLine {
            text: String::new(),
            score,
            polygon: [[x, y], [x + w, y], [x + w, y + h], [x, y + h]],
        });
        if !line.text.is_empty() {
            line.text.push(' ');
        }
        line.text.push_str(c[11]);
        line.score = line.score.min(score);
        line.polygon[1][0] = x + w;
        line.polygon[2][0] = x + w;
        line.polygon[2][1] = (y + h).max(line.polygon[2][1]);
        line.polygon[3][1] = line.polygon[2][1];
    }
    let lines: Vec<_> = grouped.into_values().collect();
    let findings = if lines.is_empty() || lines.iter().any(|l| l.score < 0.8) {
        vec!["OCR contains uncertain or missing text".into()]
    } else {
        vec![]
    };
    Ok(OcrResult {
        lines,
        findings,
        engine: "tesseract-external".into(),
    })
}
