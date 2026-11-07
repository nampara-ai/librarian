use crate::config::{Config, default_library};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::{
    io::Write,
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Location {
    pub root: PathBuf,
    pub config: PathBuf,
}
impl Location {
    pub fn for_root(root: PathBuf) -> Self {
        Self {
            config: root.join("config.toml"),
            root,
        }
    }
    pub fn active() -> Result<Self> {
        let path = default_library().join("active-profile.json");
        if !path.exists() {
            return Ok(Self::for_root(default_library()));
        }
        serde_json::from_slice(&std::fs::read(path)?).context("Cannot read active runtime profile")
    }
    pub fn activate(&self) -> Result<()> {
        write_json(&default_library().join("active-profile.json"), self)
    }
    pub fn ready(&self) -> Result<bool> {
        if !self.config.is_file() || !self.root.join("setup-complete.json").is_file() {
            return Ok(false);
        }
        let config = Config::load(&self.config)?;
        let stamp: serde_json::Value =
            serde_json::from_slice(&std::fs::read(self.root.join("setup-complete.json"))?)
                .context("Invalid setup completion record")?;
        if stamp["config_path"] != serde_json::to_value(&self.config)?
            || stamp["configuration"].as_str() != Some(verification_key(&config)?.as_str())
        {
            return Ok(false);
        }
        if config.processing.edit || config.processing.classify || config.processing.vision {
            if config.provider.model.is_empty() {
                return Ok(false);
            }
            if config
                .provider
                .credential
                .as_ref()
                .is_some_and(|r| r.session_only)
                && crate::credentials::resolve(&config.provider).is_err()
            {
                return Ok(false);
            }
        }
        Ok(true)
    }
}
pub fn write_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or(Path::new("."));
    std::fs::create_dir_all(parent)?;
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    serde_json::to_writer_pretty(&mut file, value)?;
    file.flush()?;
    file.as_file().sync_all()?;
    file.persist(path)?;
    Ok(())
}

/// Invalidate readiness when a connection or selected engine changes, without
/// forcing another paid check for presentation or budget edits.
pub fn verification_key(config: &Config) -> Result<String> {
    let p = &config.provider;
    Ok(blake3::hash(&serde_json::to_vec(&serde_json::json!({"kind":p.kind,"endpoint":p.base_url,"credential":p.credential,"model":p.model,"vision_model":p.vision_model,"format":p.structured_output,"ocr":config.processing.ocr,"ocr_language":config.processing.ocr_language}))?).to_hex().to_string())
}
