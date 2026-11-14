//! The active workspace, what has been verified for it, and the transaction
//! that activates a changed configuration.
use crate::{
    config::{Config, default_library},
    credentials::{self, Vault},
};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::{
    io::Write,
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
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
    /// A workspace is established once its configuration file exists. Nothing
    /// else is needed to open Home and to parse.
    pub fn established(&self) -> bool {
        self.config.is_file()
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

/// The fingerprint written by releases that kept one stamp for everything.
/// Read only to carry an earlier verification forward.
fn legacy_key(config: &Config) -> Result<String> {
    let p = &config.provider;
    Ok(blake3::hash(&serde_json::to_vec(&serde_json::json!({"kind":p.kind,"endpoint":p.base_url,"credential":p.credential,"model":p.model,"vision_model":p.vision_model,"format":p.structured_output,"ocr":config.processing.ocr,"ocr_language":config.processing.ocr_language}))?).to_hex().to_string())
}

/// Identifies what a connection check establishes: this protocol, at this
/// endpoint, with this key, reaches this model and gets a structured reply.
/// Output preferences, processing defaults, ceilings and prices are not part
/// of it, so changing them does not call for another billed request. A newly
/// entered key always has a new account name and therefore a new fingerprint.
pub fn provider_fingerprint(config: &Config) -> String {
    let p = &config.provider;
    let value = serde_json::json!({
        "check": "provider-v1",
        "kind": p.kind,
        "endpoint": p.base_url.trim_end_matches('/'),
        "account": p.credential.as_ref().map(|c| c.account.as_str()),
        "key_env": if p.credential.is_none() { Some(p.key_env.as_str()) } else { None },
        "model": p.model,
        "vision_model": p.vision_model,
        "structured_output": p.structured_output,
    });
    blake3::hash(value.to_string().as_bytes())
        .to_hex()
        .to_string()
}

/// One successful connection check.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct ProviderCheck {
    pub fingerprint: String,
    pub verified_at: i64,
    pub model: String,
    #[serde(default)]
    pub input_tokens: i64,
    #[serde(default)]
    pub output_tokens: i64,
    #[serde(default)]
    pub estimated_cost: Option<f64>,
}

/// What has been verified for a workspace: provider access. Whether the local
/// engines are installed is read from the engine files each time it is shown,
/// and nothing here says anything about the quality of a processed document.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Verification {
    #[serde(default)]
    pub version: u32,
    #[serde(default)]
    pub provider: Option<ProviderCheck>,
}

impl Verification {
    fn path(root: &Path) -> PathBuf {
        root.join("verification.json")
    }
    pub fn load(location: &Location, config: &Config) -> Self {
        if let Ok(bytes) = std::fs::read(Self::path(&location.root)) {
            return serde_json::from_slice(&bytes).unwrap_or_default();
        }
        // A workspace set up by an earlier release carries one stamp. It is
        // honoured while the configuration it covered is unchanged.
        let Ok(bytes) = std::fs::read(location.root.join("setup-complete.json")) else {
            return Self::default();
        };
        let Ok(stamp) = serde_json::from_slice::<serde_json::Value>(&bytes) else {
            return Self::default();
        };
        let unchanged = legacy_key(config)
            .ok()
            .is_some_and(|key| stamp["configuration"].as_str() == Some(key.as_str()));
        if !unchanged {
            return Self::default();
        }
        let at = stamp["verified_at"].as_i64().unwrap_or(0);
        Self {
            version: 2,
            provider: Some(ProviderCheck {
                fingerprint: provider_fingerprint(config),
                verified_at: at,
                model: config.provider.model.clone(),
                input_tokens: 0,
                output_tokens: 0,
                estimated_cost: None,
            }),
        }
    }
    /// Write the record of a workspace that has only the stamp of an earlier
    /// release, so that later changes are judged by the present fingerprint.
    pub fn carry_forward(location: &Location, config: &Config) {
        if !Self::path(&location.root).exists() && location.root.is_dir() {
            let record = Self::load(location, config);
            if record.provider.is_some() {
                let _ = record.save(&location.root);
            }
        }
    }
    pub fn save(&self, root: &Path) -> Result<()> {
        write_json(
            &Self::path(root),
            &Self {
                version: 2,
                ..self.clone()
            },
        )
    }
    /// The recorded check, if it covers this configuration.
    pub fn provider_for(&self, config: &Config) -> Option<&ProviderCheck> {
        self.provider
            .as_ref()
            .filter(|check| check.fingerprint == provider_fingerprint(config))
    }
}

