mod onboarding;
mod parse_cmd;
mod parse_tui;
mod setup;
mod terminal;
mod ui;
use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand};
use librarian_native::{
    config::Config,
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
    /// Workspace folder to use instead of the active profile's workspace.
    #[arg(long, global = true)]
    library: Option<PathBuf>,
    /// Configuration file to use instead of config.toml in the workspace.
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
    /// Run the setup wizard: workspace, provider, credential, model, limits,
    /// defaults, engines and a connection check.
    Setup,
    #[command(hide = true)]
    SetupVerify,
    /// Interactive dashboard, source/destination picker, queue and live progress.
    Tui,
    /// Show, locate, describe, change, edit or reset the configuration file.
    Config {
        #[command(subcommand)]
        action: ConfigCommand,
    },
    /// Choose source/destination, queue files, and process them sequentially.
    Clean {
        /// Source file or folder. Omit on a terminal to choose it in the browser.
        source: Option<PathBuf>,
        /// Destination folder for the finished Markdown. Omit on a terminal to
        /// choose it in the browser.
        #[arg(short, long)]
        output: Option<PathBuf>,
        /// Include files in subfolders when the source is a folder.
        #[arg(long)]
        recursive: bool,
        /// Create the destination folder if it does not exist.
        #[arg(long)]
        create_output: bool,
        /// Extract without paid editing, classification or vision calls.
        #[arg(long)]
        extract_only: bool,
        /// Send no image requests to the model.
        #[arg(long)]
        no_vision: bool,
        /// Omit front matter, title, synopsis, page markers and review notes.
        #[arg(long)]
        body_only: bool,
        /// Queue without starting the worker.
        #[arg(long)]
        queue_only: bool,
    },
    /// Process queued files, resuming committed work after an interruption.
    Run {
        /// Show no progress display and print no per-job result lines.
        #[arg(long)]
        quiet: bool,
    },
    /// Print the queue as JSON, or open the dashboard with --watch.
    Status {
        /// Open the dashboard instead of printing. Ignored with --json.
        #[arg(long)]
        watch: bool,
    },
    /// Pause a job, or all active/queued jobs if no id is supplied.
    Pause {
        /// Job number. Omit to pause every queued and running job.
        id: Option<i64>,
    },
    /// Return paused, cancelled, failed or budget-stopped jobs to the queue.
    Resume {
        /// Job number. Omit to resume every eligible job.
        id: Option<i64>,
        /// Start the worker after resuming and wait for it to finish.
        #[arg(long)]
        start: bool,
    },
    /// Reprocess an immutable original with current configuration as a new job.
    Retry {
        /// Job number whose retained original is processed again.
        id: i64,
        /// Start the worker after queueing and wait for it to finish.
        #[arg(long)]
        start: bool,
    },
    /// Cancel without deleting originals or committed work.
    Cancel {
        /// Job number. Omit to cancel every queued, running and paused job.
        id: Option<i64>,
    },
    /// Full-text search of accepted document content.
    Search {
        /// Words to find. Every word must occur; each is matched literally.
        query: String,
        /// Maximum number of results.
        #[arg(short, long, default_value_t = 20)]
        limit: usize,
    },
    /// Show job details, diagnostics and metadata.
    Show {
        /// Job number.
        id: i64,
    },
    /// Render the source and run local OCR only; no paid calls or document export.
    Prepare {
        /// Source file.
        source: PathBuf,
    },
    /// Inspect the ordered image/OCR/visual/copyeditor matrix for a run.
    Pages {
        /// Job number.
        id: i64,
        /// Print the full contents of this page instead of the summary.
        #[arg(long)]
        page: Option<i64>,
    },
    /// Print an immutable role prompt; omit the role to list versions and hashes.
    Prompts {
        /// copyeditor, reviewer or protocol.
        role: Option<String>,
    },
    /// Inspect editorial request packets, evidence, interventions and revisions.
    Editorial {
        /// Job number.
        id: i64,
    },
    /// Save an expert correction privately for later calibration; does not re-export.
    Feedback {
        /// Job number.
        id: i64,
        /// Reviewed segment the correction applies to, in the form review:N.
        #[arg(long)]
        segment: String,
        /// File holding the corrected text.
        #[arg(long)]
        replacement: PathBuf,
        /// Why the correction is right. Required.
        #[arg(long)]
        reason: String,
    },
    /// List finished documents with their metadata, class and accession number.
    Catalog,
    /// Show library totals by status and classification.
    Stats,
    /// Print the resulting Markdown to stdout.
    Print {
        /// Job number.
        id: i64,
    },
    /// Re-export with current presentation settings, without model calls.
    Export {
        /// Job number of a complete or needs_review job.
        id: i64,
        /// Destination folder for the new copy.
        #[arg(short, long)]
        output: PathBuf,
        /// Create the destination folder if it does not exist.
        #[arg(long)]
        create_output: bool,
    },
    /// Inspect configuration and engine availability without provider requests.
    Doctor {
        /// Load native libraries and OCR model sessions to diagnose ABI/model errors.
        #[arg(long)]
        load_engines: bool,
    },
    /// Install or list the pinned PDF and OCR engine assets.
    Engines {
        #[command(subcommand)]
        action: EngineCommand,
    },
    /// Parse a PDF, or a folder of PDFs, locally into typed layout blocks,
    /// Markdown and JSON. No model calls.
    Parse {
        /// PDF file, or a folder of PDF files.
        source: PathBuf,
        /// Folder for <name>.md and <name>.layout.json; omit to print Markdown.
        #[arg(short, long)]
        output: Option<PathBuf>,
        /// Create the output folder if it does not exist.
        #[arg(long)]
        create_output: bool,
        /// Include PDFs in subfolders when the source is a folder.
        #[arg(short, long)]
        recursive: bool,
        /// Keep running headers, footers and page numbers in the Markdown.
        #[arg(long)]
        furniture: bool,
        /// Mark page boundaries in the Markdown with HTML comments.
        #[arg(long)]
        page_markers: bool,
        /// Text only: no bold/italic/link markup or picture markers.
        #[arg(long)]
        plain: bool,
        /// Include word indices and positioned words in the JSON.
        #[arg(long)]
        words: bool,
        /// Use this PDFium library instead of the installed engine.
        #[arg(long, hide = true)]
        pdfium: Option<PathBuf>,
    },
    /// Convert UTF-8 text/Markdown directly, without adding it to the library.
    Convert {
        /// A .txt, .md or .markdown file.
        source: PathBuf,
        /// Markdown file to write; it must not exist. Omit to print.
        #[arg(short, long)]
        output: Option<PathBuf>,
    },
    /// Report Markdown syntax counts.
    Inspect {
        /// A .txt, .md or .markdown file.
        source: PathBuf,
    },
}
#[derive(Subcommand)]
enum ConfigCommand {
    /// Print the configuration as TOML.
    Show,
    /// Print the path of the configuration file.
    Path,
    /// Print the JSON Schema of the configuration.
    Schema,
    /// Set one value, for example processing.page_concurrency 3.
    Set {
        /// Dotted key: section.field.
        key: String,
        /// New value. JSON is accepted; other text is taken as a string.
        value: String,
    },
    /// Open the file in $VISUAL or $EDITOR, then validate it.
    Edit,
    /// Copy the file to a dated backup, then write the defaults.
    Reset,
}
#[derive(Subcommand)]
enum EngineCommand {
    /// Download, verify and install the engines into the workspace.
    Install,
    /// Print the pinned engine assets for this platform.
    List,
}

