use clap::{Parser, Subcommand};
use librarian_native::{convert_file, write_new_markdown};
use std::{
    io::{self, Write},
    path::PathBuf,
    process::ExitCode,
};

#[derive(Parser)]
#[command(
    version,
    about = "Librarian Rust foundation: source-preserving Markdown conversion",
    after_help = "This build supports UTF-8 text and Markdown. OCR, LLM editing, classification, and indexing are not yet implemented."
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// Convert text to literal Markdown, or preserve an existing Markdown document.
    Convert {
        source: PathBuf,
        /// Write a new .md file. Existing files are never replaced; default is stdout.
        #[arg(short, long)]
        output: Option<PathBuf>,
    },
    /// Report Markdown structure as JSON; does not validate Mermaid semantics.
    Inspect { source: PathBuf },
}

fn run(cli: Cli) -> Result<(), Box<dyn std::error::Error>> {
    match cli.command {
        Command::Convert { source, output } => {
            let document = convert_file(&source)?;
            if let Some(path) = output {
                write_new_markdown(&path, &document.markdown)?;
                eprintln!("Wrote {}", path.display());
            } else {
                io::stdout()
                    .lock()
                    .write_all(document.markdown.as_bytes())?;
            }
        }
        Command::Inspect { source } => {
            let document = convert_file(&source)?;
            let json = serde_json::to_string_pretty(&document.stats)?;
            writeln!(io::stdout().lock(), "{json}")?;
        }
    }
    Ok(())
}

fn main() -> ExitCode {
    match run(Cli::parse()) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error)
            if error
                .downcast_ref::<io::Error>()
                .is_some_and(|e| e.kind() == io::ErrorKind::BrokenPipe) =>
        {
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("error: {error}");
            ExitCode::FAILURE
        }
    }
}
