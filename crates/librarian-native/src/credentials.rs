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

/// Where keys that outlive the process are kept: the operating system's
/// credential store in the program, a table in the tests of the activation
/// transaction.
pub trait Vault {
    /// Whether the store can be reached at all. Asking writes nothing.
    fn available(&self) -> Result<()>;
    fn set(&self, account: &str, secret: &str) -> Result<()>;
    fn get(&self, account: &str) -> Result<Zeroizing<String>>;
    /// Succeeds when the entry is gone, whether or not it existed.
    fn delete(&self, account: &str) -> Result<()>;
}

/// Keychain on macOS, Credential Manager on Windows, Secret Service on Linux.
pub struct SystemVault;

impl Vault for SystemVault {
    fn available(&self) -> Result<()> {
        keyring::v1::Entry::new(SERVICE, "availability")
            .map(|_| ())
            .context(
                "The system credential store is not available on this computer. The key can be kept for this session only.",
            )
    }
    fn set(&self, account: &str, secret: &str) -> Result<()> {
        keyring::v1::Entry::new(SERVICE, account)
            .context(
                "The system credential store is not available. The key can be kept for this session only.",
            )?
            .set_password(secret)
            .context("The key could not be saved in the system credential store. Unlock the store, or keep the key for this session only.")
    }
    fn get(&self, account: &str) -> Result<Zeroizing<String>> {
        Ok(Zeroizing::new(
            keyring::v1::Entry::new(SERVICE, account)
                .context("System credential store unavailable")?
                .get_password()
                .context(
                    "Could not load saved API key; unlock your credential store or open Setup",
                )?,
        ))
    }
    fn delete(&self, account: &str) -> Result<()> {
        let entry = keyring::v1::Entry::new(SERVICE, account)
            .context("System credential store unavailable")?;
        match entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
            Err(error) => Err(error).context("Could not remove the unreferenced key"),
        }
    }
}

fn checked(secret: &str) -> Result<&str> {
    let secret = secret.trim();
    if secret.is_empty() || secret.chars().any(char::is_whitespace) {
        bail!("Enter an API key without spaces or line breaks");
    }
    Ok(secret)
}

/// Keep a newly entered key in the memory of this process under a new
/// reference. Nothing is written anywhere. Setup works with this reference
/// until the profile is activated; only then may the key be persisted.
pub fn hold(endpoint: &str, secret: &str) -> Result<CredentialRef> {
    let secret = checked(secret)?;
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
        session_only: true,
    };
    SESSION
        .lock()
        .map_err(|_| anyhow::anyhow!("Session credential store unavailable"))?
        .insert(reference.account.clone(), Zeroizing::new(secret.to_owned()));
    Ok(reference)
}

/// Write the held key of `reference` to the vault under the same account
/// name and read it back. Returns the persistent form of the reference. An
/// account name is new for every entered key, so no existing entry is
/// overwritten.
pub fn persist(vault: &dyn Vault, reference: &CredentialRef) -> Result<CredentialRef> {
    let secret = SESSION
        .lock()
        .map_err(|_| anyhow::anyhow!("Session credential store unavailable"))?
        .get(&reference.account)
        .cloned()
        .context("The key to save is no longer held; enter it again")?;
    vault.set(&reference.account, &secret)?;
    let loaded = vault
        .get(&reference.account)
        .context("Saved key could not be read back")?;
    if loaded.as_str() != secret.as_str() {
        bail!("The credential store did not return the saved key");
    }
    Ok(CredentialRef {
        session_only: false,
        ..reference.clone()
    })
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
    Ok(Some(SystemVault.get(&reference.account)?))
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
    let secret = checked(secret)?;
    SESSION
        .lock()
        .map_err(|_| anyhow::anyhow!("Session credential store unavailable"))?
        .insert(reference.account.clone(), Zeroizing::new(secret.to_owned()));
    Ok(reference.clone())
}
