//! Setup. A workspace is all that Home and parsing need. The provider, the
//! key, the model and the connection check are asked for when work that calls
//! a model is first chosen, or from the Setup menu.
//!
//! Nothing outside this process changes until a section is saved. A key that
//! is entered is held in memory; it reaches the system credential store only
//! in the activation transaction (`profile::activate`). Leaving Setup at any
//! earlier point leaves the active configuration and its key as they were.
use crate::{setup::expand_path, ui};
use anyhow::{Context, Result, bail};
use librarian_native::{
    assets, catalog,
    config::Config,
    credentials::{self, CredentialRef, SystemVault},
    ocr,
    profile::{self, Ai, Location, ProviderCheck, SavedIncomplete, Verification},
    store::{Library, now},
};
use serde::{Deserialize, Serialize};
use std::{
    collections::HashSet,
    io::Read,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::atomic::Ordering,
    time::Duration,
};
use zeroize::Zeroizing;

/// The parts of Setup that can be run on their own.
#[derive(Clone, Copy, PartialEq, Eq, Debug, Serialize, Deserialize)]
pub enum Section {
    /// Choose and prepare the workspace. All that the first start asks.
    Workspace,
    /// Provider, key, model, per-document limit and the connection check.
    Provider,
    Processing,
    Output,
    Engines,
    /// Every step in order.
    All,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug, Serialize, Deserialize)]
enum Step {
    Welcome,
    Workspace,
    Storage,
    Provider,
    Credential,
    Remember,
    Models,
    Model,
    Limit,
    Processing,
    Output,
    Engines,
    Check,
    Activate,
}

impl Section {
    fn steps(self) -> &'static [Step] {
        use Step::*;
        match self {
            Section::Workspace => &[Welcome, Workspace, Storage],
            Section::Provider => &[
                Provider, Credential, Remember, Models, Model, Limit, Check, Activate,
            ],
            Section::Processing => &[Processing],
            Section::Output => &[Output],
            Section::Engines => &[Engines],
            Section::All => &[
                Welcome, Workspace, Storage, Provider, Credential, Remember, Models, Model, Limit,
                Processing, Output, Engines, Check, Activate,
            ],
        }
    }
    fn name(self) -> &'static str {
        match self {
            Section::Workspace => "Workspace",
            Section::Provider => "AI provider",
            Section::Processing => "Processing defaults",
            Section::Output => "Markdown output defaults",
            Section::Engines => "Local engines",
            Section::All => "All steps",
        }
    }
    /// Sections long enough that an interrupted run is worth resuming.
    fn drafted(self) -> bool {
        matches!(self, Section::Provider | Section::All)
    }
}

/// Steps that do work without asking anything.
fn automatic(step: Step) -> bool {
    matches!(step, Step::Storage | Step::Models | Step::Engines)
}

#[derive(Serialize, Deserialize)]
struct Draft {
    section: Section,
    location: Location,
    config: Config,
    step: Step,
}

enum Flow {
    Next,
    Back,
    Stay,
    Go(Step),
    /// The section saved itself.
    Done,
}

struct Wizard {
    section: Section,
    /// Where Setup began. The draft is kept here.
    start: Location,
    location: Location,
    config: Config,
    /// The configuration when Setup began.
    baseline: Config,
    /// What is on record as verified, and the workspace it is on record for.
    stored: Verification,
    stored_for: Location,
    /// The key as typed in this run, for reconnecting a session reference.
    secret: Zeroizing<String>,
    /// Store the held key in the system credential store on activation.
    remember: bool,
    /// Accounts whose key was entered in this run. A recorded check does not
    /// cover them, whatever its fingerprint.
    rekeyed: HashSet<String>,
    /// In the full sequence, the owner chose to leave the provider for later.
    skip_ai: bool,
    storage_ready: Option<PathBuf>,
    models: Option<(String, Vec<catalog::Model>)>,
    engines_ready: Option<(PathBuf, String)>,
    /// A connection check passed in this run.
    checked: Option<ProviderCheck>,
}

fn choice(
    t: &mut ratatui::DefaultTerminal,
    title: &str,
    description: &str,
    labels: &[&str],
    initial: usize,
) -> Result<Option<usize>> {
    ui::choose(
        t,
        title,
        description,
        &labels.iter().map(|s| (*s).into()).collect::<Vec<_>>(),
        initial,
    )
}

/// A calendar date in UTC for a time in seconds since 1970.
pub fn date(seconds: i64) -> String {
    let days = seconds.div_euclid(86_400);
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!("{year:04}-{month:02}-{day:02}")
}

fn provider_name(config: &Config) -> String {
    catalog::providers()
        .into_iter()
        .find(|p| p.id == config.provider.catalog_id)
        .map(|p| p.name)
        .unwrap_or_else(|| config.provider.catalog_id.clone())
}

/// One line on the state of the provider, for Home and the Setup menu.
pub fn ai_line(location: &Location, config: &Config) -> String {
    let who = format!("{} · {}", provider_name(config), config.provider.model);
    match profile::ai_state(location, config) {
        Ai::Ready => match Verification::load(location, config).provider_for(config) {
            Some(check) => format!("{who} · connection verified {}", date(check.verified_at)),
            None => who,
        },
        Ai::Unverified => format!("{who} · not verified"),
        Ai::KeyMissing => format!("{who} · the session key has not been entered in this session"),
        Ai::NotConnected => "not connected".into(),
        Ai::NotNeeded => {
            if config.provider.model.is_empty() {
                "not connected; editing, classification and visual reading are off".into()
            } else {
                format!("{who} · not used: editing, classification and visual reading are off")
            }
        }
    }
}

/// One line on the state of the local engines.
pub fn engines_line(root: &Path) -> String {
    let missing = assets::missing(&root.join("engines"), assets::ALL);
    if missing.is_empty() {
        "PDF and OCR engines installed".into()
    } else if !missing.iter().any(|m| m == "pdfium") {
        "PDF engine installed; OCR engine not installed".into()
    } else {
        "not installed; installed when first needed".into()
    }
}

/// The engines a configuration uses: OCR models only for the bundled OCR.
pub fn engines_for(config: &Config) -> &'static [&'static str] {
    if config.processing.ocr == "paddle" {
        assets::ALL
    } else {
        assets::PDF
    }
}

fn install_task(
    t: &mut ratatui::DefaultTerminal,
    title: &str,
    description: &str,
    root: PathBuf,
    need: &'static [&'static str],
    tesseract: Option<String>,
) -> Result<ui::Ended<()>> {
    ui::task_ended(t, title, description, move |progress, cancel| {
        let engines = root.join("engines");
        let load = |engines: &Path| -> Result<()> {
            if need.len() > 1 {
                assets::load_engines(engines)
            } else {
                assets::load_pdf(engines)
            }
        };
        if load(&engines).is_err() {
            let _ = progress.send("Downloading pinned engine files…".into());
            tokio::runtime::Runtime::new()?.block_on(async {
                let operation = assets::install_selected(&engines, need, |name, done, total| {
                    let _ = progress.send(format!(
                        "{name}: {} / {} MiB",
                        done / 1_048_576,
                        total.unwrap_or(0) / 1_048_576
                    ));
                });
                tokio::pin!(operation);
                loop {
                    tokio::select! {
                        result = &mut operation => break result,
                        _ = tokio::time::sleep(Duration::from_millis(100)) => {
                            if cancel.load(Ordering::Relaxed) {
                                bail!("Engine installation cancelled");
                            }
                        }
                    }
                }
            })?;
            load(&engines)?;
        }
        if let Some(language) = &tesseract {
            let installed = ocr::tesseract_languages()?;
            ocr::tesseract_language(language, &installed)?;
        }
        let _ = progress.send(
            if need.len() > 1 {
                "The PDF engine and the OCR models are installed and load"
            } else {
                "The PDF engine is installed and loads"
            }
            .into(),
        );
        Ok(())
    })
}

