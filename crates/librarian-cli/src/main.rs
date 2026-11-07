mod setup;
mod terminal;
use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand};
use librarian_native::{
    config::{Config, default_library},
    convert_file, pipeline,
    store::{Library, sql_params},
    write_new_markdown,
};
use std::{
    io::{self, IsTerminal, Write},
    path::PathBuf,
    process::ExitCode,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};

#[derive(Parser)]
#[command(
    version,
    about = "Librarian — turn source documents into a clean, searchable Markdown library"
)]
struct Cli {
    #[arg(long, global = true)]
    library: Option<PathBuf>,
    #[arg(long, global = true)]
    config: Option<PathBuf>,
    /// Emit machine-readable JSON instead of animated terminal output.
    #[arg(long, global = true)]
    json: bool,
    #[command(subcommand)]
    command: Option<Command>,
}
#[derive(Subcommand)]
enum Command {
    /// Interactive setup for processing, providers, metadata and output conventions.
    Setup,
    /// Interactive dashboard, source/destination picker, queue and live progress.
    Tui,
    /// Configure processing and output profiles.
    Config {
        #[command(subcommand)]
        action: ConfigCommand,
    },
    /// Choose source/destination, queue files, and process them sequentially.
    Clean {
        source: Option<PathBuf>,
        #[arg(short, long)]
        output: Option<PathBuf>,
        #[arg(long)]
        recursive: bool,
        #[arg(long)]
        create_output: bool,
        /// Extract without paid editing, classification or vision calls.
        #[arg(long)]
        extract_only: bool,
        #[arg(long)]
        no_vision: bool,
        #[arg(long)]
        body_only: bool,
        /// Queue without starting the worker.
        #[arg(long)]
        queue_only: bool,
    },
    /// Process queued files, resuming committed work after an interruption.
    Run {
        #[arg(long)]
        quiet: bool,
    },
    /// Inspect the persisted queue, optionally watching live updates.
    Status {
        #[arg(long)]
        watch: bool,
    },
    /// Pause a job, or all active/queued jobs if no id is supplied.
    Pause { id: Option<i64> },
    /// Resume paused, failed or cancelled jobs.
    Resume {
        id: Option<i64>,
        #[arg(long)]
        start: bool,
    },
    /// Reprocess an immutable original with current configuration as a new job.
    Retry {
        id: i64,
        #[arg(long)]
        start: bool,
    },
    /// Cancel without deleting originals or committed work.
    Cancel { id: Option<i64> },
    /// Full-text search of accepted document content.
    Search {
        query: String,
        #[arg(short, long, default_value_t = 20)]
        limit: usize,
    },
    /// Show job details, diagnostics and metadata.
    Show { id: i64 },
    /// Print the resulting Markdown to stdout.
    Print { id: i64 },
    /// Re-export with current presentation settings, without model calls.
    Export {
        id: i64,
        #[arg(short, long)]
        output: PathBuf,
        #[arg(long)]
        create_output: bool,
    },
    /// Inspect configuration and engine availability without provider requests.
    Doctor {
        /// Load native libraries and OCR model sessions to diagnose ABI/model errors.
        #[arg(long)]
        load_engines: bool,
    },
    /// Install or inspect pinned PDF/OCR engine assets.
    Engines {
        #[command(subcommand)]
        action: EngineCommand,
    },
    /// Convert UTF-8 text/Markdown directly, without adding it to the library.
    Convert {
        source: PathBuf,
        #[arg(short, long)]
        output: Option<PathBuf>,
    },
    /// Report Markdown syntax counts.
    Inspect { source: PathBuf },
}
#[derive(Subcommand)]
enum ConfigCommand {
    Show,
    Path,
    Schema,
    Set { key: String, value: String },
    Edit,
    Reset,
}
#[derive(Subcommand)]
enum EngineCommand {
    Install,
    List,
}

#[tokio::main]
async fn main() -> ExitCode {
    match run(Cli::parse()).await {
        Ok(code) => ExitCode::from(code),
        Err(error) => {
            if error
                .downcast_ref::<io::Error>()
                .is_some_and(|e| e.kind() == io::ErrorKind::BrokenPipe)
            {
                return ExitCode::SUCCESS;
            }
            eprintln!("error: {error:#}");
            ExitCode::FAILURE
        }
    }
}

