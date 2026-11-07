use crate::{
    config::Config,
    extract::{self, Observation},
    store::Library,
};
use anyhow::{Context, Result, bail};
use fontdue::{
    Font, FontSettings,
    layout::{CoordinateSystem, Layout, LayoutSettings, TextStyle},
};
use image::{DynamicImage, Rgb, RgbImage};
use rusqlite::{OptionalExtension, params};
use std::{
    cell::RefCell,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{
        Arc, OnceLock,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

thread_local! {static CANCELLATION:RefCell<Option<Arc<AtomicBool>>>=const {RefCell::new(None)};}
pub(super) struct Cancellation(Option<Arc<AtomicBool>>);
impl Cancellation {
    pub(super) fn enter(flag: Arc<AtomicBool>) -> Self {
        Self(CANCELLATION.with(|slot| slot.replace(Some(flag))))
    }
}
impl Drop for Cancellation {
    fn drop(&mut self) {
        CANCELLATION.with(|slot| slot.replace(self.0.take()));
    }
}
fn cancelled() -> bool {
    CANCELLATION.with(|slot| {
        slot.borrow()
            .as_ref()
            .is_some_and(|flag| flag.load(Ordering::Relaxed))
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    // Force the pause/resume race: the old producer wakes after the database
    // already says running again. Its scoped cancellation must still prevent
    // publishing stale pages, without poisoning a subsequent producer.
    #[test]
    fn cancelled_producer_cannot_publish_into_resumed_job() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("library");
        let source = temp.path().join("source.txt");
        std::fs::write(&source, "Reader continuity.").unwrap();
        let library = Library::open(&root).unwrap();
        let mut config = Config::default();
        config.processing.prepare_only = true;
        let job = crate::pipeline::queue(
            &library,
            &source,
            &temp.path().join("output"),
            &config,
            false,
            true,
        )
        .unwrap()[0];
        library
            .db
            .execute("UPDATE jobs SET status='running' WHERE id=?", [job])
            .unwrap();
        image::RgbImage::from_pixel(8, 8, image::Rgb([255, 255, 255]))
            .save(root.join("assets/page.png"))
            .unwrap();
        let cancel = Arc::new(AtomicBool::new(false));
        let flag = cancel.clone();
        let (ready_tx, ready_rx) = std::sync::mpsc::channel();
        let (release_tx, release_rx) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            let library = Library::open(&root).unwrap();
            let guard = Cancellation::enter(flag);
            ready_tx.send(()).unwrap();
            release_rx.recv().unwrap();
            assert!(insert(&library, job, 0, 0, "page.png", "stale", &config).is_err());
            drop(guard);
            insert(&library, job, 0, 0, "page.png", "fresh", &config).unwrap();
        });
        ready_rx.recv().unwrap();
        library
            .db
            .execute("UPDATE jobs SET status='paused' WHERE id=?", [job])
            .unwrap();
        cancel.store(true, Ordering::Relaxed);
        library
            .db
            .execute("UPDATE jobs SET status='running' WHERE id=?", [job])
            .unwrap();
        release_tx.send(()).unwrap();
        worker.join().unwrap();
        let pages: Vec<String> = library
            .db
            .prepare("SELECT native FROM editorial_pages WHERE job=?")
            .unwrap()
            .query_map([job], |row| row.get(0))
            .unwrap()
            .collect::<std::result::Result<_, _>>()
            .unwrap();
        assert_eq!(pages, ["fresh"]);
    }
}
pub(crate) fn checkpoint(library: &Library, job: i64) -> Result<()> {
    if cancelled() || !library.active(job)? {
        bail!("Page producer stopped; committed work retained");
    }
    Ok(())
}

pub fn work(library: &Library, job: i64) -> PathBuf {
    library
        .root
        .join(".librarian-work")
        .join(format!("job-{job}"))
}

pub fn office_executable(root: &Path, config: &Config) -> Option<PathBuf> {
    if let Some(path) = &config.processing.office_renderer {
        return path.is_file().then(|| path.clone());
    }
    let mut candidates = vec![
        root.join("engines/office/program/soffice"),
        root.join("engines/office/program/soffice.com"),
        root.join("engines/office/LibreOffice.app/Contents/MacOS/soffice"),
    ];
    if let Ok(exe) = std::env::current_exe()
        && let Some(parent) = exe.parent()
    {
        candidates.extend([
            parent.join("engines/office/program/soffice"),
            parent.join("engines/office/program/soffice.com"),
            parent.join("engines/office/LibreOffice.app/Contents/MacOS/soffice"),
        ]);
    }
    candidates.extend([
        PathBuf::from("/Applications/LibreOffice.app/Contents/MacOS/soffice"),
        PathBuf::from("/opt/homebrew/bin/soffice"),
        PathBuf::from("/usr/bin/libreoffice"),
    ]);
    for key in ["ProgramFiles", "ProgramFiles(x86)"] {
        if let Some(root) = std::env::var_os(key) {
            candidates.push(PathBuf::from(root).join("LibreOffice/program/soffice.com"));
        }
    }
    if let Some(path) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&path) {
            for name in ["soffice", "soffice.com", "soffice.exe", "libreoffice"] {
                candidates.push(dir.join(name));
            }
        }
    }
    candidates.into_iter().find(|p| p.is_file())
}