/// Make sure the named engines are present, installing them if not. Returns
/// false if the owner went back or the installation failed.
pub fn ensure_engines(
    t: &mut ratatui::DefaultTerminal,
    root: &Path,
    need: &'static [&'static str],
) -> Result<bool> {
    if assets::missing(&root.join("engines"), need).is_empty() {
        return Ok(true);
    }
    let description = if need.len() > 1 {
        "The PDF engine and the OCR models are not installed yet. They are downloaded once into the workspace and verified against pinned checksums. Nothing else is installed."
    } else {
        "The PDF engine is not installed yet. It is downloaded once into the workspace and verified against a pinned checksum. Nothing else is installed."
    };
    match install_task(
        t,
        "Installing local engines",
        description,
        root.to_owned(),
        need,
        None,
    ) {
        Ok(ui::Ended::Continue(()) | ui::Ended::Back(Some(()))) => Ok(true),
        Ok(ui::Ended::Back(None)) => Ok(false),
        Err(error) if error.is::<ui::SaveExit>() => Err(error),
        Err(error) => {
            ui::notice(
                t,
                "The engines could not be installed",
                &format!(
                    "{error:#}\n\nAn engine installed before this point is kept. Check the network connection and try again, or run: librarian engines install"
                ),
            )?;
            Ok(false)
        }
    }
}

const BILLING: &str = "This sends one request to the provider, and the provider bills it: one small built-in image and at most 1,024 generated tokens. No document of yours is sent. It confirms the key, image input and a structured reply. It does not measure the quality of document processing.";
const BILLING_TEXT: &str = "This sends one request to the provider, and the provider bills it: one built-in sentence and at most 1,024 generated tokens. No document of yours is sent and no image is sent, because the fast pipeline is selected with image reading off. It confirms the key, the model and a structured reply. It does not measure the quality of document processing.";

/// The disclosure for the check this configuration would send.
fn billing(config: &Config) -> &'static str {
    if profile::text_only(config) {
        BILLING_TEXT
    } else {
        BILLING
    }
}

/// Send the connection check from a separate process that loads the profile
/// as a worker would.
fn check_task(
    t: &mut ratatui::DefaultTerminal,
    title: &str,
    location: &Location,
    config: &Config,
) -> Result<ui::Ended<ProviderCheck>> {
    config.validate()?;
    let location = location.clone();
    let config = config.clone();
    ui::task_ended(
        t,
        title,
        "Sending one billed request from a separate worker process. Esc stops waiting; a request already sent may still be billed.",
        move |progress, cancel| {
            std::fs::create_dir_all(&location.root)?;
            let pending = tempfile::Builder::new()
                .prefix("setup-profile-")
                .suffix(".toml")
                .tempfile_in(&location.root)?;
            config.save(pending.path())?;
            let log = tempfile::NamedTempFile::new_in(&location.root)?;
            let mut command = Command::new(std::env::current_exe()?);
            command
                .arg("--library")
                .arg(&location.root)
                .arg("--config")
                .arg(pending.path())
                .arg("setup-verify")
                .stdin(Stdio::null())
                .stdout(log.reopen()?)
                .stderr(Stdio::null());
            credentials::attach_session(&mut command, &config.provider)?;
            let mut child = command
                .spawn()
                .context("Cannot start the verification worker")?;
            let started = std::time::Instant::now();
            let status = loop {
                if cancel.load(Ordering::Relaxed)
                    || started.elapsed() > Duration::from_secs(config.provider.timeout_seconds + 30)
                {
                    let _ = child.kill();
                    let _ = child.wait();
                    bail!("The connection check was stopped or timed out");
                }
                if let Some(status) = child.try_wait()? {
                    break status;
                }
                std::thread::sleep(Duration::from_millis(100));
            };
            let mut text = String::new();
            log.reopen()?.take(65536).read_to_string(&mut text)?;
            let report: serde_json::Value = serde_json::from_str(&text)
                .context("The verification worker ended without a readable result")?;
            if !status.success() {
                bail!(
                    "{}",
                    report["error"]
                        .as_str()
                        .unwrap_or("The connection check failed")
                );
            }
            let check = ProviderCheck {
                fingerprint: profile::provider_fingerprint(&config),
                verified_at: now(),
                model: config.provider.model.clone(),
                input_tokens: report["input_tokens"].as_i64().unwrap_or(0),
                output_tokens: report["output_tokens"].as_i64().unwrap_or(0),
                estimated_cost: report["estimated_cost"].as_f64(),
                image: report["image_input_and_structured_output"]
                    .as_bool()
                    .unwrap_or(false),
            };
            let _ = progress.send(format!(
                "A separate worker authenticated; {} and the structured reply were correct. {} input and {} output tokens were used.",
                if check.image {
                    "the image"
                } else {
                    "the repaired sentence"
                },
                check.input_tokens,
                check.output_tokens
            ));
            Ok(check)
        },
    )
}

impl Wizard {
    fn open(
        t: &mut ratatui::DefaultTerminal,
        initial: Location,
        section: Section,
    ) -> Result<Option<(Self, Option<Step>)>> {
        // An activation that was cut short is settled before anything else.
        let _ = profile::recover(&SystemVault, &initial);
        let baseline = Config::load(&initial.config)?;
        Verification::carry_forward(&initial, &baseline);
        let mut config = baseline.clone();
        // Earlier profiles have no preset name. Recognize their endpoint.
        if let Some(preset) = catalog::providers().into_iter().find(|p| {
            p.base_url.trim_end_matches('/') == config.provider.base_url.trim_end_matches('/')
        }) {
            config.provider.catalog_id = preset.id;
        }
        let mut wizard = Self {
            section,
            stored: Verification::load(&initial, &baseline),
            stored_for: initial.clone(),
            start: initial.clone(),
            location: initial,
            config,
            baseline,
            secret: Zeroizing::new(String::new()),
            remember: true,
            rekeyed: HashSet::new(),
            skip_ai: false,
            storage_ready: None,
            models: None,
            engines_ready: None,
            checked: None,
        };
        let mut resume = None;
        if section.drafted()
            && let Ok(bytes) = std::fs::read(wizard.draft_path())
            && let Ok(saved) = serde_json::from_slice::<Draft>(&bytes)
            && saved.section == section
        {
            match choice(
                t,
                "Resume setup",
                "An unfinished setup is saved. No key is kept in it; a key entered then must be entered again.",
                &[
                    "Resume the saved setup",
                    "Start from the current configuration",
                ],
                0,
            )? {
                Some(0) => {
                    wizard.location = saved.location;
                    wizard.config = saved.config;
                    resume = Some(saved.step);
                    // A key that was only held in the earlier process is gone.
                    let held_then = wizard
                        .config
                        .provider
                        .credential
                        .as_ref()
                        .is_some_and(|c| c.session_only)
                        && credentials::resolve(&wizard.config.provider).is_err();
                    if held_then {
                        wizard.config.provider.credential =
                            wizard.baseline.provider.credential.clone();
                        resume = Some(Step::Credential);
                    }
                }
                Some(_) => {}
                None => return Ok(None),
            }
        }
        Ok(Some((wizard, resume)))
    }

    fn draft_path(&self) -> PathBuf {
        self.start.root.join("setup-draft.json")
    }

    fn save_draft(&self, step: Step) -> Result<()> {
        if !self.section.drafted() {
            return Ok(());
        }
        std::fs::create_dir_all(&self.start.root)?;
        profile::write_json(
            &self.draft_path(),
            &Draft {
                section: self.section,
                location: self.location.clone(),
                config: self.config.clone(),
                step,
            },
        )
    }

    fn clear_draft(&self) {
        let _ = std::fs::remove_file(self.draft_path());
    }

    fn changed(&self) -> bool {
        self.location != self.start
            || serde_json::to_value(&self.config).ok() != serde_json::to_value(&self.baseline).ok()
    }

    fn applies(&self, step: Step) -> bool {
        match step {
            Step::Welcome => !self.start.established(),
            Step::Credential | Step::Models | Step::Model | Step::Limit | Step::Check => {
                !self.skip_ai
            }
            Step::Remember => !self.skip_ai && self.holding_new_key(),
            _ => true,
        }
    }

    /// The configuration refers to a key entered in this run and not stored.
    fn holding_new_key(&self) -> bool {
        self.config
            .provider
            .credential
            .as_ref()
            .is_some_and(|c| c.session_only && self.rekeyed.contains(&c.account))
            && !self.secret.is_empty()
    }

