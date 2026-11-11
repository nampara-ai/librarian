//! Native filesystem boundary. Existing files are never replaced.
#![forbid(unsafe_code)]

pub mod assets;
pub mod catalog;
pub mod config;
pub mod credentials;
pub mod editorial;
pub mod export;
pub mod extract;
pub mod ocr;
pub mod parse;
pub mod pipeline;
pub mod profile;
pub mod provider;
pub mod store;

use librarian_app::{AppError, InputFormat, convert};
use librarian_core::{MAX_SOURCE_BYTES, RenderedDocument};
use std::{
    fs::File,
    io::{self, Read, Write},
    path::{Path, PathBuf},
};
use tempfile::NamedTempFile;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum NativeError {
    #[error("Unsupported input format; convert and inspect accept .txt, .md and .markdown")]
    UnsupportedFormat,
    #[error("Output document must have a .md extension")]
    OutputFormat,
    #[error("Input exceeds the {MAX_SOURCE_BYTES}-byte limit of convert and inspect")]
    TooLarge,
    #[error("Input is not UTF-8: {0}")]
    Encoding(#[from] std::string::FromUtf8Error),
    #[error(transparent)]
    Application(#[from] AppError),
    #[error(transparent)]
    Io(#[from] io::Error),
    #[error(
        "Markdown was published at {path}, but directory durability could not be confirmed: {error}"
    )]
    PublishedNotDurable { path: PathBuf, error: io::Error },
}

pub fn convert_file(path: &Path) -> Result<RenderedDocument, NativeError> {
    let extension = path
        .extension()
        .and_then(|s| s.to_str())
        .unwrap_or("")
        .to_ascii_lowercase();
    let format = match extension.as_str() {
        "txt" => InputFormat::Text,
        "md" | "markdown" => InputFormat::Markdown,
        _ => return Err(NativeError::UnsupportedFormat),
    };
    let file = File::open(path)?;
    if file.metadata()?.len() > MAX_SOURCE_BYTES as u64 {
        return Err(NativeError::TooLarge);
    }
    let mut bytes = Vec::new();
    file.take(MAX_SOURCE_BYTES as u64 + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() > MAX_SOURCE_BYTES {
        return Err(NativeError::TooLarge);
    }
    Ok(convert(format, String::from_utf8(bytes)?)?)
}

/// Stage in the destination directory, sync, and publish without replacing a file.
pub fn write_new_markdown(path: &Path, markdown: &str) -> Result<(), NativeError> {
    if !path
        .extension()
        .and_then(|s| s.to_str())
        .is_some_and(|s| s.eq_ignore_ascii_case("md"))
    {
        return Err(NativeError::OutputFormat);
    }
    write_new_file(path, markdown.as_bytes())
}

/// Durable no-clobber publication for any derived artifact.
pub fn write_new_file(path: &Path, bytes: &[u8]) -> Result<(), NativeError> {
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    let mut temporary = NamedTempFile::new_in(parent)?;
    temporary.write_all(bytes)?;
    temporary.as_file().sync_all()?;
    temporary.persist_noclobber(path).map_err(|e| e.error)?;
    #[cfg(unix)]
    File::open(parent)
        .and_then(|directory| directory.sync_all())
        .map_err(|error| NativeError::PublishedNotDurable {
            path: path.to_owned(),
            error,
        })?;
    Ok(())
}
