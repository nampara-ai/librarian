use anyhow::{Context, Result, bail};
use std::{
    io::{self, IsTerminal},
    path::PathBuf,
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
pub fn configure(
    location: librarian_native::profile::Location,
) -> Result<Option<librarian_native::profile::Location>> {
    let result =
        crate::ui::with_terminal(|terminal| crate::onboarding::run(terminal, location.clone()));
    if result
        .as_ref()
        .is_err_and(|e| e.is::<crate::ui::SaveExit>())
    {
        librarian_native::store::Library::open(&location.root)?.control(None, "pause")?;
    }
    result
}

pub fn paths(
    source: Option<PathBuf>,
    destination: Option<PathBuf>,
    create: bool,
) -> Result<(PathBuf, PathBuf, bool)> {
    if let (Some(source), Some(destination)) = (&source, &destination)
        && (destination.exists() || create || !io::stdin().is_terminal())
    {
        source
            .metadata()
            .with_context(|| format!("Cannot access {}", source.display()))?;
        return Ok((source.clone(), destination.clone(), create));
    }
    crate::ui::with_terminal(|t| {
        let start = std::env::current_dir()?;
        let source = match source {
            Some(path) => path,
            None => crate::ui::browse(t, &start, false)?.context("Import cancelled")?,
        };
        let destination = match destination {
            Some(path) => path,
            None => crate::ui::browse(t, &start, true)?.context("Import cancelled")?,
        };
        if source.as_os_str().is_empty() || destination.as_os_str().is_empty() {
            bail!("Choose both source and destination");
        }
        let create = if !destination.exists() && !create {
            crate::ui::choose(
                t,
                "Create destination",
                &format!("Create {} before processing?", destination.display()),
                &["Create folder".into(), "Cancel import".into()],
                0,
            )? == Some(0)
        } else {
            create
        };
        if !destination.exists() && !create {
            bail!("Import cancelled; destination was not created");
        }
        source
            .metadata()
            .with_context(|| format!("Cannot access {}", source.display()))?;
        Ok((source, destination, create))
    })
}