    fn discovery_key(&self) -> String {
        let p = &self.config.provider;
        format!(
            "{}|{}|{}|{}",
            p.catalog_id,
            p.kind,
            p.base_url,
            p.credential
                .as_ref()
                .map(|c| c.account.clone())
                .unwrap_or_else(|| p.key_env.clone())
        )
    }

    /// Whether an automatic step already has a result that still holds.
    fn settled(&self, step: Step) -> bool {
        match step {
            Step::Storage => self.storage_ready.as_ref() == Some(&self.location.root),
            Step::Models => self
                .models
                .as_ref()
                .is_some_and(|(key, _)| *key == self.discovery_key()),
            Step::Engines => {
                self.engines_ready
                    == Some((
                        self.location.root.clone(),
                        self.config.processing.ocr.clone(),
                    ))
            }
            _ => false,
        }
    }

    /// The check that covers the configuration as it now stands, if any.
    fn verified(&self) -> Option<&ProviderCheck> {
        let fingerprint = profile::provider_fingerprint(&self.config);
        if let Some(check) = self
            .checked
            .as_ref()
            .filter(|c| c.fingerprint == fingerprint)
            // A check that sent no image covers only the text-only configuration.
            .filter(|c| c.image || profile::text_only(&self.config))
        {
            return Some(check);
        }
        let rekeyed = self
            .config
            .provider
            .credential
            .as_ref()
            .is_some_and(|c| self.rekeyed.contains(&c.account));
        if rekeyed || self.location != self.stored_for {
            return None;
        }
        self.stored.provider_for(&self.config)
    }

    fn next(&self, steps: &[Step], from: Option<usize>) -> Option<usize> {
        let first = from.map_or(0, |i| i + 1);
        (first..steps.len())
            .find(|i| self.applies(steps[*i]) && !(automatic(steps[*i]) && self.settled(steps[*i])))
    }

    /// Back never lands on an automatic step: a settled one is passed over,
    /// and one that is not settled runs again on the way forward.
    fn back(&self, steps: &[Step], from: usize) -> Option<usize> {
        (0..from)
            .rev()
            .find(|i| self.applies(steps[*i]) && !automatic(steps[*i]))
    }

    fn step(&mut self, t: &mut ratatui::DefaultTerminal, title: &str, step: Step) -> Result<Flow> {
        match step {
            Step::Welcome => {
                let text = "Librarian keeps its work in a workspace folder: originals, the queue, caches and the search index. Choose it next.\n\nWith a workspace you can parse PDFs at once. Parsing runs on this computer and needs no account, no key and no payment.\n\nCleaning documents with AI needs a provider and a key. Librarian asks for them when you first choose that work, or from Setup.\n\nArrow keys or Tab move. Enter or Space chooses. Esc goes back.";
                Ok(
                    if choice(t, title, text, &["Choose the workspace"], 0)?.is_some() {
                        Flow::Next
                    } else {
                        Flow::Back
                    },
                )
            }
            Step::Workspace => {
                let text = format!(
                    "Where should Librarian keep its originals, jobs, caches and search index?\n\n{}\n\nThe folder for finished Markdown is chosen with each import. This workspace is opened at every later start.",
                    self.location.root.display()
                );
                match choice(
                    t,
                    title,
                    &text,
                    &[
                        "Use this workspace",
                        "Browse for a workspace",
                        "Type a workspace path",
                    ],
                    0,
                )? {
                    Some(0) => return Ok(Flow::Next),
                    Some(1) => {
                        if let Some(path) = ui::browse(t, &self.location.root, true)? {
                            self.select_workspace(t, path)?;
                        }
                    }
                    Some(2) => {
                        if let Some(path) = ui::input(
                            t,
                            title,
                            "Workspace folder. It is created if it does not exist.",
                            &self.location.root.to_string_lossy(),
                            false,
                        )? {
                            let path = expand_path(&path);
                            if path.as_os_str().is_empty() {
                                bail!("Choose a workspace folder");
                            }
                            self.select_workspace(
                                t,
                                if path.is_absolute() {
                                    path
                                } else {
                                    std::env::current_dir()?.join(path)
                                },
                            )?;
                        }
                    }
                    None => return Ok(Flow::Back),
                    _ => {}
                }
                Ok(Flow::Stay)
            }
            Step::Storage => {
                let root = self.location.root.clone();
                let ended = ui::task_ended(
                    t,
                    title,
                    "Preparing the workspace. No document is imported.",
                    move |progress, _| {
                        let library = Library::open(&root)?;
                        let probe = tempfile::NamedTempFile::new_in(&library.root)?;
                        probe.as_file().sync_all()?;
                        let _ =
                            progress.send("The workspace folder and its database are ready".into());
                        Ok(library.root)
                    },
                )?;
                let (root, flow) = match ended {
                    ui::Ended::Continue(root) => (Some(root), Flow::Next),
                    ui::Ended::Back(root) => (root, Flow::Back),
                };
                if let Some(root) = root {
                    // The folder is now known by its canonical path.
                    let default_config =
                        self.location.config == self.location.root.join("config.toml");
                    self.location.root = root.clone();
                    if default_config {
                        self.location.config = root.join("config.toml");
                    }
                    self.storage_ready = Some(root);
                }
                Ok(flow)
            }
            Step::Provider => self.provider(t, title),
            Step::Credential => self.credential(t, title),
            Step::Remember => self.remember(t, title),
            Step::Models => {
                let config = self.config.clone();
                let ended = ui::task_ended(
                    t,
                    title,
                    "Asking the provider for its current list of models. No charge is made for this request.",
                    move |progress, _| {
                        let models =
                            tokio::runtime::Runtime::new()?.block_on(catalog::discover(&config))?;
                        let _ =
                            progress.send(format!("The provider listed {} models", models.len()));
                        Ok(models)
                    },
                )?;
                let (found, flow) = match ended {
                    ui::Ended::Continue(found) => (Some(found), Flow::Next),
                    ui::Ended::Back(found) => (found, Flow::Back),
                };
                if let Some(found) = found {
                    self.models = Some((self.discovery_key(), found));
                }
                Ok(flow)
            }
            Step::Model => self.model(t, title),
            Step::Limit => self.limit(t, title),
            Step::Processing => {
                let label = if self.section == Section::Processing {
                    "Save and return"
                } else {
                    "Continue"
                };
                Ok(if preferences(t, &mut self.config, false, label)? {
                    Flow::Next
                } else {
                    Flow::Back
                })
            }
            Step::Output => {
                let label = if self.section == Section::Output {
                    "Save and return"
                } else {
                    "Continue"
                };
                Ok(if preferences(t, &mut self.config, true, label)? {
                    Flow::Next
                } else {
                    Flow::Back
                })
            }
            Step::Engines => {
                let tesseract = (self.config.processing.ocr == "tesseract")
                    .then(|| self.config.processing.ocr_language.clone());
                let ended = install_task(
                    t,
                    title,
                    "Checking the local engines. Engines supplied with the program are used as they are. Missing engines are downloaded into the workspace and verified against pinned checksums.",
                    self.location.root.clone(),
                    engines_for(&self.config),
                    tesseract,
                )?;
                let (done, flow) = match ended {
                    ui::Ended::Continue(()) => (true, Flow::Next),
                    ui::Ended::Back(done) => (done.is_some(), Flow::Back),
                };
                if done {
                    self.engines_ready = Some((
                        self.location.root.clone(),
                        self.config.processing.ocr.clone(),
                    ));
                }
                Ok(flow)
            }
            Step::Check => self.check(t, title),
            Step::Activate => self.activate(t, title),
        }
    }