#[tokio::main]
async fn main() -> ExitCode {
    match run(Cli::parse()).await {
        Ok(code) => ExitCode::from(code),
        Err(error) => {
            if error.is::<ui::SaveExit>() {
                return ExitCode::SUCCESS;
            }
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
    let mut location = match cli.library {
        Some(root) => librarian_native::profile::Location::for_root(if root.is_absolute() {
            root
        } else {
            std::env::current_dir()?.join(root)
        }),
        None => librarian_native::profile::Location::active()?,
    };
    if let Some(path) = cli.config {
        location.config = if path.is_absolute() {
            path
        } else {
            std::env::current_dir()?.join(path)
        };
    }
    let root = location.root.clone();
    let config_path = location.config.clone();
    let command = cli.command.unwrap_or(Command::Tui);
    match command {
        Command::Prompts { ref role } => {
            use librarian_core::editorial;
            let roles = [
                (
                    "copyeditor",
                    editorial::COPYEDITOR_VERSION,
                    editorial::COPYEDITOR,
                ),
                ("reviewer", editorial::REVIEWER_VERSION, editorial::REVIEWER),
                (
                    "protocol",
                    "editorial-protocol-v2",
                    editorial::contract::PROTOCOL,
                ),
            ];
            if let Some(name) = role {
                let (_, version, text) = roles
                    .iter()
                    .find(|(r, v, _)| r == name || v == name)
                    .context("Role must be copyeditor, reviewer or protocol")?;
                if cli.json {
                    println!(
                        "{}",
                        serde_json::json!({"version":version,"sha256":editorial::contract::digest(text),"text":text})
                    );
                } else {
                    print!("{text}");
                }
            } else {
                println!("{}",serde_json::to_string_pretty(&roles.iter().map(|(role,version,text)|serde_json::json!({"role":role,"version":version,"sha256":editorial::contract::digest(text)})).collect::<Vec<_>>())?);
            }
            return Ok(0);
        }
        Command::Parse {
            ref source,
            ref output,
            create_output,
            recursive,
            furniture,
            page_markers,
            plain,
            words,
            ref pdfium,
        } => {
            let mut options = if plain {
                librarian_core::layout::RenderOptions::plain()
            } else {
                librarian_core::layout::RenderOptions::default()
            };
            options.furniture |= furniture;
            options.page_markers = page_markers;
            return parse_cmd::run(parse_cmd::Request {
                root: &root,
                source,
                output: output.as_deref(),
                create_output,
                recursive,
                options,
                words,
                pdfium: pdfium.as_deref(),
                json: cli.json,
                stop: None,
            });
        }
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
            setup::configure(location)?;
            return Ok(0);
        }
        Command::SetupVerify => {
            let result = async {
                let config = Config::load(&config_path)?;
                librarian_native::catalog::verify(&config, &root).await
            }
            .await;
            return match result {
                Ok(report) => {
                    println!("{}", serde_json::to_string(&report)?);
                    Ok(0)
                }
                Err(error) => {
                    println!("{}", serde_json::json!({"error":format!("{error:#}")}));
                    Ok(1)
                }
            };
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
                let jobs = library
                    .jobs(100000)?
                    .into_iter()
                    .filter(|j| matches!(j.status.as_str(), "queued" | "running"))
                    .map(|j| j.id)
                    .collect::<Vec<_>>();
                worker(&library, cli.json, false).await?;
                return result_code(&library, &jobs);
            }
        }
        Command::Retry { id, start } => {
            let config = Config::load(&config_path)?;
            let new_id = pipeline::retry(&library, id, &config)?;
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
                    &serde_json::json!({"job":job,"metadata":metadata,"editorial":librarian_native::editorial::progress(&library,id)?,"events":library.events(Some(id),100)?})
                )?
            );
        }
        Command::Prepare { source } => {
            let mut config = Config::load(&config_path)?;
            config.processing.pipeline = "editorial".into();
            config.processing.prepare_only = true;
            config.processing.edit = false;
            config.processing.vision = false;
            config.processing.classify = false;
            let destination = library.root.join("prepared");
            let jobs = pipeline::queue(&library, &source, &destination, &config, false, true)?;
            println!(
                "{}",
                serde_json::json!({"queued":jobs,"mode":"page images and local OCR only"})
            );
            worker(&library, cli.json, false).await?;
            return result_code(&library, &jobs);
        }
        Command::Pages { id, page } => {
            let mut query=library.db.prepare("SELECT page,image,native,ocr,reading,cleaned FROM editorial_pages WHERE job=? AND (? IS NULL OR page=?) ORDER BY page")?;
            let rows=query.query_map(sql_params![id,page,page],|r|{
                let number:i64=r.get(0)?;let image:String=r.get(1)?;let native:String=r.get(2)?;
                let ocr:Option<String>=r.get(3)?;let reading:Option<String>=r.get(4)?;let cleaned:Option<String>=r.get(5)?;
                Ok(if page.is_some(){serde_json::json!({"page":number,"A_image":image,"native":native,"B_ocr":ocr.and_then(|s|serde_json::from_str::<serde_json::Value>(&s).ok()),"C_visual":reading,"copyeditor":cleaned})}
                else{serde_json::json!({"page":number,"image":image,"native_bytes":native.len(),"ocr_done":ocr.is_some(),"visual_done":reading.is_some(),"copyedit_done":cleaned.is_some()})})
            })?.collect::<Result<Vec<_>,_>>()?;
            println!("{}", serde_json::to_string_pretty(&rows)?);
        }
        Command::Editorial { id } => println!(
            "{}",
            serde_json::to_string_pretty(&librarian_native::editorial::v2::inspect(&library, id)?)?
        ),
        Command::Feedback {
            id,
            segment,
            replacement,
            reason,
        } => {
            let text = std::fs::read_to_string(&replacement)
                .with_context(|| format!("Read expert correction {}", replacement.display()))?;
            let record =
                librarian_native::editorial::v2::feedback(&library, id, &segment, &text, &reason)?;
            println!(
                "{}",
                serde_json::json!({"feedback_id":record,"job":id,"segment":segment,"publication_changed":false})
            );
        }
        Command::Catalog => {
            let mut query=library.db.prepare("SELECT j.id,j.source,j.output,j.status,m.payload,a.class,a.sequence FROM jobs j LEFT JOIN job_metadata m ON m.job=j.id LEFT JOIN accessions a ON a.job=j.id WHERE j.status IN ('complete','needs_review') ORDER BY COALESCE(a.class,''),j.id")?;
            let rows=query.query_map([],|r|{let metadata:Option<String>=r.get(4)?;Ok(serde_json::json!({"job":r.get::<_,i64>(0)?,"source":r.get::<_,String>(1)?,"output":r.get::<_,Option<String>>(2)?,"status":r.get::<_,String>(3)?,"metadata":metadata.and_then(|s|serde_json::from_str::<serde_json::Value>(&s).ok()),"class":r.get::<_,Option<String>>(5)?,"sequence":r.get::<_,Option<i64>>(6)?}))})?.collect::<Result<Vec<_>,_>>()?;
            println!("{}", serde_json::to_string_pretty(&rows)?);
        }
        Command::Stats => {
            let mut query=library.db.prepare("SELECT status,count(*),sum(requests),sum(input_tokens),sum(output_tokens) FROM jobs GROUP BY status")?;
            let statuses=query.query_map([],|r|Ok(serde_json::json!({"status":r.get::<_,String>(0)?,"documents":r.get::<_,i64>(1)?,"requests":r.get::<_,i64>(2)?,"input_tokens":r.get::<_,i64>(3)?,"output_tokens":r.get::<_,i64>(4)?})))?.collect::<Result<Vec<_>,_>>()?;
            let mut query = library
                .db
                .prepare("SELECT class,count(*) FROM accessions GROUP BY class ORDER BY class")?;
            let categories=query.query_map([],|r|Ok(serde_json::json!({"class":r.get::<_,String>(0)?,"documents":r.get::<_,i64>(1)?})))?.collect::<Result<Vec<_>,_>>()?;
            println!(
                "{}",
                serde_json::to_string_pretty(
                    &serde_json::json!({"statuses":statuses,"categories":categories})
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
                "INSERT INTO accessions(job,class,sequence) SELECT ?,class,sequence FROM accessions WHERE job=?",
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
                    &serde_json::json!({"library":library.root,"config":config_path,"platform":format!("{}-{}",std::env::consts::OS,std::env::consts::ARCH),"provider":config.provider.kind,"model_configured":!config.provider.model.is_empty(),"credential_storage":config.provider.credential.as_ref().map(|r|if r.session_only{"session"}else{"system"}),"credential_environment_present":!config.provider.key_env.is_empty()&&std::env::var_os(&config.provider.key_env).is_some(),"engines":engines,"engine_sessions_loaded":load_engines,"office_renderer":librarian_native::editorial::pages::office_executable(&library.root,&config)})
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
            let string_field = object.get(*field).is_some_and(serde_json::Value::is_string)
                || matches!(
                    key.as_str(),
                    "processing.classification_override" | "processing.office_renderer"
                );
            let parsed = if string_field && value != "null" {
                serde_json::Value::String(serde_json::from_str::<String>(&value).unwrap_or(value))
            } else {
                serde_json::from_str(&value).unwrap_or(serde_json::Value::String(value))
            };
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