async fn run(cli: Cli) -> Result<u8> {
    let root = cli.library.unwrap_or_else(default_library);
    let config_path = cli.config.unwrap_or_else(|| root.join("config.toml"));
    let command = cli.command.unwrap_or(Command::Tui);
    match command {
        Command::Convert { source, output } => {
            let doc = convert_file(&source)?;
            if let Some(path) = output {
                write_new_markdown(&path, &doc.markdown)?;
                eprintln!("Wrote {}", path.display());
            } else {
                io::stdout().lock().write_all(doc.markdown.as_bytes())?;
            }
            return Ok(0);
        }
        Command::Inspect { source } => {
            println!(
                "{}",
                serde_json::to_string_pretty(&convert_file(&source)?.stats)?
            );
            return Ok(0);
        }
        Command::Setup => {
            setup::configure(&config_path)?;
            return Ok(0);
        }
        Command::Config { action } => {
            configure(action, &config_path)?;
            return Ok(0);
        }
        _ => {}
    }
    let library = Library::open(&root)?;
    match command {
        Command::Tui => terminal::run(&library.root, &config_path)?,
        Command::Clean {
            source,
            output,
            recursive,
            create_output,
            extract_only,
            no_vision,
            body_only,
            queue_only,
        } => {
            let mut config = Config::load(&config_path)?;
            if extract_only {
                config.processing.edit = false;
                config.processing.classify = false;
                config.processing.vision = false;
            }
            if no_vision {
                config.processing.vision = false;
            }
            if body_only {
                config.output.frontmatter = false;
                config.output.title = false;
                config.output.summary = false;
                config.output.page_markers = false;
                config.output.review_notes = false;
            }
            let (source, output, create_output) = setup::paths(source, output, create_output)?;
            let jobs = pipeline::queue(
                &library,
                &source,
                &output,
                &config,
                recursive,
                create_output,
            )?;
            if cli.json {
                println!(
                    "{}",
                    serde_json::json!({"queued":jobs,"destination":output})
                );
            } else {
                eprintln!("==> Queued {} file(s) → {}", jobs.len(), output.display());
            }
            if !queue_only {
                worker(&library, cli.json, false).await?;
                return result_code(&library, &jobs);
            }
        }
        Command::Run { quiet } => {
            let jobs = library
                .jobs(100000)?
                .iter()
                .filter(|j| matches!(j.status.as_str(), "queued" | "running"))
                .map(|j| j.id)
                .collect::<Vec<_>>();
            worker(&library, cli.json, quiet).await?;
            return result_code(&library, &jobs);
        }
        Command::Status { watch } => {
            if watch && !cli.json {
                terminal::run(&library.root, &config_path)?;
            } else {
                println!("{}", serde_json::to_string_pretty(&library.jobs(1000)?)?);
            }
        }
        Command::Pause { id } => {
            let n = library.control(id, "pause")?;
            println!("{}", serde_json::json!({"paused":n}));
        }
        Command::Cancel { id } => {
            let n = library.control(id, "cancel")?;
            println!("{}", serde_json::json!({"cancelled":n}));
        }
        Command::Resume { id, start } => {
            let n = library.control(id, "resume")?;
            println!("{}", serde_json::json!({"resumed":n}));
            if start {
                worker(&library, cli.json, false).await?;
            }
        }
        Command::Retry { id, start } => {
            let source = library.job(id)?;
            if source.status == "running" {
                bail!("Pause the existing job before retrying with changed configuration");
            }
            let config = Config::load(&config_path)?;
            let transaction = library.db.unchecked_transaction()?;
            let new_id = library.enqueue(
                std::path::Path::new(&source.source),
                std::path::Path::new(&source.destination),
                &config,
            )?;
            if let Some(document) = source.document {
                library.db.execute("INSERT INTO job_inputs(job,digest,original,format) SELECT ?,digest,original,format FROM documents WHERE id=?",sql_params![new_id,document])?;
            }
            transaction.commit()?;
            println!("{}", serde_json::json!({"queued":new_id,"retry_of":id}));
            if start {
                worker(&library, cli.json, false).await?;
                return result_code(&library, &[new_id]);
            }
        }
        Command::Search { query, limit } => {
            let results = library.search(&query, limit)?;
            if cli.json {
                println!("{}", serde_json::to_string_pretty(&results)?);
            } else {
                for hit in results {
                    println!(
                        "#{} · {}\n{}\n",
                        hit["job"],
                        hit["title"].as_str().unwrap_or(""),
                        hit["snippet"].as_str().unwrap_or("")
                    );
                }
            }
        }
        Command::Show { id } => {
            let job = library.job(id)?;
            let metadata = if job.document.is_some() {
                Some(librarian_native::export::metadata(&library, id)?)
            } else {
                None
            };
            println!(
                "{}",
                serde_json::to_string_pretty(
                    &serde_json::json!({"job":job,"metadata":metadata,"events":library.events(Some(id),100)?})
                )?
            );
        }
        Command::Print { id } => {
            let job = library.job(id)?;
            let path = job.output.context("Job has no published output")?;
            let mut file = std::fs::File::open(path)?;
            io::copy(&mut file, &mut io::stdout().lock())?;
        }
        Command::Export {
            id,
            output,
            create_output,
        } => {
            let _lock = library.lock_worker()?;
            if !output.exists() {
                if create_output {
                    std::fs::create_dir_all(&output)?;
                } else {
                    bail!("Destination is missing; pass --create-output");
                }
            }
            let source = library.job(id)?;
            if !matches!(source.status.as_str(), "complete" | "needs_review") {
                bail!("Only completed jobs can be re-exported");
            }
            let config = Config::load(&config_path)?;
            let new_id = library.enqueue(
                std::path::Path::new(&source.source),
                &output.canonicalize()?,
                &config,
            )?;
            let tx = library.db.unchecked_transaction()?;
            library.db.execute(
                "UPDATE jobs SET document=?,status='running',stage='exporting' WHERE id=?",
                sql_params![source.document, new_id],
            )?;
            for sql in [
                "INSERT INTO planned SELECT ?,ordinal,payload FROM planned WHERE job=?",
                "INSERT INTO accepted SELECT ?,ordinal,payload,needs_review FROM accepted WHERE job=?",
                "INSERT INTO job_metadata SELECT ?,payload FROM job_metadata WHERE job=?",
            ] {
                library.db.execute(sql, sql_params![new_id, id])?;
            }
            tx.commit()?;
            let path = librarian_native::export::publish(&library, new_id, &config)?;
            println!("{}", path.display());
        }
        Command::Doctor { load_engines } => {
            let config = Config::load(&config_path)?;
            let engines = librarian_native::assets::defaults()?;
            if load_engines {
                librarian_native::assets::load_engines(&library.root.join("engines"))?;
            }
            println!(
                "{}",
                serde_json::to_string_pretty(
                    &serde_json::json!({"library":library.root,"config":config_path,"platform":format!("{}-{}",std::env::consts::OS,std::env::consts::ARCH),"provider":config.provider.kind,"model_configured":!config.provider.model.is_empty(),"credential_environment_present":!config.provider.key_env.is_empty()&&std::env::var_os(&config.provider.key_env).is_some(),"engines":engines,"engine_sessions_loaded":load_engines})
                )?
            );
        }
        Command::Engines {
            action: EngineCommand::List,
        } => println!(
            "{}",
            serde_json::to_string_pretty(&librarian_native::assets::defaults()?)?
        ),
        Command::Engines {
            action: EngineCommand::Install,
        } => {
            let pb = indicatif::ProgressBar::new_spinner();
            pb.set_style(indicatif::ProgressStyle::with_template(
                "{spinner:.cyan} {msg} [{bar:30.cyan/blue}] {bytes}/{total_bytes}",
            )?);
            librarian_native::assets::install(
                &library.root.join("engines"),
                |name, done, total| {
                    pb.set_message(name.to_owned());
                    if let Some(n) = total {
                        pb.set_length(n);
                    }
                    pb.set_position(done);
                },
            )
            .await?;
            pb.finish_with_message("PDF/OCR engines installed");
        }
        _ => unreachable!(),
    }
    Ok(0)
}