    fn provider(&mut self, t: &mut ratatui::DefaultTerminal, title: &str) -> Result<Flow> {
        let presets = catalog::providers();
        let mut labels = presets
            .iter()
            .map(|p| {
                format!(
                    "{}  · {}",
                    p.name,
                    p.vision_score
                        .map(|s| format!("Vision Arena {s}; {}", p.ranked_model))
                        .unwrap_or_else(|| "unranked / catalog-dependent".into())
                )
            })
            .collect::<Vec<_>>();
        if self.section == Section::All {
            labels.push("Leave the AI provider for later (parsing needs none)".into());
        }
        // With nothing chosen yet the list opens at its first entry.
        let chosen =
            !self.config.provider.model.is_empty() || self.config.provider.credential.is_some();
        let initial = presets
            .iter()
            .position(|p| chosen && p.id == self.config.provider.catalog_id)
            .unwrap_or(0);
        let description = format!(
            "Choose a provider. Its endpoint is filled in.\nProviders with a score are sorted by the Vision Arena score of their best listed model, dated {}. Overlapping scores do not establish that one is better.\nGateways, hosting providers and local servers have no comparable score and are listed as unranked.\nSource: {}",
            catalog::RANK_DATE,
            catalog::RANK_SOURCE
        );
        let Some(index) = ui::choose(t, title, &description, &labels, initial)? else {
            return Ok(Flow::Back);
        };
        if index == presets.len() {
            self.skip_ai = true;
            self.config.provider = self.baseline.provider.clone();
            return Ok(Flow::Next);
        }
        self.skip_ai = false;
        let preset = &presets[index];
        let same_endpoint = preset.id == self.config.provider.catalog_id
            && (preset.id == "custom" || preset.base_url == self.config.provider.base_url);
        if !same_endpoint {
            if preset.id == self.baseline.provider.catalog_id
                && (preset.id == "custom" || preset.base_url == self.baseline.provider.base_url)
            {
                // Returning to the provider in force restores its settings.
                self.config.provider = self.baseline.provider.clone();
                self.config.processing.max_cost = self.baseline.processing.max_cost;
            } else {
                preset.apply(&mut self.config);
            }
            self.secret = Zeroizing::new(String::new());
        }
        if preset.id == "custom" {
            let Some(url) = ui::input(
                t,
                "Custom endpoint",
                "Enter the API base URL. HTTPS is required except for a server on this computer.",
                &self.config.provider.base_url,
                false,
            )?
            else {
                return Ok(Flow::Stay);
            };
            if url.trim_end_matches('/') != self.config.provider.base_url.trim_end_matches('/') {
                self.config.provider.credential = None;
                self.config.provider.model.clear();
                self.config.provider.vision_model.clear();
                self.config.provider.base_url = url.to_string();
            }
            let kinds = ["chat", "responses", "anthropic"];
            let Some(mode) = choice(
                t,
                "Custom API protocol",
                "Choose the protocol this endpoint implements.",
                &[
                    "OpenAI-compatible Chat Completions",
                    "OpenAI Responses",
                    "Anthropic Messages",
                ],
                kinds
                    .iter()
                    .position(|k| *k == self.config.provider.kind)
                    .unwrap_or(0),
            )?
            else {
                return Ok(Flow::Stay);
            };
            self.config.provider.kind = kinds[mode].into();
            self.config.provider.prompt_cache = if mode == 2 { "ephemeral" } else { "off" }.into();
        }
        self.config.validate()?;
        Ok(Flow::Next)
    }

    fn credential(&mut self, t: &mut ratatui::DefaultTerminal, title: &str) -> Result<Flow> {
        let p = &self.config.provider;
        let preset = catalog::providers()
            .into_iter()
            .find(|v| v.id == p.catalog_id);
        let help = preset
            .as_ref()
            .map(|p| p.key_url.clone())
            .unwrap_or_default();
        let held = self.holding_new_key();
        let saved = !held
            && p.credential
                .as_ref()
                .is_some_and(|r| !r.session_only || credentials::resolve(p).is_ok());
        let environment = !p.key_env.is_empty() && std::env::var_os(&p.key_env).is_some();
        let local = catalog::local(&self.config);
        let mut labels: Vec<(&str, String)> = Vec::new();
        if held {
            labels.push(("held", "Use the key entered a moment ago".into()));
        }
        if saved {
            labels.push(("saved", "Use the saved key".into()));
        }
        labels.push(("paste", "Paste a new API key".into()));
        if environment {
            labels.push(("environment", format!("Import the key from {}", p.key_env)));
        }
        if local {
            labels.push(("none", "This local server needs no key".into()));
        }
        if !help.is_empty() {
            labels.push(("page", "Open the provider's API-key page".into()));
        }
        let description = format!(
            "Connect to:\n{}\n\nA pasted key is shown as dots. It is kept in memory until this setup is saved. It is never written to the configuration file, job records, logs or command-line arguments.\nIf you have no key, create one on the provider's key page and enable billing there.\n{}",
            p.base_url, help
        );
        let names: Vec<String> = labels.iter().map(|(_, l)| l.clone()).collect();
        let Some(index) = ui::choose(t, title, &description, &names, 0)? else {
            return Ok(Flow::Back);
        };
        match labels[index].0 {
            "held" => Ok(Flow::Next),
            "saved" => {
                let provider = self.config.provider.clone();
                let ended = ui::task_ended(
                    t,
                    title,
                    "Reading the saved key. The operating system may ask you to unlock its credential store.",
                    move |progress, _| {
                        credentials::resolve(&provider)?
                            .context("The saved key is not available")?;
                        let _ = progress.send("The saved key was read".into());
                        Ok(())
                    },
                )?;
                Ok(match ended {
                    ui::Ended::Continue(()) => Flow::Next,
                    ui::Ended::Back(_) => Flow::Stay,
                })
            }
            "none" => {
                self.config.provider.credential = None;
                self.config.provider.key_env.clear();
                self.secret = Zeroizing::new(String::new());
                Ok(Flow::Next)
            }
            "page" => {
                open_link(&help)?;
                Ok(Flow::Stay)
            }
            kind => {
                let value = if kind == "paste" {
                    let Some(value) = ui::input(
                        t,
                        title,
                        "Paste your API key. Enter accepts it. Esc returns without keeping it.",
                        "",
                        true,
                    )?
                    else {
                        return Ok(Flow::Stay);
                    };
                    if value.trim().is_empty() {
                        bail!("Enter a key, or go back and choose another way to connect");
                    }
                    Zeroizing::new(value.trim().to_owned())
                } else {
                    Zeroizing::new(
                        std::env::var(&self.config.provider.key_env)
                            .context("The environment variable is no longer set")?,
                    )
                };
                // Held in memory under a new reference. Nothing is stored yet.
                let reference = credentials::hold(&self.config.provider.base_url, &value)?;
                // A check passed earlier in this run was for another key.
                self.checked = None;
                self.rekeyed.insert(reference.account.clone());
                self.config.provider.namespace = reference.account.clone();
                self.config.provider.credential = Some(reference);
                self.secret = value;
                self.remember = true;
                Ok(Flow::Next)
            }
        }
    }

    fn remember(&mut self, t: &mut ratatui::DefaultTerminal, title: &str) -> Result<Flow> {
        let previous: Option<CredentialRef> =
            self.baseline.provider.credential.clone().filter(|r| {
                r.session_only && r.check_endpoint(&self.config.provider.base_url).is_ok()
            });
        let mut options = vec![
            "Remember it in the system credential store".to_string(),
            "Keep it for this session only".to_string(),
        ];
        if previous.is_some() {
            options.push("Reconnect the previous session key, including its queued jobs".into());
        }
        let Some(index) = ui::choose(
            t,
            title,
            "Should this key be remembered on this computer?\n\nThe system credential store is Keychain on macOS, Credential Manager on Windows and Secret Service on Linux. The key is written there only when this setup is saved; until then nothing is stored. Existing entries are never changed.\n\nA session key is kept in memory and is asked for again at the next start.\nReconnecting makes the entered key available to jobs already queued under the previous session reference.",
            &options,
            if previous.is_some() {
                2
            } else {
                usize::from(!self.remember)
            },
        )?
        else {
            return Ok(Flow::Back);
        };
        match index {
            0 => self.remember = true,
            1 => self.remember = false,
            _ => {
                let reference = credentials::reconnect_session(
                    previous.as_ref().context("No previous session key")?,
                    &self.config.provider.base_url,
                    &self.secret,
                )?;
                self.checked = None;
                self.rekeyed.insert(reference.account.clone());
                self.config.provider.namespace = reference.account.clone();
                self.config.provider.credential = Some(reference);
                self.remember = false;
            }
        }
        Ok(Flow::Next)
    }

