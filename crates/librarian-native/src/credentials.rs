//! Credentials never enter configuration values, job snapshots, or argv.
use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashMap,
    process::Command,
    sync::{LazyLock, Mutex},
};
use zeroize::Zeroizing;

const SERVICE: &str = "ai.nampara.librarian.terminal";
static SESSION: LazyLock<Mutex<HashMap<String, Zeroizing<String>>>> =
    LazyLock::new(|| Mutex::new(HashMap::new()));

#[derive(Clone, Debug, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct CredentialRef {
    pub account: String,
    pub endpoint: String,
    pub session_only: bool,
}
impl CredentialRef {
    pub fn check_endpoint(&self, endpoint: &str) -> Result<()> {
        if self.endpoint.trim_end_matches('/') != endpoint.trim_end_matches('/') {
            bail!(
                "This saved credential belongs to a different endpoint. Open Setup to connect this provider."
            );
        }
        if self.account.len() != 64 || !self.account.bytes().all(|c| c.is_ascii_hexdigit()) {
            bail!("Invalid credential reference");
        }
        Ok(())
    }
    fn variable(&self) -> String {
        format!("LIBRARIAN_SESSION_{}", self.account.to_ascii_uppercase())
    }
}

pub fn save(endpoint: &str, secret: &str, remember: bool) -> Result<CredentialRef> {
    let secret = secret.trim();
    if secret.is_empty() || secret.chars().any(char::is_whitespace) {
        bail!("Enter an API key without spaces or line breaks");
    }
    // New key entries are created under fresh names. Existing Keychain entries
    // are never overwritten or deleted, including when setup is cancelled.
    let nonce = tempfile::NamedTempFile::new()?;
    let account = blake3::hash(
        format!(
            "{}:{:?}:{}",
            nonce.path().display(),
            std::time::SystemTime::now(),
            std::process::id()
        )
        .as_bytes(),
    )
    .to_hex()
    .to_string();
    let reference = CredentialRef {
        account,
        endpoint: endpoint.trim_end_matches('/').into(),
        session_only: !remember,
    };
    if remember {
        let entry = keyring::v1::Entry::new(SERVICE, &reference.account).context(
            "System credential store is unavailable. Unlock it or choose 'New session only'.",
        )?;
        entry.set_password(secret).context("Could not save the key in the system credential store. Choose 'New session only' to continue without persistent storage.")?;
        let loaded = Zeroizing::new(
            entry
                .get_password()
                .context("Saved key could not be read back")?,
        );
        if loaded.as_str() != secret {
            bail!("The credential store did not return the saved key");
        }
    } else {
        SESSION
            .lock()
            .map_err(|_| anyhow::anyhow!("Session credential store unavailable"))?
            .insert(reference.account.clone(), Zeroizing::new(secret.to_owned()));
    }
    Ok(reference)
}

pub fn resolve(provider: &crate::config::Provider) -> Result<Option<Zeroizing<String>>> {
    let Some(reference) = &provider.credential else {
        return Ok(std::env::var(&provider.key_env)
            .ok()
            .filter(|v| !v.trim().is_empty())
            .map(Zeroizing::new));
    };
    reference.check_endpoint(&provider.base_url)?;
    if reference.session_only {
        if let Some(secret) = SESSION
            .lock()
            .map_err(|_| anyhow::anyhow!("Session credential store unavailable"))?
            .get(&reference.account)
        {
            return Ok(Some(secret.clone()));
        }
        return std::env::var(reference.variable())
            .ok()
            .filter(|s| !s.is_empty())
            .map(|s| Some(Zeroizing::new(s)))
            .context("Session key is unavailable. Open Setup and enter it for this session.");
    }
    Ok(Some(Zeroizing::new(
        keyring::v1::Entry::new(SERVICE, &reference.account)
            .context("System credential store unavailable")?
            .get_password()
            .context("Could not load saved API key; unlock your credential store or open Setup")?,
    )))
}

pub fn attach_session(command: &mut Command, provider: &crate::config::Provider) -> Result<()> {
    if let Some(reference) = &provider.credential {
        reference.check_endpoint(&provider.base_url)?;
        if reference.session_only {
            let secret = resolve(provider)?.context("Session key unavailable")?;
            command.env(reference.variable(), secret.as_str());
        }
    }
    Ok(())
}

/// Reconnect an explicitly selected session reference without changing queued
/// job snapshots or touching any persistent credential-store entry.
pub fn reconnect_session(
    reference: &CredentialRef,
    endpoint: &str,
    secret: &str,
) -> Result<CredentialRef> {
    reference.check_endpoint(endpoint)?;
    if !reference.session_only {
        bail!("Only session credentials can be reconnected in memory");
    }
    let secret = secret.trim();
    if secret.is_empty() || secret.chars().any(char::is_whitespace) {
        bail!("Enter an API key without spaces or line breaks");
    }
    SESSION
        .lock()
        .map_err(|_| anyhow::anyhow!("Session credential store unavailable"))?
        .insert(reference.account.clone(), Zeroizing::new(secret.to_owned()));
    Ok(reference.clone())
}