async fn worker(library: &Library, json: bool, quiet: bool) -> Result<()> {
    let started = librarian_native::store::now();
    let stop = Arc::new(AtomicBool::new(false));
    let signal = stop.clone();
    let signal_root = library.root.clone();
    let handler = tokio::spawn(async move {
        let _ = tokio::signal::ctrl_c().await;
        signal.store(true, Ordering::Relaxed);
        if let Ok(library) = Library::open(&signal_root) {
            let _ = library.control(None, "pause");
        }
    });
    let progress = if !quiet && !json && io::stderr().is_terminal() {
        Some(terminal::Progress::start(
            library.root.clone(),
            stop.clone(),
        )?)
    } else {
        None
    };
    let result = pipeline::run_queue(library, stop.clone()).await;
    stop.store(true, Ordering::Relaxed);
    handler.abort();
    if let Some(progress) = progress {
        progress.finish();
    }
    result?;
    if !quiet {
        for job in library
            .jobs(1000)?
            .into_iter()
            .rev()
            .filter(|j| j.updated >= started)
        {
            if json {
                println!("{}", serde_json::to_string(&job)?);
            } else {
                eprintln!(
                    "{} #{} {}{}",
                    match job.status.as_str() {
                        "complete" => "✓",
                        "needs_review" => "!",
                        "failed" => "✗",
                        _ => "·",
                    },
                    job.id,
                    job.status,
                    job.output
                        .map(|p| format!(" → {p}"))
                        .or_else(|| job.error.map(|s| format!(" — {s}")))
                        .unwrap_or_default()
                );
            }
        }
    }
    Ok(())
}
fn result_code(library: &Library, ids: &[i64]) -> Result<u8> {
    let mut code = 0;
    for id in ids {
        let job = library.job(*id)?;
        if matches!(job.status.as_str(), "failed" | "budget_exhausted") {
            return Ok(1);
        }
        if job.status == "needs_review" {
            code = 2;
        }
        if matches!(job.status.as_str(), "paused" | "cancelled") {
            code = 3;
        }
    }
    Ok(code)
}