    fn model(&mut self, t: &mut ratatui::DefaultTerminal, title: &str) -> Result<Flow> {
        let Some((_, models)) = self.models.clone().filter(|_| self.settled(Step::Models)) else {
            return Ok(Flow::Go(Step::Models));
        };
        let eligible = models.iter().collect::<Vec<_>>();
        if eligible.is_empty() {
            bail!("The provider lists no model. Go back and choose another provider.");
        }
        let mut labels = eligible
            .iter()
            .map(|m| {
                format!(
                    "{}  · {}{}",
                    m.name,
                    match m.vision {
                        Some(true) => "image input reported",
                        Some(false) => "text only",
                        None => "image support unverified",
                    },
                    m.input_price
                        .zip(m.output_price)
                        .map(|(i, o)| format!(" · ${i}/{o} per million in/out tokens"))
                        .unwrap_or_default()
                )
            })
            .collect::<Vec<_>>();
        labels.push("Ask the provider for the list again".into());
        let initial = eligible
            .iter()
            .position(|m| m.id == self.config.provider.model)
            .or_else(|| eligible.iter().position(|m| m.id == "claude-haiku-5-5"))
            .unwrap_or(0);
        let Some(index) = ui::choose(
            t,
            title,
            "Choose the model.\nThe list came from the provider. A text-only model cleans text with the fast pipeline; it cannot read pictures, and choosing one turns image reading off. Scanned pages are then read by local OCR only. A model whose image support is not reported is tested with a small image before this setup is saved, unless image reading is off.\nRates, where the provider supplies them, are estimates. A missing rate is not treated as free.",
            &labels,
            initial,
        )?
        else {
            return Ok(Flow::Back);
        };
        if index == eligible.len() {
            self.models = None;
            return Ok(Flow::Go(Step::Models));
        }
        catalog::select(&mut self.config, eligible[index])?;
        if eligible[index].vision == Some(false)
            && (self.config.processing.vision || self.config.processing.pipeline != "fast")
        {
            self.config.processing.vision = false;
            self.config.processing.pipeline = "fast".into();
            ui::notice(
                t,
                "Image reading is now off",
                &format!(
                    "{} reports that it does not accept images. The fast pipeline is selected and image reading is off: text is cleaned; pictures are not read by the model, and scanned pages are read by local OCR when an engine is installed. Both settings are under Processing and take effect when Setup is saved.",
                    eligible[index].name
                ),
            )?;
        }
        Ok(Flow::Next)
    }

    fn limit(&mut self, t: &mut ratatui::DefaultTerminal, title: &str) -> Result<Flow> {
        let priced = self.config.provider.input_price_per_million.is_some()
            && self.config.provider.output_price_per_million.is_some();
        if priced {
            let Some(index) = choice(
                t,
                title,
                "Choose a spending limit for each document.\nA dollar limit is computed from the configured token rates and a cautious estimate of each request. It is an estimate and not a guarantee of the invoice. The request and token limits also apply.\nA document that reaches a limit stops and keeps its work. The limit can then be raised for that document.",
                &[
                    "$0.25 estimated limit per document",
                    "$1.00 estimated limit per document",
                    "$5.00 estimated limit per document",
                    "Keep the current limit",
                    "No dollar limit; keep the request and token limits",
                ],
                if self.config.processing.max_cost.is_some() {
                    3
                } else {
                    0
                },
            )?
            else {
                return Ok(Flow::Back);
            };
            match index {
                0 => self.config.processing.max_cost = Some(0.25),
                1 => self.config.processing.max_cost = Some(1.0),
                2 => self.config.processing.max_cost = Some(5.0),
                4 => self.config.processing.max_cost = None,
                _ => {}
            }
        } else {
            self.config.processing.max_cost = None;
            let Some(index) = choice(
                t,
                title,
                "The provider's list gives no rates for this model, so a dollar limit cannot be computed.\nChoose a limit on generated tokens for each document. The request limit also applies.\nA document that reaches a limit stops and keeps its work. The limit can then be raised for that document.",
                &[
                    "100,000 output tokens per document",
                    "1,000,000 output tokens per document",
                    "Keep the current token and request limits",
                ],
                if self.config.processing.max_generated_tokens
                    == Config::default().processing.max_generated_tokens
                {
                    0
                } else {
                    2
                },
            )?
            else {
                return Ok(Flow::Back);
            };
            match index {
                0 => self.config.processing.max_generated_tokens = 100_000,
                1 => self.config.processing.max_generated_tokens = 1_000_000,
                _ => {}
            }
        }
        Ok(Flow::Next)
    }

    fn check(&mut self, t: &mut ratatui::DefaultTerminal, title: &str) -> Result<Flow> {
        if self.config.provider.model.is_empty() {
            return Ok(Flow::Go(Step::Model));
        }
        let (text, labels): (String, Vec<&str>) = match self.verified() {
            Some(check) => (
                format!(
                    "This provider, key and model passed the connection check on {}. Nothing that the check covers has changed, so no request is needed.\n\nA new check is optional. {}",
                    date(check.verified_at),
                    billing(&self.config)
                ),
                vec![
                    "Continue without a new request",
                    "Test the connection again (one billed request)",
                    "Choose a different model",
                ],
            ),
            None => (
                format!(
                    "The provider, key or model is new or has changed, or image reading was turned on after a check that sent no image. The connection must be checked before this setup can be saved.\n\n{}",
                    billing(&self.config)
                ),
                vec![
                    "Run the connection check (one billed request)",
                    "Choose a different model",
                ],
            ),
        };
        const TEXT_ONLY: &str = "Use text only: the fast pipeline with image reading off";
        let mut labels = labels;
        if self.verified().is_none() && !profile::text_only(&self.config) {
            labels.push(TEXT_ONLY);
        }
        let Some(index) = choice(t, title, &text, &labels, 0)? else {
            return Ok(Flow::Back);
        };
        match labels[index] {
            "Continue without a new request" => Ok(Flow::Next),
            "Choose a different model" => Ok(Flow::Go(Step::Model)),
            TEXT_ONLY => {
                // For a model that reads no images. The check then sends a sentence.
                self.config.processing.pipeline = "fast".into();
                self.config.processing.vision = false;
                Ok(Flow::Stay)
            }
            _ => match check_task(t, title, &self.location, &self.config) {
                Ok(ui::Ended::Continue(check)) => {
                    self.checked = Some(check);
                    Ok(Flow::Next)
                }
                Ok(ui::Ended::Back(check)) => {
                    // A check that passed stays passed; Back returns to the question.
                    if check.is_some() {
                        self.checked = check;
                    }
                    Ok(Flow::Stay)
                }
                Err(error) if error.is::<ui::SaveExit>() => Err(error),
                Err(error) => {
                    // The test failed: no check covers this configuration now.
                    // If it is the configuration in force, its record on disk
                    // is withdrawn as well, as the menu's test would do.
                    let fingerprint = profile::provider_fingerprint(&self.config);
                    self.checked = None;
                    if self
                        .stored
                        .provider
                        .as_ref()
                        .is_some_and(|c| c.fingerprint == fingerprint)
                    {
                        self.stored.provider = None;
                        if self.location == self.stored_for
                            && fingerprint == profile::provider_fingerprint(&self.baseline)
                        {
                            self.stored.save(&self.location.root)?;
                        }
                    }
                    Err(error)
                }
            },
        }
    }