pub fn office_pdf(library: &Library, job: i64, source: &Path, config: &Config) -> Result<PathBuf> {
    checkpoint(library, job)?;
    library.progress(job, "laying out office pages", 0, 0)?;
    let folder = work(library, job).join("office");
    std::fs::create_dir_all(&folder)?;
    let target = folder
        .join(source.file_stem().context("Office source has no name")?)
        .with_extension("pdf");
    if target.is_file() {
        return Ok(target);
    }
    let executable=office_executable(&library.root,config).context("Office layout engine is unavailable. Use a full Librarian package, or set processing.office_renderer to the LibreOffice soffice executable.")?;
    let staging = tempfile::tempdir_in(&folder)?;
    let profile = staging.path().join("profile/user");
    std::fs::create_dir_all(&profile)?;
    // A private profile isolates conversion from running desktop Office sessions.
    // Disable document macros; source content must never execute in the renderer.
    std::fs::write(
        profile.join("registrymodifications.xcu"),
        r#"<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item><item oor:path="/org.openoffice.Office.Common/Load"><prop oor:name="UpdateDocMode" oor:op="fuse"><value>0</value></prop></item></oor:items>"#,
    )?;
    let uri = reqwest::Url::from_directory_path(profile.parent().unwrap())
        .map_err(|_| anyhow::anyhow!("Invalid renderer profile path"))?;
    let log = std::fs::File::create(folder.join("conversion.log"))?;
    let mut cmd = Command::new(&executable);
    cmd.env("SAL_USE_VCLPLUGIN", "svp");
    cmd.args(["--headless", "--nologo", "--nodefault", "--norestore"])
        .arg(format!("-env:UserInstallation={uri}"))
        .args(["--convert-to", "pdf", "--outdir"])
        .arg(staging.path())
        .arg(source)
        .stdin(Stdio::null())
        .stdout(log.try_clone()?)
        .stderr(log);
    #[cfg(target_os = "linux")]
    if let Some(program) = executable.parent() {
        let libs = program.parent().unwrap_or(program).join("lib");
        if libs.is_dir() {
            cmd.env("LD_LIBRARY_PATH", std::env::join_paths([program, &libs])?);
        }
    }
    let mut child = cmd.spawn().context("Cannot start Office layout engine")?;
    let started = Instant::now();
    loop {
        if let Some(status) = child.try_wait()? {
            if !status.success() {
                bail!(
                    "Office conversion failed ({status}): {}. Full log: {}",
                    conversion_error(&folder),
                    folder.join("conversion.log").display()
                );
            }
            break;
        }
        if cancelled() || !library.active(job)? || started.elapsed() > Duration::from_secs(600) {
            let _ = child.kill();
            let _ = child.wait();
            bail!("Office conversion stopped or exceeded its ten-minute deadline; source retained");
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    let rendered = staging.path().join(target.file_name().unwrap());
    if !rendered.is_file() {
        bail!(
            "Office produced no PDF: {}. Full log: {}",
            conversion_error(&folder),
            folder.join("conversion.log").display()
        );
    }
    std::fs::rename(rendered, &target)?;
    Ok(target)
}

fn conversion_error(folder: &Path) -> String {
    let log = std::fs::read_to_string(folder.join("conversion.log")).unwrap_or_default();
    librarian_core::editorial::tail(&log, 3000)
}

pub fn record(library: &Library, job: i64, ordinal: u64, observation: &Observation) -> Result<()> {
    checkpoint(library, job)?;
    let enabled = library
        .db
        .query_row(
            "SELECT 1 FROM editorial_runs WHERE job=?",
            [job],
            |_| Ok(()),
        )
        .optional()?
        .is_some();
    if !enabled
        || library
            .db
            .query_row(
                "SELECT 1 FROM editorial_observations WHERE job=? AND ordinal=?",
                params![job, ordinal as i64],
                |_| Ok(()),
            )
            .optional()?
            .is_some()
    {
        return Ok(());
    }
    let config = library.config(job)?;
    let full = observation.visuals.iter().find(|v| v.replace_page);
    if let Some(visual) = full {
        insert(
            library,
            job,
            ordinal,
            0,
            &visual.asset,
            &observation.raw_text,
            &config,
        )?;
    } else {
        let text = if matches!(
            observation.method.as_str(),
            "text" | "txt" | "log" | "markdown"
        ) || observation.blocks.is_empty()
        {
            observation.raw_text.clone()
        } else {
            observation
                .blocks
                .iter()
                .map(|s| {
                    librarian_core::corpus::render_content(&s.content).map_err(anyhow::Error::msg)
                })
                .collect::<Result<Vec<_>>>()?
                .join("\n\n")
        };
        let count = print(library, job, ordinal, &text, &config)?;
        for (i, visual) in observation.visuals.iter().enumerate() {
            insert(library, job, ordinal, count + i, &visual.asset, "", &config)?;
        }
    }
    library.db.execute(
        "INSERT OR IGNORE INTO editorial_observations(job,ordinal) VALUES(?,?)",
        params![job, ordinal as i64],
    )?;
    Ok(())
}

fn insert(
    library: &Library,
    job: i64,
    observation: u64,
    part: usize,
    asset: &str,
    native: &str,
    config: &Config,
) -> Result<()> {
    if library
        .db
        .query_row(
            "SELECT 1 FROM editorial_pages WHERE job=? AND observation=? AND part=?",
            params![job, observation as i64, part as i64],
            |_| Ok(()),
        )
        .optional()?
        .is_some()
    {
        return Ok(());
    }
    loop {
        checkpoint(library, job)?;
        let ahead: i64 = library.db.query_row(
            "SELECT count(*) FROM editorial_pages WHERE job=? AND reading IS NULL",
            [job],
            |r| r.get(0),
        )?;
        if config.processing.prepare_only
            || ahead < (config.processing.page_concurrency * 4).max(4) as i64
        {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    let page: i64 = library.db.query_row(
        "SELECT COALESCE(max(page),0)+1 FROM editorial_pages WHERE job=?",
        [job],
        |r| r.get(0),
    )?;
    let folder = work(library, job).join("pages");
    std::fs::create_dir_all(&folder)?;
    let target = folder.join(format!("{page:06}.png"));
    let source = library.root.join("assets").join(asset);
    if !target.exists() && std::fs::hard_link(&source, &target).is_err() {
        std::fs::copy(&source, &target)?;
    }
    library.db.execute("INSERT INTO editorial_pages(job,page,observation,part,image,native) SELECT ?,?,?,?,?,? WHERE EXISTS(SELECT 1 FROM jobs WHERE id=? AND status='running')",params![job,page,observation as i64,part as i64,target.to_string_lossy(),native,job])?;
    Ok(())
}

fn print(
    library: &Library,
    job: i64,
    observation: u64,
    text: &str,
    config: &Config,
) -> Result<usize> {
    let mut count = 0;
    // Markdown/HTML observations can be large; bound glyph-layout memory too.
    for (start, end) in librarian_core::editorial::ranges(text, 64 * 1024) {
        count += print_window(library, job, observation, &text[start..end], config, count)?;
    }
    Ok(count)
}

fn print_window(
    library: &Library,
    job: i64,
    observation: u64,
    text: &str,
    config: &Config,
    base: usize,
) -> Result<usize> {
    static FONT: OnceLock<Font> = OnceLock::new();
    let font = FONT.get_or_init(|| {
        Font::from_bytes(
            include_bytes!("../../resources/NotoSans-Regular.ttf") as &[u8],
            FontSettings::default(),
        )
        .expect("bundled Noto Sans is a valid font")
    });
    let width = config.processing.render_width;
    let height = width * 1414 / 1000;
    let margin = width / 16;
    let size = width as f32 / 65.0;
    let usable = (height - margin * 2) as f32;
    let mut layout = Layout::new(CoordinateSystem::PositiveYDown);
    layout.reset(&LayoutSettings {
        x: margin as f32,
        y: 0.0,
        max_width: Some((width - margin * 2) as f32),
        line_height: 1.25,
        ..Default::default()
    });
    layout.append(&[font], &TextStyle::new(text, size, 0));
    let glyphs = layout.glyphs();
    let mut first = 0;
    let mut source_start = 0;
    let mut part = 0;
    while first < glyphs.len() {
        let origin = glyphs[first].y;
        let mut end = first + 1;
        while end < glyphs.len() && glyphs[end].y + glyphs[end].height as f32 - origin < usable {
            end += 1;
        }
        // Keep all glyphs on a line together (descenders have different y values).
        if end < glyphs.len() {
            let y = glyphs[end].y;
            while end > first + 1 && (glyphs[end - 1].y - y).abs() < size {
                end -= 1;
            }
        }
        let source_end = if end == glyphs.len() {
            text.len()
        } else {
            glyphs[end].byte_offset
        };
        let mut image = RgbImage::from_pixel(width, height, Rgb([255, 255, 255]));
        for g in &glyphs[first..end] {
            // Layout retains control characters for byte ownership; they have no ink.
            if g.parent.is_control() || g.parent.is_whitespace() {
                continue;
            }
            let (metrics, bitmap) = font.rasterize_config(g.key);
            for y in 0..metrics.height {
                for x in 0..metrics.width {
                    let px = g.x as i32 + x as i32;
                    let py = (g.y - origin) as i32 + margin as i32 + y as i32;
                    if px >= 0 && py >= 0 && px < (width as i32) && py < (height as i32) {
                        let shade = 255 - bitmap[y * metrics.width + x];
                        image.put_pixel(px as u32, py as u32, Rgb([shade, shade, shade]));
                    }
                }
            }
        }
        let asset = extract::save_image(library, &DynamicImage::ImageRgb8(image))?;
        insert(
            library,
            job,
            observation,
            part,
            &asset,
            &text[source_start..source_end],
            config,
        )?;
        source_start = source_end;
        first = end;
        part += 1;
    }
    if part == 0 && !text.is_empty() {
        let image =
            DynamicImage::ImageRgb8(RgbImage::from_pixel(width, height, Rgb([255, 255, 255])));
        insert(
            library,
            job,
            observation,
            base,
            &extract::save_image(library, &image)?,
            text,
            config,
        )?;
        part = 1;
    }
    Ok(part)
}