fn configure(action: ConfigCommand, path: &std::path::Path) -> Result<()> {
    match action {
        ConfigCommand::Path => println!("{}", path.display()),
        ConfigCommand::Show => println!("{}", toml::to_string_pretty(&Config::load(path)?)?),
        ConfigCommand::Schema => println!(
            "{}",
            serde_json::to_string_pretty(&schemars::schema_for!(Config))?
        ),
        ConfigCommand::Reset => {
            let backup =
                path.with_extension(format!("toml.backup-{}", librarian_native::store::now()));
            if path.exists() {
                std::fs::copy(path, &backup)?;
            }
            Config::default().save(path)?;
            println!("Wrote {}", path.display());
        }
        ConfigCommand::Set { key, value } => {
            let mut config = serde_json::to_value(Config::load(path)?)?;
            let mut current = &mut config;
            let parts: Vec<_> = key.split('.').collect();
            for part in &parts[..parts.len() - 1] {
                current = current
                    .get_mut(*part)
                    .with_context(|| format!("Unknown configuration section {part}"))?;
            }
            let field = parts.last().context("Missing key")?;
            let object = current
                .as_object_mut()
                .context("Configuration section is not an object")?;
            if !object.contains_key(*field)
                && !key.starts_with("taxonomy.")
                && !key.starts_with("output.fields.")
                && !key.starts_with("processing.glossary.")
            {
                bail!("Unknown configuration field {key}");
            }
            let parsed = serde_json::from_str(&value).unwrap_or(serde_json::Value::String(value));
            object.insert((*field).into(), parsed);
            let config: Config = serde_json::from_value(config)?;
            config.save(path)?;
            println!("Updated {key}");
        }
        ConfigCommand::Edit => {
            if !path.exists() {
                Config::default().save(path)?;
            }
            let editor = std::env::var("VISUAL")
                .or_else(|_| std::env::var("EDITOR"))
                .unwrap_or_else(|_| {
                    if cfg!(windows) {
                        "notepad".into()
                    } else {
                        "vi".into()
                    }
                });
            let mut args = editor.split_whitespace();
            let program = args.next().context("Empty editor command")?;
            let status = std::process::Command::new(program)
                .args(args)
                .arg(path)
                .status()?;
            if !status.success() {
                bail!("Editor exited unsuccessfully");
            }
            Config::load(path)?;
            println!("Configuration saved");
        }
    }
    Ok(())
}
