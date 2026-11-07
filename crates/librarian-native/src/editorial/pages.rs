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
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::OnceLock,
    time::{Duration, Instant},
};

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
            cmd.env("LD_LIBRARY_PATH", libs);
        }
    }
    let mut child = cmd.spawn().context("Cannot start Office layout engine")?;
    let started = Instant::now();
    loop {
        if let Some(status) = child.try_wait()? {
            if !status.success() {
                bail!(
                    "Office conversion failed; see {}",
                    folder.join("conversion.log").display()
                );
            }
            break;
        }
        if !library.active(job)? || started.elapsed() > Duration::from_secs(600) {
            let _ = child.kill();
            let _ = child.wait();
            bail!("Office conversion stopped or exceeded its ten-minute deadline; source retained");
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    let rendered = staging.path().join(target.file_name().unwrap());
    if !rendered.is_file() {
        bail!(
            "Office produced no PDF; see {}",
            folder.join("conversion.log").display()
        );
    }
    std::fs::rename(rendered, &target)?;
    Ok(target)
}

pub fn record(library: &Library, job: i64, ordinal: u64, observation: &Observation) -> Result<()> {
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
        if !library.active(job)? {
            bail!("Job paused or cancelled");
        }
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