    fn activate(&mut self, t: &mut ratatui::DefaultTerminal, title: &str) -> Result<Flow> {
        if !self.skip_ai && self.verified().is_none() {
            return Ok(Flow::Go(Step::Check));
        }
        let key = match &self.config.provider.credential {
            _ if self.skip_ai => "unchanged",
            Some(c) if c.session_only && self.holding_new_key() && self.remember => {
                "will be stored in the system credential store now"
            }
            Some(c) if c.session_only => "kept for this session and its workers; not stored",
            Some(_) => "the key already in the system credential store",
            None if catalog::local(&self.config) => "none; a server on this computer",
            None => "read from the environment variable",
        };
        let ai = if self.skip_ai {
            "AI provider: left for later. Cleaning with AI will ask for it.".to_owned()
        } else {
            format!(
                "Provider: {}\nEndpoint: {}\nModel: {}\nKey: {key}\nConnection check: passed {}",
                provider_name(&self.config),
                self.config.provider.base_url,
                self.config.provider.model,
                self.verified()
                    .map(|c| date(c.verified_at))
                    .unwrap_or_default()
            )
        };
        let body = format!(
            "Save this setup?\n\nWorkspace: {}\n{ai}\n\nNothing has been changed so far. Saving writes the configuration{}. Jobs already queued keep the settings they were queued with.",
            self.location.root.display(),
            if key.starts_with("will be stored") {
                " and stores the key"
            } else {
                ""
            }
        );
        if choice(t, title, &body, &["Save"], 0)?.is_none() {
            return Ok(Flow::Back);
        }
        loop {
            match self.commit() {
                Ok(()) => return Ok(Flow::Done),
                Err(error) if error.is::<ui::SaveExit>() => return Err(error),
                Err(error) => {
                    let storing = self.remember && self.holding_new_key();
                    let mut labels = vec!["Try again"];
                    if storing {
                        labels.push("Keep the key for this session only, and save");
                    }
                    labels.push("Back");
                    let (heading, text) = if error.is::<SavedIncomplete>() {
                        (
                            "The setup was saved, with one record missing",
                            format!(
                                "The configuration, and the key if one was to be stored, are saved and in force. But {error:#}\n\n'Try again' writes the missing record."
                            ),
                        )
                    } else {
                        (
                            "The setup was not saved",
                            format!(
                                "{error:#}\n\nNothing was changed: the earlier configuration and its key are still in force."
                            ),
                        )
                    };
                    let picked = choice(t, heading, &text, &labels, 0)?;
                    match picked.map(|i| labels[i]) {
                        Some("Try again") => {}
                        Some("Keep the key for this session only, and save") => {
                            self.remember = false;
                        }
                        _ => return Ok(Flow::Stay),
                    }
                }
            }
        }
    }

    /// Save what this section is responsible for.
    fn commit(&mut self) -> Result<()> {
        match self.section {
            Section::Workspace => {
                Library::open(&self.location.root)?;
                if !self.location.config.is_file() {
                    self.config.save(&self.location.config)?;
                }
                self.location.activate()?;
            }
            Section::Processing | Section::Output => {
                self.config.save(&self.location.config)?;
            }
            // The engines part has nothing to record: its effect is the
            // engines themselves, and their state is read from the files.
            Section::Engines => {}
            Section::Provider | Section::All => {
                let mut record = self.stored.clone();
                if !self.skip_ai {
                    record.provider = self.verified().cloned();
                }
                let remember = self.remember && self.holding_new_key();
                let saved = profile::activate(
                    &SystemVault,
                    &self.location,
                    &self.config,
                    remember,
                    &record,
                )?;
                // From here the saved configuration is in force.
                self.config = saved;
                self.secret = Zeroizing::new(String::new());
                self.location.activate().map_err(|error| {
                    anyhow::Error::new(SavedIncomplete(format!(
                        "the workspace could not be recorded as the active one: {error:#}"
                    )))
                })?;
            }
        }
        Ok(())
    }

    fn select_workspace(&mut self, t: &mut ratatui::DefaultTerminal, path: PathBuf) -> Result<()> {
        let next = Location::for_root(path);
        if next.root == self.location.root {
            return Ok(());
        }
        if next.config.is_file() {
            let Some(picked) = choice(
                t,
                "Existing workspace",
                "This folder already holds a Librarian configuration. Open it with its own settings, or choose another folder. Its jobs keep the settings they were queued with.",
                &[
                    "Open this workspace with its settings",
                    "Choose another folder",
                ],
                0,
            )?
            else {
                return Ok(());
            };
            if picked != 0 {
                return Ok(());
            }
            // An existing workspace is taken with its own settings, its own
            // key reference and its own record of what was verified.
            let own = Config::load(&next.config)?;
            Verification::carry_forward(&next, &own);
            self.stored = Verification::load(&next, &own);
            self.config = own.clone();
            self.baseline = own;
        } else {
            // A new workspace starts from the settings in force. Its provider
            // has no check on record there, so the first use asks for one.
            self.config = self.baseline.clone();
            self.stored = Verification::default();
        }
        self.stored_for = next.clone();
        self.checked = None;
        self.location = next;
        Ok(())
    }
}

/// Run one section of Setup. Returns the workspace in force when the section
/// was saved, or `None` if it was left without saving.
pub fn run(
    t: &mut ratatui::DefaultTerminal,
    initial: Location,
    section: Section,
) -> Result<Option<Location>> {
    let Some((mut wizard, resume)) = Wizard::open(t, initial, section)? else {
        return Ok(None);
    };
    let steps = section.steps();
    let mut index = resume
        .and_then(|step| steps.iter().position(|s| *s == step))
        .filter(|i| wizard.applies(steps[*i]))
        .or_else(|| wizard.next(steps, None));
    loop {
        let Some(current) = index else {
            // Every step was passed: save the section.
            match wizard.commit() {
                Ok(()) => {
                    wizard.clear_draft();
                    return Ok(Some(wizard.location));
                }
                Err(error) if error.is::<ui::SaveExit>() => return Err(error),
                Err(error) => {
                    ui::notice(
                        t,
                        "The setup was not saved",
                        &format!("{error:#}\n\nNothing was changed."),
                    )?;
                    index = wizard.back(steps, steps.len());
                    if index.is_none() {
                        return Ok(None);
                    }
                    continue;
                }
            }
        };
        let step = steps[current];
        wizard.save_draft(step)?;
        let shown = steps.iter().filter(|s| wizard.applies(**s)).count();
        let position = steps[..=current]
            .iter()
            .filter(|s| wizard.applies(**s))
            .count();
        let title = if steps.len() == 1 {
            format!("Setup · {}", section.name())
        } else {
            format!("Setup · {} · step {position} of {shown}", section.name())
        };
        match wizard.step(t, &title, step) {
            Ok(Flow::Next) => index = wizard.next(steps, Some(current)),
            Ok(Flow::Stay) => {}
            Ok(Flow::Go(target)) => {
                index = steps.iter().position(|s| *s == target).or(index);
            }
            Ok(Flow::Done) => {
                wizard.clear_draft();
                return Ok(Some(wizard.location));
            }
            Ok(Flow::Back) => match wizard.back(steps, current) {
                Some(previous) => index = Some(previous),
                None => {
                    // Left at the first step. A draft is worth keeping only
                    // if something was answered.
                    if !wizard.changed() {
                        wizard.clear_draft();
                    }
                    return Ok(None);
                }
            },
            Err(error) => {
                wizard.save_draft(step)?;
                if error.is::<ui::SaveExit>() {
                    return Err(error);
                }
                let picked = choice(
                    t,
                    "Setup needs attention",
                    &format!(
                        "{error:#}\n\nNothing has been saved. The configuration in force and the queued jobs are unchanged."
                    ),
                    &["Try this step again", "Back"],
                    0,
                )?;
                if picked != Some(0) {
                    match wizard.back(steps, current) {
                        Some(previous) => index = Some(previous),
                        None => return Ok(None),
                    }
                }
            }
        }
    }
}

