use anyhow::{Context, Result, bail};
use librarian_native::config::Config;
use std::{
    io::{self, IsTerminal, Write},
    path::{Path, PathBuf},
};

pub fn expand_path(value: &str) -> PathBuf {
    let value = value.trim().trim_matches(['\'', '"']);
    if let Some(rest) = value.strip_prefix("~/")
        && let Some(home_dir) = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"))
    {
        return PathBuf::from(home_dir).join(rest);
    }
    PathBuf::from(value)
}
fn prompt(label: &str, default: &str) -> Result<String> {
    if !io::stdin().is_terminal() {
        bail!("Interactive setup requires a terminal; use explicit CLI options or config set");
    }
    eprint!("{label} [{}]: ", default);
    io::stderr().flush()?;
    let mut value = String::new();
    if io::stdin().read_line(&mut value)? == 0 {
        bail!("Input closed");
    }
    let value = value.trim();
    Ok(if value.is_empty() {
        default.into()
    } else {
        value.into()
    })
}
fn toggle(label: &str, current: bool) -> Result<bool> {
    let answer = prompt(label, if current { "yes" } else { "no" })?;
    match answer.to_ascii_lowercase().as_str() {
        "y" | "yes" | "true" => Ok(true),
        "n" | "no" | "false" => Ok(false),
        _ => bail!("Expected yes or no"),
    }
}

pub fn configure(path: &Path) -> Result<()> {
    let mut config = Config::load(path)?;
    eprintln!("\n  LIBRARIAN\n  Set up your document pipeline\n");
    let prior = config.provider.kind.clone();
    config.provider.kind = prompt("Provider: responses / chat / anthropic", &prior)?;
    if config.provider.kind != prior {
        match config.provider.kind.as_str() {
            "anthropic" => {
                config.provider.base_url = "https://api.anthropic.com/v1".into();
                config.provider.key_env = "ANTHROPIC_API_KEY".into();
                config.provider.prompt_cache = "ephemeral".into();
            }
            "chat" => {
                config.provider.base_url = "http://localhost:11434/v1".into();
                config.provider.prompt_cache = "off".into();
            }
            _ => {
                config.provider.base_url = "https://api.openai.com/v1".into();
                config.provider.key_env = "OPENAI_API_KEY".into();
                config.provider.prompt_cache = "implicit".into();
            }
        }
    }
    config.provider.base_url = prompt("API base URL", &config.provider.base_url)?;
    config.provider.model = prompt(
        "Text model (may be configured later)",
        &config.provider.model,
    )?;
    config.provider.vision_model = prompt(
        "Vision model (blank uses text model)",
        &config.provider.vision_model,
    )?;
    config.provider.key_env = prompt(
        "Environment variable containing the API key",
        &config.provider.key_env,
    )?;
    eprintln!("  Credentials stay in your environment; they are not written to this file.");
    config.processing.edit = toggle("Copyedit prose", config.processing.edit)?;
    config.processing.classify = toggle(
        "Generate title, synopsis, tags and classification",
        config.processing.classify,
    )?;
    config.processing.vision = toggle(
        "Use vision for figures and complex pages",
        config.processing.vision,
    )?;
    config.processing.ocr = prompt(
        "OCR: paddle / tesseract / vision / off",
        &config.processing.ocr,
    )?;
    config.processing.ocr_language = prompt("OCR language", &config.processing.ocr_language)?;
    config.output.frontmatter = toggle("Include metadata frontmatter", config.output.frontmatter)?;
    config.output.title = toggle("Include document title", config.output.title)?;
    config.output.summary = toggle("Include synopsis", config.output.summary)?;
    config.output.page_markers = toggle("Include source page markers", config.output.page_markers)?;
    config.output.review_notes = toggle(
        "Include unresolved-content notices",
        config.output.review_notes,
    )?;
    config.output.filename = prompt(
        "Filename template ({title}, {class}, {number}, {id})",
        &config.output.filename,
    )?;
    config.save(path)?;
    eprintln!(
        "\n✓ Saved {}\n  Advanced options: librarian config edit\n  Metadata schema: librarian config show\n  Engine installation: librarian engines install\n  Start: librarian clean or librarian tui\n",
        path.display()
    );
    Ok(())
}

pub fn paths(
    source: Option<PathBuf>,
    destination: Option<PathBuf>,
    create: bool,
) -> Result<(PathBuf, PathBuf, bool)> {
    let source = match source {
        Some(path) => path,
        None => expand_path(&prompt("Source file or folder", "")?),
    };
    if source.as_os_str().is_empty() {
        bail!("Choose a source file or folder");
    }
    let destination = match destination {
        Some(path) => path,
        None => expand_path(&prompt("Destination folder", "")?),
    };
    if destination.as_os_str().is_empty() {
        bail!("Choose a destination folder");
    }
    let create = if !destination.exists() && !create && io::stdin().is_terminal() {
        toggle(&format!("Create {}", destination.display()), false)?
    } else {
        create
    };
    source
        .metadata()
        .with_context(|| format!("Cannot access {}", source.display()))?;
    Ok((source, destination, create))
}
