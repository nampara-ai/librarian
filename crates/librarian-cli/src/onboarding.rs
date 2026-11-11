use crate::{setup::expand_path, ui};
use anyhow::{Context, Result, bail};
use librarian_native::{
    assets, catalog,
    config::Config,
    credentials,
    profile::{self, Location},
    store::Library,
};
use serde::{Deserialize, Serialize};
use std::{
    io::Read,
    process::{Command, Stdio},
    sync::atomic::Ordering,
    time::Duration,
};
use zeroize::Zeroizing;

#[derive(Serialize, Deserialize)]
struct Draft {
    location: Location,
    config: Config,
    step: usize,
}

pub fn run(terminal: &mut ratatui::DefaultTerminal, initial: Location) -> Result<Option<Location>> {
    let draft_path = initial.root.join("setup-draft.json");
    let mut config = Config::load(&initial.config)?;
    if !initial.config.is_file() {
        catalog::providers()[0].apply(&mut config);
    }
    let mut draft = Draft {
        config,
        location: initial,
        step: 0,
    };
    if let Ok(bytes) = std::fs::read(&draft_path)
        && let Ok(saved) = serde_json::from_slice::<Draft>(&bytes)
    {
        match ui::choose(
            terminal,
            "Resume setup",
            "An unfinished setup is saved. Keys are stored separately from this draft.",
            &[
                "Resume saved setup".into(),
                "Start from current configuration".into(),
            ],
            0,
        )? {
            Some(0) => {
                draft = saved;
                draft.step = draft.step.min(4);
            }
            None => return Ok(None),
            _ => {}
        }
    }
    // Old profiles predate catalog IDs. Recognize their actual endpoint.
    if let Some(preset) = catalog::providers().into_iter().find(|p| {
        p.base_url.trim_end_matches('/') == draft.config.provider.base_url.trim_end_matches('/')
    }) {
        draft.config.provider.catalog_id = preset.id;
    }
    let mut secret = Zeroizing::new(String::new());
    let mut models = Vec::<catalog::Model>::new();
    let mut model_verified = false;
    let mut engine_verified = false;
    loop {
        // This draft contains only a credential reference, never the key value.
        // Pending key entry always resumes at the key screen after a restart.
        let saved_step = draft.step;
        if draft.step == 5 {
            draft.step = 4;
        }
        profile::write_json(&draft_path, &draft)?;
        draft.step = saved_step;
        let title = format!("Set up environment · Step {} of 14", draft.step + 1);
        let outcome = step(
            terminal,
            &title,
            &mut draft,
            &mut secret,
            &mut models,
            &mut model_verified,
            &mut engine_verified,
        );
        match outcome {
            Ok(Action::Next) => draft.step += 1,
            Ok(Action::Back) => {
                if draft.step == 0 {
                    return Ok(None);
                }
                draft.step -= 1;
            }
            Ok(Action::Go(index)) => draft.step = index,
            Ok(Action::Done) => {
                // Remove only our non-secret draft after committing the profile.
                if draft_path.is_file() {
                    std::fs::remove_file(&draft_path)?;
                }
                return Ok(Some(draft.location));
            }
            Err(error) => {
                // Save accepted settings even when Ctrl+S leaves a nested editor.
                let saved_step = draft.step;
                if draft.step == 5 {
                    draft.step = 4;
                }
                profile::write_json(&draft_path, &draft)?;
                draft.step = saved_step;
                if error.is::<ui::SaveExit>() {
                    return Err(error);
                }
                let choice = ui::choose(
                    terminal,
                    "Setup needs attention",
                    &format!(
                        "{error:#}\n\nSetup has not completed. Your queued jobs are unchanged."
                    ),
                    &["Retry this step".into(), "Back to previous step".into()],
                    0,
                )?;
                if choice != Some(0) {
                    if draft.step == 0 {
                        return Ok(None);
                    }
                    draft.step -= 1;
                }
            }
        }
    }
}
enum Action {
    Next,
    Back,
    Go(usize),
    Done,
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

#[allow(clippy::too_many_arguments)]
fn step(
    t: &mut ratatui::DefaultTerminal,
    title: &str,
    d: &mut Draft,
    secret: &mut Zeroizing<String>,
    models: &mut Vec<catalog::Model>,
    model_verified: &mut bool,
    engine_verified: &mut bool,
) -> Result<Action> {
    match d.step {
        0 => {
            let message = "Librarian will prepare a workspace, connect your model provider, and check its PDF/OCR engines.\n\nNo Python environment, shell activation, or export commands are needed.\nArrow keys or Tab move. Enter/Space choose. Esc always goes back.\nAutomatic steps show their progress; x continues after completion.";
            return Ok(
                if choice(t, title, message, &["Set up Librarian"], 0)?.is_some() {
                    Action::Next
                } else {
                    Action::Back
                },
            );
        }
        1 => {
            let text = format!(
                "Where should Librarian keep its originals, jobs, caches and search index?\n\n{}\n\nProcessed Markdown destinations are chosen for each import. This workspace becomes the active profile on future launches.",
                d.location.root.display()
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
                Some(0) => return Ok(Action::Next),
                Some(1) => {
                    if let Some(path) = ui::browse(t, &d.location.root, true)? {
                        select_workspace(t, d, path)?;
                        *engine_verified = false;
                        *model_verified = false;
                    }
                }
                Some(2) => {
                    if let Some(path) = ui::input(
                        t,
                        title,
                        "Workspace folder (created if missing).",
                        &d.location.root.to_string_lossy(),
                        false,
                    )? {
                        let path = expand_path(&path);
                        if path.as_os_str().is_empty() {
                            bail!("Choose a workspace folder");
                        }
                        select_workspace(
                            t,
                            d,
                            if path.is_absolute() {
                                path
                            } else {
                                std::env::current_dir()?.join(path)
                            },
                        )?;
                        *engine_verified = false;
                        *model_verified = false;
                    }
                }
                None => return Ok(Action::Back),
                _ => {}
            }
            return Ok(Action::Go(1));
        }
        2 => {
            let location = d.location.clone();
            let result = ui::task(
                t,
                title,
                "Preparing local storage. No source documents are imported by setup.",
                move |progress, _| {
                    let library = Library::open(&location.root)?;
                    let probe = tempfile::NamedTempFile::new_in(&library.root)?;
                    probe.as_file().sync_all()?;
                    let _ = progress.send(
                        "Workspace, SQLite journal, cache and writable storage are ready".into(),
                    );
                    Ok(library.root)
                },
            )?;
            if let Some(root) = result {
                d.location.root = root;
                return Ok(Action::Next);
            }
            return Ok(Action::Back);
        }
        3 => {
            let presets = catalog::providers();
            let labels = presets
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
            let initial = presets
                .iter()
                .position(|p| p.id == d.config.provider.catalog_id)
                .unwrap_or(0);
            let desc = format!(
                "Choose a provider. Its endpoint is filled in automatically.\nSorted by best listed model's Vision Arena score, {}. Overlapping scores are not a universal quality guarantee.\nGateways and providers without comparable scores are unranked.\nSource: {}",
                catalog::RANK_DATE,
                catalog::RANK_SOURCE
            );
            let Some(index) = ui::choose(t, title, &desc, &labels, initial)? else {
                return Ok(Action::Back);
            };
            let preset = &presets[index];
            if preset.id != d.config.provider.catalog_id
                || preset.base_url != d.config.provider.base_url
            {
                preset.apply(&mut d.config);
                *model_verified = false;
                models.clear();
                secret.clear();
            }
            if preset.id == "custom" {
                let Some(url) = ui::input(
                    t,
                    "Custom endpoint",
                    "Enter the API base URL. HTTPS is required except for localhost.",
                    &d.config.provider.base_url,
                    false,
                )?
                else {
                    return Ok(Action::Go(3));
                };
                if url.as_str() != d.config.provider.base_url {
                    d.config.provider.credential = None;
                    d.config.provider.base_url = url.to_string();
                }
                let modes = [
                    "OpenAI-compatible Chat Completions",
                    "OpenAI Responses",
                    "Anthropic Messages",
                ];
                let Some(mode) = choice(
                    t,
                    "Custom API protocol",
                    "Choose the protocol implemented by this endpoint.",
                    &modes,
                    0,
                )?
                else {
                    return Ok(Action::Go(3));
                };
                d.config.provider.kind = ["chat", "responses", "anthropic"][mode].into();
                d.config.provider.prompt_cache = if mode == 2 { "ephemeral" } else { "off" }.into();
            }
            d.config.validate()?;
        }
        4 => {
            let p = &d.config.provider;
            let preset = catalog::providers()
                .into_iter()
                .find(|v| v.id == p.catalog_id);
            let help = preset.as_ref().map(|p| p.key_url.as_str()).unwrap_or("");
            let mut labels = vec!["Paste a new API key".to_string()];
            let saved = p
                .credential
                .as_ref()
                .is_some_and(|r| !r.session_only || credentials::resolve(p).is_ok());
            if saved {
                labels.push("Use the saved credential".into());
            }
            let environment = !p.key_env.is_empty() && std::env::var_os(&p.key_env).is_some();
            if environment {
                labels.push(format!("Import key from {}", p.key_env));
            }
            if catalog::local(&d.config) {
                labels.push("This local server needs no key".into());
            }
            if !help.is_empty() {
                labels.push("Open the provider's API-key page".into());
            }
            let desc = format!(
                "Connect your provider at:\n{}\n\nPaste the key into the masked field. It won't be written to config, job records, logs, or command-line arguments.\nIf you don't have a key, use the provider's key page to create one and enable API access/billing there.\n{}",
                p.base_url, help
            );
            let Some(index) = ui::choose(t, title, &desc, &labels, usize::from(saved))? else {
                return Ok(Action::Back);
            };
            match labels[index].as_str() {
                "Use the saved credential" => {
                    let provider = d.config.provider.clone();
                    if ui::task(t,title,"Loading the saved credential. Your OS may ask you to unlock its credential store.",move|progress,_|{credentials::resolve(&provider)?.context("Saved credential is unavailable")?;let _=progress.send("Saved credential loaded".into());Ok(())})?.is_some(){return Ok(Action::Go(6));}
                }
                "This local server needs no key" => {
                    d.config.provider.credential = None;
                    d.config.provider.key_env.clear();
                    return Ok(Action::Go(6));
                }
                "Open the provider's API-key page" => {
                    open_link(help)?;
                }
                "Paste a new API key" => {
                    if let Some(value) = ui::input(
                        t,
                        title,
                        "Paste your API key here. Enter accepts it; Esc returns without saving.",
                        secret,
                        true,
                    )? {
                        if value.trim().is_empty() {
                            bail!("Enter a key or go back to choose another authentication option");
                        }
                        *secret = Zeroizing::new(value.trim().to_owned());
                        return Ok(Action::Next);
                    }
                }
                _ => {
                    *secret = Zeroizing::new(
                        std::env::var(&p.key_env)
                            .context("Environment credential is no longer available")?,
                    );
                    return Ok(Action::Next);
                }
            }
            return Ok(Action::Go(4));
        }
        5 => {
            if secret.is_empty() {
                return Ok(Action::Go(4));
            }
            let previous = d
                .config
                .provider
                .credential
                .clone()
                .filter(|r| r.session_only);
            let mut options = vec![
                "Remember securely on this device".to_string(),
                "New session only".to_string(),
            ];
            if previous.is_some() {
                options
                    .push("Reconnect the previous session key, including its queued jobs".into());
            }
            let Some(index) = ui::choose(
                t,
                title,
                "Remember this key on this device?\n\nThe system credential store uses Keychain on macOS, Credential Manager on Windows, and Secret Service on Linux. Existing entries are never deleted.\nIf a secure store is unavailable or locked, use a session key. Reconnecting a previous session makes the entered key available to jobs already queued under that reference.",
                &options,
                if previous.is_some() { 2 } else { 0 },
            )?
            else {
                return Ok(Action::Back);
            };
            let endpoint = d.config.provider.base_url.clone();
            let key = secret.clone();
            if let Some(reference) = ui::task(
                t,
                title,
                "Saving the credential separately from your runtime configuration.",
                move |progress, _| {
                    let reference = if index == 2 {
                        credentials::reconnect_session(
                            previous.as_ref().context("No prior session credential")?,
                            &endpoint,
                            &key,
                        )?
                    } else {
                        credentials::save(&endpoint, &key, index == 0)?
                    };
                    let _ = progress.send(
                        if index == 0 {
                            "Credential saved and read back from the system store"
                        } else {
                            "Credential loaded for this session and its background workers"
                        }
                        .into(),
                    );
                    Ok(reference)
                },
            )? {
                d.config.provider.namespace = reference.account.clone();
                d.config.provider.credential = Some(reference);
                *model_verified = false;
                secret.clear();
            } else {
                return Ok(Action::Back);
            }
        }
        6 => {
            let config = d.config.clone();
            let result = ui::task(
                t,
                title,
                "Authenticating and retrieving the provider's live model catalog. No model IDs need to be typed.",
                move |progress, _| {
                    let models =
                        tokio::runtime::Runtime::new()?.block_on(catalog::discover(&config))?;
                    let _ = progress.send(format!("Retrieved {} live model entries", models.len()));
                    Ok(models)
                },
            )?;
            if let Some(found) = result {
                *models = found;
            } else {
                return Ok(Action::Go(4));
            }
        }
        7 => {
            if models.is_empty() {
                return Ok(Action::Go(6));
            }
            let eligible = models
                .iter()
                .filter(|m| m.vision != Some(false))
                .collect::<Vec<_>>();
            if eligible.is_empty() {
                bail!("No image-capable models were reported. Go back to choose another provider.");
            }
            let mut labels = eligible
                .iter()
                .map(|m| {
                    format!(
                        "{}  · {}{}",
                        m.name,
                        if m.vision == Some(true) {
                            "image input reported"
                        } else {
                            "image support unverified"
                        },
                        m.input_price
                            .zip(m.output_price)
                            .map(|(i, o)| format!(" · ${i}/{o} per million in/out tokens"))
                            .unwrap_or_default()
                    )
                })
                .collect::<Vec<_>>();
            labels.push("Refresh the live catalog".into());
            let initial = eligible
                .iter()
                .position(|m| m.id == d.config.provider.model)
                .or_else(|| eligible.iter().position(|m| m.id == "claude-haiku-5-5"))
                .unwrap_or(0);
            let Some(index) = ui::choose(
                t,
                title,
                "Choose the model for text and vision.\nThis list came from your provider. Unknown image capabilities will be checked with a small image before setup finishes. Models explicitly reporting no image input are excluded.\nRates, where supplied, are estimates; a missing price is never treated as free.",
                &labels,
                initial,
            )?
            else {
                return Ok(Action::Go(4));
            };
            if index == eligible.len() {
                return Ok(Action::Go(6));
            }
            catalog::select(&mut d.config, eligible[index])?;
            *model_verified = false;
        }
        8 => {
            let priced = d.config.provider.input_price_per_million.is_some()
                && d.config.provider.output_price_per_million.is_some();
            if priced {
                let labels = [
                    "$0.25 estimated limit per document",
                    "$1.00 estimated limit per document",
                    "$5.00 estimated limit per document",
                    "Keep current limit",
                    "No dollar ceiling; retain token/request limits",
                ];
                let Some(index) = choice(
                    t,
                    title,
                    "Choose a spending limit for each document.\nDollar limits use configured token prices and conservative request reservations; they are estimates rather than an invoice guarantee. Your token and request limits also apply.",
                    &labels,
                    if d.config.processing.max_cost.is_some() {
                        3
                    } else {
                        0
                    },
                )?
                else {
                    return Ok(Action::Back);
                };
                match index {
                    0 => d.config.processing.max_cost = Some(0.25),
                    1 => d.config.processing.max_cost = Some(1.0),
                    2 => d.config.processing.max_cost = Some(5.0),
                    4 => d.config.processing.max_cost = None,
                    _ => {}
                }
            } else {
                d.config.processing.max_cost = None;
                let Some(index) = choice(
                    t,
                    title,
                    "This catalog does not supply verified prices for the selected model, so dollar estimates are unavailable.\nChoose a generated-token ceiling per document. The existing request-count limit also remains active.",
                    &[
                        "100,000 output tokens per document",
                        "1,000,000 output tokens per document",
                        "Keep current token/request limits",
                    ],
                    if d.config.processing.max_generated_tokens
                        == Config::default().processing.max_generated_tokens
                    {
                        0
                    } else {
                        2
                    },
                )?
                else {
                    return Ok(Action::Back);
                };
                match index {
                    0 => d.config.processing.max_generated_tokens = 100_000,
                    1 => d.config.processing.max_generated_tokens = 1_000_000,
                    _ => {}
                }
            }
        }
        9 => {
            if !preferences(t, &mut d.config, false)? {
                return Ok(Action::Back);
            }
        }
        10 => {
            if !preferences(t, &mut d.config, true)? {
                return Ok(Action::Back);
            }
        }
        11 => {
            let root = d.location.root.clone();
            let external_ocr = d.config.processing.ocr == "tesseract";
            if ui::task(t,title,"Preparing PDF and OCR engines. Bundled engines are reused; missing engines are downloaded with pinned checksum verification. No Python, Node or Homebrew installation is needed.",move|progress,cancel|{
                let engines=root.join("engines");
                if assets::load_engines(&engines).is_err(){
                    let _=progress.send("Installing verified engine assets…".into());
                    tokio::runtime::Runtime::new()?.block_on(async {
                        let operation=assets::install(&engines,|name,done,total|{let _=progress.send(format!("{name}: {} / {} MiB",done/1048576,total.unwrap_or(0)/1048576));});
                        tokio::pin!(operation);
                        loop {tokio::select! {
                            result=&mut operation=>break result,
                            _=tokio::time::sleep(Duration::from_millis(100))=>if cancel.load(Ordering::Relaxed){bail!("Engine preparation cancelled");},
                        }}
                    })?;
                    assets::load_engines(&engines)?;
                }
                if external_ocr {
                    let status=Command::new("tesseract").arg("--version").stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).status().context("Tesseract was selected but its executable could not run. Select the bundled Paddle engine or install Tesseract.")?;
                    if !status.success(){bail!("Tesseract did not start successfully; choose Paddle or repair the external installation");}
                }
                let _=progress.send("PDFium and both OCR model sessions initialized".into());Ok(())
            })?.is_none(){return Ok(Action::Back);}
            *engine_verified = true;
        }
        12 => {
            let Some(choice) = choice(
                t,
                title,
                "Check this profile in a fresh worker before saving?\n\nThe worker will load its own saved credential and send a small built-in image to the selected model. This uses API billing, with at most 1,024 generated tokens and one request. No user documents are sent.\nIt checks connectivity, image input and structured responses—not document quality.",
                &[
                    "Run the connection and vision check",
                    "Choose a different model",
                ],
                0,
            )?
            else {
                return Ok(Action::Back);
            };
            if choice == 1 {
                return Ok(Action::Go(7));
            }
            d.config.validate()?;
            let location = d.location.clone();
            let config = d.config.clone();
            let result = ui::task(
                t,
                title,
                "Checking that a fresh worker can load and use the selected runtime profile. Cancelling stops the local check; a request already sent may still be billed by the provider.",
                move |progress, cancel| {
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
                        .context("Cannot start the profile verification worker")?;
                    let started = std::time::Instant::now();
                    let status = loop {
                        if cancel.load(Ordering::Relaxed)
                            || started.elapsed()
                                > Duration::from_secs(config.provider.timeout_seconds + 30)
                        {
                            let _ = child.kill();
                            let _ = child.wait();
                            bail!("Connection check cancelled or timed out");
                        }
                        if let Some(status) = child.try_wait()? {
                            break status;
                        }
                        std::thread::sleep(Duration::from_millis(100));
                    };
                    let mut text = String::new();
                    log.reopen()?.take(65536).read_to_string(&mut text)?;
                    let report: serde_json::Value = serde_json::from_str(&text)
                        .context("Verification worker exited without a readable result")?;
                    if !status.success() {
                        bail!(
                            "{}",
                            report["error"]
                                .as_str()
                                .unwrap_or("Connection verification failed")
                        );
                    }
                    let _=progress.send(format!("Fresh worker authenticated; image and JSON response verified. {} input / {} output tokens.",report["input_tokens"],report["output_tokens"]));
                    Ok(())
                },
            )?;
            if result.is_none() {
                return Ok(Action::Go(7));
            }
            *model_verified = true;
        }
        13 => {
            if !*model_verified {
                return Ok(Action::Go(12));
            }
            if !*engine_verified {
                return Ok(Action::Go(11));
            }
            let body = format!(
                "Ready to activate this runtime profile:\n\nWorkspace: {}\nProvider: {}\nEndpoint: {}\nText + vision model: {}\nCredential: {}\nPDF/OCR engines: initialized\nFresh-worker vision/API check: passed\n\nAll CLI, TUI and background workers will load this profile. Existing queued jobs retain their saved settings; they are not silently rewritten.",
                d.location.root.display(),
                d.config.provider.catalog_id,
                d.config.provider.base_url,
                d.config.provider.model,
                if d.config
                    .provider
                    .credential
                    .as_ref()
                    .is_some_and(|r| r.session_only)
                {
                    "this session and its workers"
                } else if d.config.provider.credential.is_some() {
                    "system credential store"
                } else {
                    "local server without key"
                }
            );
            if choice(t, title, &body, &["Save profile and open Home"], 0)?.is_none() {
                return Ok(Action::Back);
            }
            d.config.save(&d.location.config)?;
            profile::write_json(
                &d.location.root.join("setup-complete.json"),
                &serde_json::json!({"version":1,"model":d.config.provider.model,"verified_at":librarian_native::store::now(),"config_path":d.location.config,"configuration":profile::verification_key(&d.config)?}),
            )?;
            d.location.activate()?;
            return Ok(Action::Done);
        }
        _ => unreachable!(),
    }
    Ok(Action::Next)
}