/// The Setup menu: the state of the workspace, and each section on its own.
/// Returns a workspace when Home must reopen on it.
pub fn menu(t: &mut ratatui::DefaultTerminal, location: &Location) -> Result<Option<Location>> {
    let mut selected = 0;
    loop {
        let config = Config::load(&location.config)?;
        let state = profile::ai_state(location, &config);
        let connected = !matches!(state, Ai::NotConnected) && !config.provider.model.is_empty();
        let body = format!(
            "Workspace       {}\nAI provider     {}\nLocal engines   {}\n\nParsing PDFs needs only the workspace and the PDF engine.\nCleaning documents with AI needs a verified provider. A connection check is one billed request; it is asked for only when the provider, the key or the model changes.",
            location.root.display(),
            ai_line(location, &config),
            engines_line(&location.root)
        );
        let mut entries: Vec<(&str, &str)> = vec![(
            "provider",
            if connected {
                "Change the AI provider, key or model"
            } else {
                "Connect an AI provider"
            },
        )];
        if connected {
            entries.push(("retest", "Test the connection again (one billed request)"));
        }
        entries.extend([
            ("processing", "Processing defaults"),
            ("output", "Markdown output defaults"),
            ("engines", "Install or check the local engines"),
            ("workspace", "Change the workspace"),
            ("all", "Go through every step in order"),
            ("back", "Back to Home"),
        ]);
        let labels: Vec<&str> = entries.iter().map(|(_, l)| *l).collect();
        let Some(index) = choice(t, "Setup", &body, &labels, selected)? else {
            return Ok(None);
        };
        selected = index;
        let section = match entries[index].0 {
            "provider" => Section::Provider,
            "processing" => Section::Processing,
            "output" => Section::Output,
            "engines" => Section::Engines,
            "workspace" => Section::Workspace,
            "all" => Section::All,
            "retest" => {
                retest(t, location, &config)?;
                continue;
            }
            _ => return Ok(None),
        };
        if let Some(saved) = run(t, location.clone(), section)?
            && saved != *location
        {
            return Ok(Some(saved));
        }
    }
}

/// The manual connection check. A pass is recorded; a failure withdraws the
/// record, because access is then not established.
fn retest(t: &mut ratatui::DefaultTerminal, location: &Location, config: &Config) -> Result<()> {
    let text = format!(
        "Test the connection to {} with the model {}?\n\n{}",
        provider_name(config),
        config.provider.model,
        billing(config)
    );
    if choice(
        t,
        "Test the connection",
        &text,
        &["Send one billed request", "Back"],
        0,
    )? != Some(0)
    {
        return Ok(());
    }
    let mut record = Verification::load(location, config);
    match check_task(t, "Test the connection", location, config) {
        Ok(ui::Ended::Continue(check) | ui::Ended::Back(Some(check))) => {
            record.provider = Some(check);
            record.save(&location.root)?;
        }
        Ok(ui::Ended::Back(None)) => {}
        Err(error) if error.is::<ui::SaveExit>() => return Err(error),
        Err(error) => {
            record.provider = None;
            record.save(&location.root)?;
            ui::notice(
                t,
                "The connection check failed",
                &format!(
                    "{error:#}\n\nThe provider is now recorded as not verified. Work that calls a model will ask for a successful check first."
                ),
            )?;
        }
    }
    Ok(())
}

/// Before work that calls a model: if the provider is not ready, say why and
/// offer to set it up now. Returns whether the work may proceed.
pub fn ensure_ai(
    t: &mut ratatui::DefaultTerminal,
    location: &Location,
    config: &Config,
) -> Result<bool> {
    let reason = match profile::ai_state(location, config) {
        Ai::Ready | Ai::NotNeeded => return Ok(true),
        Ai::NotConnected => {
            "No AI provider is connected yet. This work sends document text, and with image reading on also images, to a language model, so it needs a provider, a key and a model."
        }
        Ai::KeyMissing => {
            "The key for this provider was kept for one session only and has not been entered in this session."
        }
        Ai::Unverified => {
            "The provider, key or model has changed since the last successful connection check."
        }
    };
    let text = format!(
        "{reason}\n\nConnecting takes a few steps and ends with one billed test request. Parsing PDFs and extraction-only imports need none of this."
    );
    if choice(
        t,
        "An AI provider is needed",
        &text,
        &["Connect the AI provider now", "Back"],
        0,
    )? != Some(0)
    {
        return Ok(false);
    }
    if run(t, location.clone(), Section::Provider)?.is_none() {
        return Ok(false);
    }
    let config = Config::load(&location.config)?;
    Ok(matches!(
        profile::ai_state(location, &config),
        Ai::Ready | Ai::NotNeeded
    ))
}

fn ocr_label(config: &Config) -> String {
    match config.processing.ocr.as_str() {
        "paddle" => "OCR: bundled (English, Chinese and Japanese, recognized automatically)".into(),
        "tesseract" => "OCR: Tesseract (installed separately)".into(),
        "vision" => "OCR: none; the model reads the page images".into(),
        _ => "OCR: off".into(),
    }
}

/// Choose a Tesseract language from those installed. Returns false if the
/// owner went back.
fn tesseract_language(
    t: &mut ratatui::DefaultTerminal,
    config: &mut Config,
    installed: &[String],
) -> Result<bool> {
    let mut labels: Vec<String> = installed.to_vec();
    labels.push("Combine several languages…".into());
    let current = ocr::tesseract_language(&config.processing.ocr_language, installed).ok();
    let initial = current
        .as_ref()
        .and_then(|c| installed.iter().position(|i| i == c))
        .unwrap_or(0);
    let Some(index) = ui::choose(
        t,
        "Tesseract language",
        "These are the languages Tesseract has data for on this computer. To use another, install its language data first.",
        &labels,
        initial,
    )?
    else {
        return Ok(false);
    };
    if index < installed.len() {
        config.processing.ocr_language = installed[index].clone();
        return Ok(true);
    }
    let Some(value) = ui::input(
        t,
        "Tesseract languages",
        &format!(
            "Join installed languages with +, for example eng+deu. Installed: {}",
            installed.join(", ")
        ),
        current.as_deref().unwrap_or(""),
        false,
    )?
    else {
        return Ok(false);
    };
    config.processing.ocr_language = ocr::tesseract_language(value.trim(), installed)?;
    Ok(true)
}

