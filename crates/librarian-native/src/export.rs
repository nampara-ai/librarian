use crate::{
    config::Config,
    store::{Library, now},
};
use anyhow::{Context, Result, bail};
use librarian_core::corpus::{Content, Metadata, render_content, safe_filename};
use rusqlite::{OptionalExtension, params};
use serde_json::json;
use std::{
    fs::File,
    io::{Read, Write},
    path::{Path, PathBuf},
};

pub fn metadata(library: &Library, job: i64) -> Result<Metadata> {
    let payload: Option<String> = library
        .db
        .query_row("SELECT payload FROM job_metadata WHERE job=?", [job], |r| {
            r.get(0)
        })
        .optional()?;
    if let Some(payload) = payload {
        Ok(serde_json::from_str(&payload)?)
    } else {
        library.metadata(
            library
                .job(job)?
                .document
                .as_deref()
                .context("Job has no document")?,
        )
    }
}

pub fn publish(library: &Library, job: i64, config: &Config) -> Result<PathBuf> {
    let state = library.job(job)?;
    if state.status != "running" {
        bail!("Job is not running; export cancelled");
    }
    let destination = PathBuf::from(&state.destination);
    if !destination.is_dir() {
        bail!("Selected output folder no longer exists");
    }
    let metadata = metadata(library, job)?;
    let count: i64 =
        library
            .db
            .query_row("SELECT count(*) FROM planned WHERE job=?", [job], |r| {
                r.get(0)
            })?;
    let accepted: i64 =
        library
            .db
            .query_row("SELECT count(*) FROM accepted WHERE job=?", [job], |r| {
                r.get(0)
            })?;
    if count == 0 || count != accepted {
        bail!("Cannot publish incomplete block ownership ({accepted}/{count})");
    }
    let review: i64 = library.db.query_row(
        "SELECT count(*) FROM accepted WHERE job=? AND needs_review=1",
        [job],
        |r| r.get(0),
    )?;
    let status = if review > 0 {
        "needs_review"
    } else {
        "complete"
    };
    let id = state
        .document
        .as_deref()
        .context("Missing document identity")?;
    let short = &id[..12.min(id.len())];
    let filename = safe_filename(&config.output.filename, &metadata, short, job as u64)
        .map_err(anyhow::Error::msg)?;
    let mut target = destination.join(filename);
    let prior: Option<(String, String, String)> = library
        .db
        .query_row(
            "SELECT path,digest,state FROM publications WHERE job=?",
            [job],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()?;
    if let Some((path, digest, _)) = &prior {
        let path = PathBuf::from(path);
        if path.exists() && file_digest(&path)? == *digest {
            index(library, job, &metadata.title)?;
            library.finish(job, status, Some(&path.to_string_lossy()), None)?;
            return Ok(path);
        }
    }
    if target.exists() {
        let stem = target
            .file_stem()
            .unwrap_or_default()
            .to_string_lossy()
            .into_owned();
        let mut n = 2;
        loop {
            let path = destination.join(format!("{stem} ({n}).md"));
            if !path.exists() {
                target = path;
                break;
            }
            n += 1;
            if n > 100_000 {
                bail!("Too many conflicting output filenames");
            }
        }
    }
    let mut temp = tempfile::NamedTempFile::new_in(&destination)?;
    if config.output.frontmatter {
        let values = json!({"id":id,"title":metadata.title,"summary":metadata.summary,"tags":metadata.tags,"class_code":metadata.class_code,"taxonomy":metadata.taxonomy,"source":state.source,"status":status});
        writeln!(temp, "---")?;
        for (field, key) in &config.output.fields {
            writeln!(temp, "{key}: {}", serde_json::to_string(&values[field])?)?;
        }
        writeln!(temp, "---\n")?;
    }
    if config.output.title && !metadata.title.is_empty() {
        writeln!(
            temp,
            "{}",
            render_content(&Content::Heading {
                level: 1,
                text: metadata.title.clone()
            })
            .map_err(anyhow::Error::msg)?
        )?;
    }
    if config.output.summary && !metadata.summary.is_empty() {
        writeln!(
            temp,
            "**Synopsis**\n\n{}\n",
            render_content(&Content::Prose {
                text: metadata.summary.clone()
            })
            .map_err(anyhow::Error::msg)?
        )?;
    }
    let mut previous_page = 0;
    let mut previous_list = false;
    let mut review_notes = Vec::new();
    for ordinal in 0..count {
        if !library.active(job)? {
            bail!("Job paused or cancelled before publication");
        }
        let block = library
            .accepted(job, ordinal as u64)?
            .context("Missing accepted block")?;
        if let Content::Figure { asset, .. } = &block.content {
            let name = Path::new(asset)
                .file_name()
                .context("Invalid asset filename")?;
            let source = library.root.join("assets").join(name);
            let output_folder = destination.join("assets");
            std::fs::create_dir_all(&output_folder)?;
            let output = output_folder.join(name);
            if output.exists() {
                if file_digest(&output)? != file_digest(&source)? {
                    bail!(
                        "Existing output asset differs from immutable source: {}",
                        output.display()
                    );
                }
            } else {
                let mut staged = tempfile::NamedTempFile::new_in(&output_folder)?;
                std::io::copy(&mut File::open(source)?, &mut staged)?;
                staged.as_file().sync_all()?;
                staged.persist_noclobber(&output)?;
            }
        }
        let page = block.evidence.first().map(|e| e.page).unwrap_or(0);
        if config.output.page_markers && page > 0 && page != previous_page && !block.continuation {
            writeln!(
                temp,
                "\n<!-- Source page {page}; placed at the next logical block boundary. -->"
            )?;
            previous_page = page;
        }
        let content = render_content(&block.content).map_err(anyhow::Error::msg)?;
        let is_list = matches!(block.content, Content::ListItem { .. });
        if ordinal > 0 {
            write!(
                temp,
                "{}",
                if block.continuation {
                    if block.tight_join { "" } else { " " }
                } else if previous_list && is_list {
                    "\n"
                } else {
                    "\n\n"
                }
            )?;
        }
        write!(temp, "{}", content.trim_end_matches('\n'))?;
        previous_list = is_list;
        if config.output.review_notes && !block.findings.is_empty() {
            review_notes.push(format!(
                "Block {} (page {}): {}",
                ordinal + 1,
                page,
                block.findings.join("; ")
            ));
        }
        library.progress(job, "exporting", ordinal as u64 + 1, count as u64)?;
    }
    if !review_notes.is_empty() {
        writeln!(temp, "\n\n## Review notes\n")?;
        for note in review_notes {
            writeln!(
                temp,
                "- {}",
                render_content(&Content::Prose { text: note })
                    .map_err(anyhow::Error::msg)?
                    .trim()
            )?;
        }
    }
    writeln!(temp)?;
    temp.as_file().sync_all()?;
    let digest = file_digest(temp.path())?;
    library.db.execute(
        "INSERT OR REPLACE INTO publications(job,path,digest,state) VALUES(?,?,?,'prepared')",
        params![job, target.to_string_lossy(), digest],
    )?;
    // The write transaction serializes publication with control changes. A pause
    // acknowledged before this transaction prevents publication; afterward the file
    // already exists and the completed result remains authoritative.
    let tx = library.db.unchecked_transaction()?;
    library
        .db
        .execute("UPDATE jobs SET updated=? WHERE id=?", params![now(), job])?;
    if !library.active(job)? {
        bail!("Job paused or cancelled before publication");
    }
    temp.persist_noclobber(&target)?;
    #[cfg(unix)]
    File::open(&destination)?
        .sync_all()
        .context("Markdown published, but output directory durability could not be confirmed")?;
    library.db.execute(
        "UPDATE publications SET state='published' WHERE job=?",
        [job],
    )?;
    tx.commit()?;
    index(library, job, &metadata.title)?;
    library.finish(job, status, Some(&target.to_string_lossy()), None)?;
    library.event(
        job,
        "info",
        &format!(
            "Wrote {} ({status}; {review} blocks need review)",
            target.display()
        ),
    )?;
    Ok(target)
}

pub fn index(library: &Library, job: i64, title: &str) -> Result<()> {
    let tx = library.db.unchecked_transaction()?;
    library
        .db
        .execute("DELETE FROM search_index WHERE job=?", [job])?;
    let mut statement = library
        .db
        .prepare("SELECT ordinal,payload FROM accepted WHERE job=? ORDER BY ordinal")?;
    let rows = statement.query_map([job], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))?;
    for row in rows {
        let (ordinal, payload) = row?;
        let segment: librarian_core::corpus::Segment = serde_json::from_str(&payload)?;
        library.db.execute(
            "INSERT INTO search_index(job,ordinal,title,body) VALUES(?,?,?,?)",
            params![job, ordinal, title, segment.content.searchable_text()],
        )?;
    }
    drop(statement);
    tx.commit()?;
    Ok(())
}

pub fn file_digest(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let mut hash = blake3::Hasher::new();
    let mut buffer = [0u8; 65536];
    loop {
        let n = file.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
    }
    Ok(hash.finalize().to_hex().to_string())
}