fn preferences(
    t: &mut ratatui::DefaultTerminal,
    config: &mut Config,
    output: bool,
) -> Result<bool> {
    let mut selected = 6;
    loop {
        let check = |yes| if yes { "[x]" } else { "[ ]" };
        let labels = if output {
            vec![
                format!("{} Metadata frontmatter", check(config.output.frontmatter)),
                format!("{} Document title", check(config.output.title)),
                format!("{} Synopsis", check(config.output.summary)),
                format!("{} Source page markers", check(config.output.page_markers)),
                format!("{} Review notices", check(config.output.review_notes)),
                format!("Filename: {}", config.output.filename),
                "Continue".into(),
            ]
        } else {
            vec![
                format!("{} Copyedit prose", check(config.processing.edit)),
                format!(
                    "{} Classify and describe documents",
                    check(config.processing.classify)
                ),
                format!(
                    "{} Visual reading of every page",
                    check(config.processing.vision)
                ),
                format!("OCR: {}", config.processing.ocr),
                format!("OCR language: {}", config.processing.ocr_language),
                "Copyediting style…".into(),
                "Continue".into(),
                format!("Pipeline: {}", config.processing.pipeline),
                format!("Page concurrency: {}", config.processing.page_concurrency),
                format!(
                    "Classification override: {}",
                    config
                        .processing
                        .classification_override
                        .as_deref()
                        .unwrap_or("automatic")
                ),
                "Office renderer…".into(),
            ]
        };
        let Some(index) = ui::choose(
            t,
            if output {
                "Markdown output defaults"
            } else {
                "Processing defaults"
            },
            "Review these defaults. Enter or Space toggles a checkbox or opens an editable option. Choose Continue when ready. Your existing values are preserved.",
            &labels,
            selected,
        )?
        else {
            return Ok(false);
        };
        selected = index;
        if index == 6 {
            return Ok(true);
        }
        if output {
            match index {
                0 => config.output.frontmatter = !config.output.frontmatter,
                1 => config.output.title = !config.output.title,
                2 => config.output.summary = !config.output.summary,
                3 => config.output.page_markers = !config.output.page_markers,
                4 => config.output.review_notes = !config.output.review_notes,
                5 => {
                    if let Some(value) = ui::input(
                        t,
                        "Filename convention",
                        "Supported placeholders: {class}, {title}, {number}, {sequence}, {id}.",
                        &config.output.filename,
                        false,
                    )? {
                        config.output.filename = value.to_string();
                    }
                }
                _ => {}
            }
        } else {
            match index {
                0 => config.processing.edit = !config.processing.edit,
                1 => config.processing.classify = !config.processing.classify,
                2 => config.processing.vision = !config.processing.vision,
                3 => {
                    if let Some(i) = choice(
                        t,
                        "OCR engine",
                        "Paddle is bundled. Tesseract requires a separately installed executable. Vision uses the selected model and API billing.",
                        &[
                            "Paddle · local",
                            "Tesseract · external",
                            "Vision · selected model",
                            "Off",
                        ],
                        0,
                    )? {
                        config.processing.ocr = ["paddle", "tesseract", "vision", "off"][i].into();
                    }
                }
                4 => {
                    if let Some(v) = ui::input(
                        t,
                        "OCR language",
                        "Paddle supports en, zh, ja or auto. Tesseract uses installed language codes.",
                        &config.processing.ocr_language,
                        false,
                    )? {
                        config.processing.ocr_language = v.to_string();
                    }
                }
                5 => {
                    if let Some(v) = ui::input(
                        t,
                        "Editorial house style",
                        "Set preferences for spelling, punctuation, register and formatting. The Copyeditor and Reviewer have separate built-in roles.",
                        &config.processing.house_style,
                        false,
                    )? {
                        config.processing.house_style = v.to_string();
                    }
                }
                7 => {
                    if let Some(i) = choice(
                        t,
                        "Processing pipeline",
                        "Editorial reads every page, reconciles visual information and polishes the assembled document. Archival is retained for compatibility with older jobs.",
                        &["Editorial synthesis", "Archival compatibility"],
                        usize::from(config.processing.pipeline == "archival"),
                    )? {
                        config.processing.pipeline =
                            if i == 0 { "editorial" } else { "archival" }.into();
                    }
                }
                8 => {
                    if let Some(v) = ui::input(
                        t,
                        "Parallel page readings",
                        "Maximum simultaneous page tasks (1–16). The provider's concurrency and rate limits still apply.",
                        &config.processing.page_concurrency.to_string(),
                        false,
                    )? && let Ok(n) = v.parse::<usize>()
                        && (1..=16).contains(&n)
                    {
                        config.processing.page_concurrency = n;
                    }
                }
                9 => {
                    if let Some(v) = ui::input(
                        t,
                        "Classification override",
                        "Leave empty for automatic classification.",
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
                10 => {
                    if let Some(v) = ui::input(
                        t,
                        "Office renderer",
                        "Leave empty to use the bundled LibreOffice engine; otherwise enter the soffice executable path.",
                        &config
                            .processing
                            .office_renderer
                            .as_ref()
                            .map(|p| p.display().to_string())
                            .unwrap_or_default(),
                        false,
                    )? {
                        config.processing.office_renderer =
                            (!v.trim().is_empty()).then(|| std::path::PathBuf::from(v.trim()));
                    }
                }
                _ => {}
            }
        }
    }
}
fn open_link(url: &str) -> Result<()> {
    if !url.starts_with("https://") {
        bail!("Only verified HTTPS provider links can be opened");
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
        .context("Could not open the browser. Use the provider URL displayed in setup.")?;
    Ok(())
}

fn select_workspace(
    t: &mut ratatui::DefaultTerminal,
    d: &mut Draft,
    path: std::path::PathBuf,
) -> Result<()> {
    let next = Location::for_root(path);
    if next.root == d.location.root {
        return Ok(());
    }
    if next.config.is_file() {
        let Some(choice) = ui::choose(
            t,
            "Existing workspace",
            "This folder already contains a Librarian configuration. Load its saved settings or choose another folder. Existing jobs keep their own settings.",
            &[
                "Load this workspace's settings".into(),
                "Choose another folder".into(),
            ],
            0,
        )?
        else {
            return Ok(());
        };
        if choice != 0 {
            return Ok(());
        }
        d.config = Config::load(&next.config)?;
    }
    d.location = next;
    Ok(())
}