/// Whether work that calls a model can start.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Ai {
    /// Editing, classification and visual reading are all off.
    NotNeeded,
    /// No provider, key or model has been chosen.
    NotConnected,
    /// The key was kept for one session and is not in this process.
    KeyMissing,
    /// A provider is configured but this combination has not passed a check.
    Unverified,
    Ready,
}

pub fn ai_state(location: &Location, config: &Config) -> Ai {
    let p = &config.processing;
    if !(p.edit || p.classify || p.vision) {
        return Ai::NotNeeded;
    }
    let provider = &config.provider;
    if provider.model.is_empty() {
        return Ai::NotConnected;
    }
    match &provider.credential {
        Some(reference) if reference.session_only => {
            if credentials::resolve(provider).is_err() {
                return Ai::KeyMissing;
            }
        }
        Some(_) => {}
        None => {
            let from_environment = !provider.key_env.is_empty()
                && std::env::var_os(&provider.key_env).is_some_and(|v| !v.is_empty());
            if !from_environment && !crate::catalog::local(config) {
                return Ai::NotConnected;
            }
        }
    }
    if Verification::load(location, config)
        .provider_for(config)
        .is_some()
    {
        Ai::Ready
    } else {
        Ai::Unverified
    }
}

/// The configuration, and the key if one was stored, are in force, but a
/// record that follows them could not be written.
#[derive(Debug)]
pub struct SavedIncomplete(pub String);
impl std::fmt::Display for SavedIncomplete {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for SavedIncomplete {}

#[derive(Serialize, Deserialize)]
struct Journal {
    account: String,
    endpoint: String,
}

fn journal(root: &Path) -> PathBuf {
    root.join("pending-credential.json")
}

/// Finish or undo an activation that was interrupted. The journal names a key
/// that an activation was about to store. If the configuration refers to it,
/// the activation completed and only the journal is left over. If not, the key
/// was stored for a configuration that never took effect, and it is removed.
/// Returns whether such a key was removed.
pub fn recover(vault: &dyn Vault, location: &Location) -> Result<bool> {
    let path = journal(&location.root);
    let Ok(bytes) = std::fs::read(&path) else {
        return Ok(false);
    };
    // With the store out of reach nothing can be settled now. The journal
    // stays, and the next start tries again.
    if vault.available().is_err() {
        return Ok(false);
    }
    let Ok(pending) = serde_json::from_slice::<Journal>(&bytes) else {
        std::fs::remove_file(&path)?;
        return Ok(false);
    };
    let referenced = Config::load(&location.config).ok().is_some_and(|config| {
        config
            .provider
            .credential
            .is_some_and(|c| c.account == pending.account && !c.session_only)
    });
    if !referenced {
        // Leave the journal in place if the store cannot be reached now, so
        // that the removal is attempted again later.
        vault.delete(&pending.account)?;
    }
    std::fs::remove_file(&path)?;
    Ok(!referenced)
}

/// Make a prepared configuration the active one, as one transaction.
///
/// Until this is called nothing outside the memory of the process has changed:
/// a newly entered key is held under a session reference. Here the key is
/// stored if the owner chose to remember it, the configuration is written,
/// and the verification record is written. The caller then records the
/// workspace as the active one with `Location::activate`.
///
/// If the configuration cannot be written, a key stored a moment earlier is
/// removed again and the previous configuration stays in force. If the process
/// stops between storing the key and writing the configuration, the journal
/// lets `recover` remove the key at the next start. Keys that existed before
/// are never touched: every entered key has a new account name.
///
/// Once the configuration is written the key and the configuration are in
/// force. A failure after that point is returned as `SavedIncomplete`, so
/// that the caller does not report that nothing changed.
pub fn activate(
    vault: &dyn Vault,
    location: &Location,
    config: &Config,
    remember: bool,
    verification: &Verification,
) -> Result<Config> {
    // An earlier interruption is settled first if it can be. That it cannot
    // be settled now is no reason to refuse this activation.
    let _ = recover(vault, location);
    let mut config = config.clone();
    config.validate()?;
    std::fs::create_dir_all(&location.root)?;
    let mut stored: Option<String> = None;
    if remember
        && let Some(reference) = config.provider.credential.clone()
        && reference.session_only
    {
        // Asked before anything is written, so that a store that is absent
        // leaves no journal behind.
        vault.available()?;
        write_json(
            &journal(&location.root),
            &Journal {
                account: reference.account.clone(),
                endpoint: reference.endpoint.clone(),
            },
        )?;
        match credentials::persist(vault, &reference) {
            Ok(persistent) => {
                stored = Some(persistent.account.clone());
                config.provider.credential = Some(persistent);
            }
            Err(error) => {
                // The write may have happened before the read-back failed.
                if vault.delete(&reference.account).is_ok() {
                    let _ = std::fs::remove_file(journal(&location.root));
                }
                return Err(error);
            }
        }
    }
    if let Err(error) = config.save(&location.config) {
        if let Some(account) = &stored
            && vault.delete(account).is_ok()
        {
            let _ = std::fs::remove_file(journal(&location.root));
        }
        return Err(error).context("The configuration could not be written; nothing was changed");
    }
    // The configuration now refers to the key: the transaction is committed.
    let _ = std::fs::remove_file(journal(&location.root));
    verification.save(&location.root).map_err(|error| {
        anyhow::Error::new(SavedIncomplete(format!(
            "the verification record could not be written: {error:#}"
        )))
    })?;
    Ok(config)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{collections::HashMap, sync::Mutex};
    use zeroize::Zeroizing;

    #[derive(Default)]
    struct Table(Mutex<HashMap<String, String>>);
    impl Vault for Table {
        fn available(&self) -> Result<()> {
            Ok(())
        }
        fn set(&self, account: &str, secret: &str) -> Result<()> {
            self.0.lock().unwrap().insert(account.into(), secret.into());
            Ok(())
        }
        fn get(&self, account: &str) -> Result<Zeroizing<String>> {
            self.0
                .lock()
                .unwrap()
                .get(account)
                .cloned()
                .map(Zeroizing::new)
                .context("no entry")
        }
        fn delete(&self, account: &str) -> Result<()> {
            self.0.lock().unwrap().remove(account);
            Ok(())
        }
    }
    impl Table {
        fn accounts(&self) -> Vec<String> {
            let mut names: Vec<String> = self.0.lock().unwrap().keys().cloned().collect();
            names.sort();
            names
        }
    }

    fn with_key(base: &Config, secret: &str) -> Config {
        let mut config = base.clone();
        config.provider.credential =
            Some(credentials::hold(&config.provider.base_url, secret).unwrap());
        config
    }

    /// Activation either takes effect whole or leaves no trace: an entered key
    /// reaches the store only with a configuration that refers to it, and an
    /// interruption or a failed write leaves the earlier configuration and
    /// its key in force with nothing new left behind.
    #[test]
    fn activation_is_all_or_nothing_and_recovers_from_interruption() {
        let temp = tempfile::tempdir().unwrap();
        let location = Location::for_root(temp.path().join("workspace"));
        let vault = Table::default();
        let mut base = Config::default();
        base.provider.model = "model-a".into();

        // 1. A first activation stores the key and refers to it.
        let first = with_key(&base, "key-one");
        let active = activate(&vault, &location, &first, true, &Verification::default()).unwrap();
        let first_account = active.provider.credential.clone().unwrap().account;
        assert!(!active.provider.credential.as_ref().unwrap().session_only);
        assert_eq!(vault.accounts(), vec![first_account.clone()]);
        assert!(!journal(&location.root).exists());
        assert_eq!(
            Config::load(&location.config)
                .unwrap()
                .provider
                .credential
                .unwrap()
                .account,
            first_account
        );

        // 2. Setup is abandoned after a new key was entered: nothing is stored.
        let _abandoned = with_key(&active, "key-two");
        assert_eq!(vault.accounts(), vec![first_account.clone()]);

        // 3. The process stops after the key was stored and before the
        //    configuration was written. The next start removes that key and
        //    keeps the active configuration and its key.
        let interrupted = with_key(&active, "key-three");
        let pending = interrupted.provider.credential.clone().unwrap();
        write_json(
            &journal(&location.root),
            &Journal {
                account: pending.account.clone(),
                endpoint: pending.endpoint.clone(),
            },
        )
        .unwrap();
        credentials::persist(&vault, &pending).unwrap();
        assert_eq!(vault.accounts().len(), 2);
        assert!(recover(&vault, &location).unwrap());
        assert_eq!(vault.accounts(), vec![first_account.clone()]);
        assert!(!journal(&location.root).exists());
        assert!(!recover(&vault, &location).unwrap(), "recovery runs once");

        // 4. The configuration cannot be written: the new key is removed again
        //    and the earlier configuration is untouched.
        std::fs::write(temp.path().join("not-a-folder"), b"").unwrap();
        let blocked = Location {
            root: location.root.clone(),
            config: temp.path().join("not-a-folder").join("config.toml"),
        };
        let attempt = with_key(&active, "key-four");
        assert!(activate(&vault, &blocked, &attempt, true, &Verification::default()).is_err());
        assert_eq!(vault.accounts(), vec![first_account.clone()]);
        assert!(!journal(&location.root).exists());
        assert_eq!(
            Config::load(&location.config)
                .unwrap()
                .provider
                .credential
                .unwrap()
                .account,
            first_account
        );

        // 5. The same attempt, retried where it can succeed, stores one key.
        //    The earlier key stays: jobs queued under it still need it.
        let retried =
            activate(&vault, &location, &attempt, true, &Verification::default()).unwrap();
        let second_account = retried.provider.credential.unwrap().account;
        let mut expected = vec![first_account.clone(), second_account.clone()];
        expected.sort();
        assert_eq!(vault.accounts(), expected);

        // 6. A journal left by an activation that did complete is only cleared.
        write_json(
            &journal(&location.root),
            &Journal {
                account: second_account,
                endpoint: base.provider.base_url.clone(),
            },
        )
        .unwrap();
        assert!(!recover(&vault, &location).unwrap());
        assert_eq!(vault.accounts().len(), 2);

        // 7. A key kept for the session never reaches the store.
        let session = with_key(&base, "key-five");
        let kept = activate(&vault, &location, &session, false, &Verification::default()).unwrap();
        assert!(kept.provider.credential.unwrap().session_only);
        assert_eq!(vault.accounts().len(), 2);
    }

    /// A check is reused exactly as long as what it established is unchanged.
    #[test]
    fn a_connection_check_covers_only_what_it_established() {
        let mut config = Config::default();
        config.provider.model = "model-a".into();
        config.provider.credential =
            Some(credentials::hold(&config.provider.base_url, "key").unwrap());
        let verified = Verification {
            version: 2,
            provider: Some(ProviderCheck {
                fingerprint: provider_fingerprint(&config),
                verified_at: 1,
                model: config.provider.model.clone(),
                input_tokens: 0,
                output_tokens: 0,
                estimated_cost: None,
            }),
        };

        // Settings that do not bear on access keep the check.
        let mut unrelated = config.clone();
        unrelated.output.title = false;
        unrelated.output.filename = "{title}.md".into();
        unrelated.processing.max_requests = 7;
        unrelated.processing.max_generated_tokens = 9000;
        unrelated.processing.house_style = "British spelling".into();
        unrelated.processing.ocr = "off".into();
        unrelated.processing.page_concurrency = 1;
        unrelated.provider.concurrency = 1;
        unrelated.provider.timeout_seconds = 30;
        unrelated.provider.input_price_per_million = Some(1.0);
        assert!(verified.provider_for(&unrelated).is_some());
        // Storing the same key persistently keeps its account name.
        let mut stored = config.clone();
        stored.provider.credential.as_mut().unwrap().session_only = false;
        assert!(verified.provider_for(&stored).is_some());

        // Each of these changes what was established.
        let changes: [fn(&mut Config); 6] = [
            |c| c.provider.model = "model-b".into(),
            |c| c.provider.vision_model = "model-v".into(),
            |c| c.provider.kind = "chat".into(),
            |c| c.provider.structured_output = "json".into(),
            |c| {
                c.provider.base_url = "https://example.invalid/v1".into();
                c.provider.credential.as_mut().unwrap().endpoint =
                    "https://example.invalid/v1".into();
            },
            |c| {
                c.provider.credential =
                    Some(credentials::hold(&c.provider.base_url, "another-key").unwrap())
            },
        ];
        for change in changes {
            let mut changed = config.clone();
            change(&mut changed);
            assert!(verified.provider_for(&changed).is_none());
        }
    }
}