fn preferences(
    t: &mut ratatui::DefaultTerminal,
    config: &mut Config,
    output: bool,
    finish: &str,
) -> Result<bool> {
    let mut selected: Option<&str> = Some("finish");
    loop {
        let check = |yes| if yes { "[x]" } else { "[ ]" };
        let rows: Vec<(&str, String)> = if output {
            vec![
                (
                    "frontmatter",
                    format!("{} Metadata front matter", check(config.output.frontmatter)),
                ),
                (
                    "title",
                    format!("{} Document title", check(config.output.title)),
                ),
                (
                    "summary",
                    format!("{} Synopsis", check(config.output.summary)),
                ),
                (
                    "page_markers",
                    format!("{} Source page markers", check(config.output.page_markers)),
                ),
                (
                    "review_notes",
                    format!("{} Review notes", check(config.output.review_notes)),
                ),
                ("filename", format!("File name: {}", config.output.filename)),
                ("finish", finish.into()),
            ]
        } else {
            let mut rows = vec![
                (
                    "edit",
                    format!("{} Copyedit prose", check(config.processing.edit)),
                ),
                (
                    "classify",
                    format!(
                        "{} Classify and describe documents",
                        check(config.processing.classify)
                    ),
                ),
                (
                    "vision",
                    format!(
                        "{} {}",
                        check(config.processing.vision),
                        if config.processing.pipeline == "fast" {
                            "Read pictures and image files with the model"
                        } else {
                            "Visual reading of every page"
                        }
                    ),
                ),
                ("ocr", ocr_label(config)),
            ];
            if config.processing.ocr == "tesseract" {
                rows.push((
                    "ocr_language",
                    format!("Tesseract language: {}", config.processing.ocr_language),
                ));
            }
            rows.extend([
                ("house_style", "House style…".to_string()),
                (
                    "pipeline",
                    format!("Pipeline: {}", config.processing.pipeline),
                ),
                (
                    "reasoning_effort",
                    format!(
                        "Reasoning effort: {}",
                        if config.provider.reasoning_effort.is_empty() {
                            "not sent; the model's default applies"
                        } else {
                            &config.provider.reasoning_effort
                        }
                    ),
                ),
                (
                    "page_concurrency",
                    format!("Pages read at once: {}", config.processing.page_concurrency),
                ),
                (
                    "classification_override",
                    format!(
                        "Class override: {}",
                        config
                            .processing
                            .classification_override
                            .as_deref()
                            .unwrap_or("none; the model classifies")
                    ),
                ),
                (
                    "office_renderer",
                    format!(
                        "Office renderer: {}",
                        config
                            .processing
                            .office_renderer
                            .as_ref()
                            .map(|p| p.display().to_string())
                            .unwrap_or_else(|| "the one supplied or installed".into())
                    ),
                ),
                ("finish", finish.into()),
            ]);
            // The fast pipeline reads no page images, so it has no pages to read at once.
            rows.retain(|(key, _)| {
                !(config.processing.pipeline == "fast" && *key == "page_concurrency")
            });
            rows
        };
        let labels: Vec<String> = rows.iter().map(|(_, l)| l.clone()).collect();
        let initial = selected
            .and_then(|key| rows.iter().position(|(k, _)| *k == key))
            .unwrap_or(rows.len() - 1);
        let Some(index) = ui::choose(
            t,
            if output {
                "Markdown output defaults"
            } else {
                "Processing defaults"
            },
            "Enter or Space switches a box or opens a value. These defaults apply to documents queued from now on. They do not call for a new connection check, with one exception: turning image reading on after a check that sent no image.",
            &labels,
            initial,
        )?
        else {
            return Ok(false);
        };
        let key = rows[index].0;
        selected = Some(key);
        match key {
            "finish" => {
                config.validate()?;
                return Ok(true);
            }
            "frontmatter" => config.output.frontmatter = !config.output.frontmatter,
            "title" => config.output.title = !config.output.title,
            "summary" => config.output.summary = !config.output.summary,
            "page_markers" => config.output.page_markers = !config.output.page_markers,
            "review_notes" => config.output.review_notes = !config.output.review_notes,
            "filename" => {
                if let Some(value) = ui::input(
                    t,
                    "File name pattern",
                    "Placeholders: {class}, {title}, {number}, {sequence}, {id}.",
                    &config.output.filename,
                    false,
                )? {
                    let previous =
                        std::mem::replace(&mut config.output.filename, value.to_string());
                    if let Err(error) = config.validate() {
                        config.output.filename = previous;
                        ui::notice(t, "That pattern cannot be used", &format!("{error:#}"))?;
                    }
                }
            }
            "edit" => config.processing.edit = !config.processing.edit,
            "classify" => config.processing.classify = !config.processing.classify,
            "vision" => config.processing.vision = !config.processing.vision,
            "ocr" => {
                let engines = ["paddle", "tesseract", "vision", "off"];
                if let Some(i) = choice(
                    t,
                    "OCR engine",
                    "The bundled engine recognizes English, Chinese and Japanese without a language setting. Tesseract is a separate program; its languages are those installed on this computer. With no OCR, the model reads the page images, and that reading is billed by the provider.",
                    &[
                        "Bundled · English, Chinese and Japanese, automatic",
                        "Tesseract · installed separately",
                        "None · the model reads the page images",
                        "Off",
                    ],
                    engines
                        .iter()
                        .position(|e| *e == config.processing.ocr)
                        .unwrap_or(0),
                )? {
                    if engines[i] == "tesseract" {
                        match ocr::tesseract_languages() {
                            Ok(installed) => {
                                let keep = ocr::tesseract_language(
                                    &config.processing.ocr_language,
                                    &installed,
                                );
                                match keep {
                                    Ok(language) => {
                                        config.processing.ocr_language = language;
                                        config.processing.ocr = "tesseract".into();
                                    }
                                    Err(_) => {
                                        if tesseract_language(t, config, &installed)? {
                                            config.processing.ocr = "tesseract".into();
                                        }
                                    }
                                }
                            }
                            Err(error) => ui::notice(
                                t,
                                "Tesseract is not available",
                                &format!("{error:#}\n\nThe OCR engine was not changed."),
                            )?,
                        }
                    } else {
                        config.processing.ocr = engines[i].into();
                    }
                }
            }
            "ocr_language" => match ocr::tesseract_languages() {
                Ok(installed) => {
                    if let Err(error) = tesseract_language(t, config, &installed) {
                        if error.is::<ui::SaveExit>() {
                            return Err(error);
                        }
                        ui::notice(t, "That language cannot be used", &format!("{error:#}"))?;
                    }
                }
                Err(error) => ui::notice(t, "Tesseract is not available", &format!("{error:#}"))?,
            },
            "house_style" => {
                if let Some(v) = ui::input(
                    t,
                    "House style",
                    "Preferences for spelling, punctuation, register and formatting. They are added to the fixed instructions of the fast pipeline and to the Copyeditor and Reviewer roles of the editorial pipeline.",
                    &config.processing.house_style,
                    false,
                )? {
                    config.processing.house_style = v.to_string();
                }
            }
            "pipeline" => {
                if let Some(i) = choice(
                    t,
                    "Processing pipeline",
                    "Fast parses the document locally, cuts the text into passages and has the model repair all of them side by side; every response is accounted for word by word. Scanned PDF pages are read by local OCR when an engine is installed.\nEditorial renders every page to an image, reads it with OCR and the model, reconciles the readings and reviews the assembled document. It is slower and sends many more requests.\nArchival is kept so that older jobs can resume.",
                    &["Fast", "Editorial", "Archival"],
                    match config.processing.pipeline.as_str() {
                        "fast" => 0,
                        "editorial" => 1,
                        _ => 2,
                    },
                )? {
                    config.processing.pipeline = ["fast", "editorial", "archival"][i].into();
                }
            }
            "reasoning_effort" => {
                if let Some(v) = ui::input(
                    t,
                    "Reasoning effort",
                    "One lower-case word sent with every request to a chat or responses endpoint, such as none, low, medium or high. Leave empty to send nothing. The accepted words depend on the model; a word the model does not accept makes every request fail with HTTP 400. Less reasoning is faster and costs less. It is set when a model is chosen and may be changed here.",
                    &config.provider.reasoning_effort,
                    false,
                )? {
                    let word = v.trim().to_lowercase();
                    if word.len() <= 16 && word.chars().all(|c| c.is_ascii_lowercase()) {
                        config.provider.reasoning_effort = word;
                    } else {
                        ui::notice(
                            t,
                            "That value cannot be used",
                            "Enter one lower-case word, or nothing. The value was not changed.",
                        )?;
                    }
                }
            }
            "page_concurrency" => {
                if let Some(v) = ui::input(
                    t,
                    "Pages read at once",
                    "A whole number from 1 to 16. The provider's own limits still apply.",
                    &config.processing.page_concurrency.to_string(),
                    false,
                )? {
                    match v.trim().parse::<usize>() {
                        Ok(n) if (1..=16).contains(&n) => config.processing.page_concurrency = n,
                        _ => ui::notice(
                            t,
                            "That value cannot be used",
                            "Enter a whole number from 1 to 16. The value was not changed.",
                        )?,
                    }
                }
            }
            "classification_override" => {
                if let Some(v) = ui::input(
                    t,
                    "Class override",
                    "A class code given to every document in place of the model's. Leave empty for none. It applies to the editorial pipeline.",
                    config
                        .processing
                        .classification_override
                        .as_deref()
                        .unwrap_or(""),
                    false,
                )? {
                    config.processing.classification_override =
                        (!v.trim().is_empty()).then(|| v.trim().to_owned());
                }
            }
            "office_renderer" => {
                if let Some(v) = ui::input(
                    t,
                    "Office renderer",
                    "Path of the LibreOffice soffice program. Leave empty to use the one supplied with the package or installed on this computer.",
                    &config
                        .processing
                        .office_renderer
                        .as_ref()
                        .map(|p| p.display().to_string())
                        .unwrap_or_default(),
                    false,
                )? {
                    config.processing.office_renderer =
                        (!v.trim().is_empty()).then(|| PathBuf::from(v.trim()));
                }
            }
            _ => {}
        }
    }
}

fn open_link(url: &str) -> Result<()> {
    if !url.starts_with("https://") {
        bail!("Only an HTTPS provider page can be opened");
    }
    #[cfg(target_os = "macos")]
    let mut command = Command::new("open");
    #[cfg(target_os = "windows")]
    let mut command = {
        let mut c = Command::new("rundll32.exe");
        c.arg("url.dll,FileProtocolHandler");
        c
    };
    #[cfg(not(any(target_os = "macos", target_os = "windows")))]
    let mut command = Command::new("xdg-open");
    command
        .arg(url)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .context("The browser could not be opened. Use the address shown on the screen.")?;
    Ok(())
}
