use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    fs::File,
    io::{Read, Write},
    path::{Path, PathBuf},
};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AssetSpec {
    pub name: String,
    pub url: String,
    pub sha256: String,
    pub archive: String,
    pub folder: String,
}

pub fn defaults() -> Result<Vec<AssetSpec>> {
    #[derive(Deserialize)]
    struct Manifest {
        platforms: std::collections::BTreeMap<String, Vec<AssetSpec>>,
        common: Vec<AssetSpec>,
    }
    let mut manifest: Manifest = serde_json::from_str(include_str!("engine-assets.json"))?;
    let platform = format!("{}-{}", std::env::consts::OS, std::env::consts::ARCH);
    let mut specs = manifest
        .platforms
        .remove(&platform)
        .context("Engine bundle unavailable for this OS/architecture")?;
    specs.extend(manifest.common);
    Ok(specs)
}

pub async fn install(root: &Path, progress: impl Fn(&str, u64, Option<u64>)) -> Result<()> {
    std::fs::create_dir_all(root)?;
    let http = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(600))
        .build()?;
    for spec in defaults()? {
        let destination = root.join(&spec.folder);
        if destination.join("manifest.json").exists() {
            progress(&format!("{} already installed", spec.name), 1, Some(1));
            continue;
        }
        let mut response = http.get(&spec.url).send().await?.error_for_status()?;
        let total = response.content_length();
        let mut received = 0u64;
        let mut archive = tempfile::NamedTempFile::new_in(root)?;
        let mut hash = Sha256::new();
        while let Some(chunk) = response.chunk().await? {
            received += chunk.len() as u64;
            if received > 512 * 1024 * 1024 {
                bail!("Engine archive exceeds 512 MiB limit");
            }
            hash.update(&chunk);
            archive.write_all(&chunk)?;
            progress(&spec.name, received, total);
        }
        let actual = format!("{:x}", hash.finalize());
        if actual != spec.sha256 {
            bail!(
                "Engine checksum mismatch for {}; no files installed",
                spec.name
            );
        }
        let staging = tempfile::tempdir_in(root)?;
        let mut files = Vec::new();
        if spec.archive == "zip" {
            let mut zip = zip::ZipArchive::new(File::open(archive.path())?)?;
            for i in 0..zip.len() {
                let mut entry = zip.by_index(i)?;
                if !entry.is_file() {
                    continue;
                }
                let Some(path) = entry.enclosed_name() else {
                    bail!("Unsafe archive entry");
                };
                copy_entry(&mut entry, &path, staging.path(), &mut files)?;
            }
        } else {
            let file = File::open(archive.path())?;
            let reader: Box<dyn Read> = if spec.archive == "tgz" {
                Box::new(flate2::read::GzDecoder::new(file))
            } else {
                Box::new(file)
            };
            let mut tar = tar::Archive::new(reader);
            for entry in tar.entries()? {
                let mut entry = entry?;
                if !entry.header().entry_type().is_file() {
                    continue;
                }
                let path = entry.path()?.into_owned();
                copy_entry(&mut entry, &path, staging.path(), &mut files)?;
            }
        }
        let manifest = serde_json::json!({"asset":spec,"files":files});
        std::fs::write(
            staging.path().join("manifest.json"),
            serde_json::to_vec_pretty(&manifest)?,
        )?;
        if destination.exists() {
            bail!(
                "Incomplete engine directory exists: {}; rename it before reinstalling",
                destination.display()
            );
        }
        std::fs::rename(staging.path(), destination)?;
    }
    Ok(())
}

fn copy_entry(
    input: &mut impl Read,
    path: &Path,
    root: &Path,
    files: &mut Vec<serde_json::Value>,
) -> Result<()> {
    if path.components().any(|c| {
        matches!(
            c,
            std::path::Component::ParentDir
                | std::path::Component::RootDir
                | std::path::Component::Prefix(_)
        )
    }) {
        bail!("Unsafe archive entry");
    }
    let target = root.join(path);
    if let Some(p) = target.parent() {
        std::fs::create_dir_all(p)?;
    }
    let mut out = File::create(&target)?;
    let mut hash = Sha256::new();
    let mut bytes = 0u64;
    let mut buffer = [0u8; 65536];
    loop {
        let n = input.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        bytes += n as u64;
        if bytes > 512 * 1024 * 1024 {
            bail!("Engine file exceeds size limit");
        }
        hash.update(&buffer[..n]);
        out.write_all(&buffer[..n])?;
    }
    out.sync_all()?;
    files.push(serde_json::json!({"path":path.to_string_lossy(),"sha256":format!("{:x}",hash.finalize()),"bytes":bytes}));
    Ok(())
}

pub fn locate(root: &Path, folder: &str, filename: &str) -> Result<PathBuf> {
    let managed = root.join(folder);
    let bundled = std::env::current_exe()?
        .parent()
        .context("Executable has no parent directory")?
        .join("engines")
        .join(folder);
    let base = if managed.join("manifest.json").is_file() {
        managed
    } else {
        bundled
    };
    let manifest: serde_json::Value =
        serde_json::from_slice(&std::fs::read(base.join("manifest.json")).with_context(|| {
            format!("{folder} engine not installed; run librarian engines install")
        })?)?;
    let expected = defaults()?
        .into_iter()
        .find(|spec| spec.folder == folder)
        .context("Unknown engine family")?;
    if manifest["asset"]["sha256"].as_str() != Some(expected.sha256.as_str()) {
        bail!(
            "Installed {folder} engine belongs to a different version; install this release's pinned engines"
        );
    }
    let files = manifest["files"]
        .as_array()
        .context("Invalid engine manifest")?;
    let entry = files
        .iter()
        .find(|entry| {
            Path::new(entry["path"].as_str().unwrap_or(""))
                .file_name()
                .is_some_and(|s| s == filename)
        })
        .with_context(|| format!("Engine file {filename} missing from manifest"))?;
    let relative = Path::new(entry["path"].as_str().context("Invalid engine file path")?);
    if relative.is_absolute()
        || relative
            .components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
    {
        bail!("Invalid engine file path");
    }
    let path = base.join(relative);
    let mut input = File::open(&path)?;
    let mut hash = Sha256::new();
    let mut buffer = [0u8; 65536];
    loop {
        let n = input.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
    }
    if format!("{:x}", hash.finalize()) != entry["sha256"].as_str().unwrap_or("") {
        bail!(
            "Installed engine file checksum mismatch: {}",
            path.display()
        );
    }
    Ok(path)
}

pub fn runtime_name() -> &'static str {
    if cfg!(target_os = "windows") {
        "onnxruntime.dll"
    } else if cfg!(target_os = "macos") {
        "libonnxruntime.1.22.0.dylib"
    } else {
        "libonnxruntime.so.1.22.0"
    }
}

/// Exercise real dynamic loading and model-session construction, without importing
/// documents, running inference, or making a provider request.
pub fn load_engines(root: &Path) -> Result<()> {
    use pdfium_render::prelude::Pdfium;
    let name = Pdfium::pdfium_platform_library_name();
    let library = locate(root, "pdfium", &name.to_string_lossy())?;
    let _pdfium = Pdfium::new(Pdfium::bind_to_library(library).context("Cannot load PDFium ABI")?);
    let _ocr = crate::ocr::Paddle::load(root)
        .context("Cannot initialize OCR runtime and model sessions")?;
    Ok(())
}
