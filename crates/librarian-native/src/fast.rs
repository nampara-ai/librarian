//! The fast pipeline's cleaning stage. Every passage is one bounded request;
//! the requests run side by side; each response is accounted for word by word
//! before it may replace its source. A response that is not an edit of its
//! passage is refused, the passage is tried again in two halves, and a half
//! that is refused again is kept as extracted and marked.
use crate::{
    config::Config,
    pipeline::job_segment,
    provider::{ModelClient, OutputLimit, Request},
    store::{Library, now},
};
use anyhow::{Context, Result};
use librarian_core::{
    clean::{self, Ledger, Verdict},
    corpus::Segment,
};
use rusqlite::params;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::sync::atomic::{AtomicBool, Ordering};

#[derive(Debug)]
struct Refused(String);
impl std::fmt::Display for Refused {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}
impl std::error::Error for Refused {}

/// Replace the job's plan with passages. The plan is a function of the
/// extracted blocks and the chunk size, so a resumed job plans the same rows.
pub fn plan(library: &Library, job: i64, blocks: Vec<Segment>, limit: usize) -> Result<u64> {
    let passages = clean::plan(blocks, limit).map_err(anyhow::Error::msg)?;
    let transaction = library.db.unchecked_transaction()?;
    for (ordinal, passage) in passages.iter().enumerate() {
        library.db.execute(
            "INSERT OR REPLACE INTO planned(job,ordinal,payload) VALUES(?,?,?)",
            params![job, ordinal as i64, serde_json::to_string(passage)?],
        )?;
    }
    library.db.execute(
        "DELETE FROM planned WHERE job=? AND ordinal>=?",
        params![job, passages.len() as i64],
    )?;
    transaction.commit()?;
    Ok(passages.len() as u64)
}

struct Repaired {
    text: String,
    ledger: Ledger,
    refused: Vec<String>,
}

struct Task<'a> {
    library: &'a Library,
    job: i64,
    client: &'a ModelClient,
    config: &'a Config,
    instructions: &'a str,
}

fn edge(text: &str, tail: bool) -> String {
    const WORDS: usize = 60;
    let words: Vec<&str> = text.split_whitespace().collect();
    if tail {
        words[words.len().saturating_sub(WORDS)..].join(" ")
    } else {
        words[..words.len().min(WORDS)].join(" ")
    }
}

impl Task<'_> {
    /// One request. `Refused` means the response was not an edit of the passage.
    async fn attempt(&self, before: &str, passage: &str, after: &str) -> Result<(String, Ledger)> {
        let effort = self.config.provider.reasoning_effort.as_str();
        let reasoning = match effort {
            "none" => 0,
            "minimal" | "low" => 1024,
            _ => 4096,
        };
        // A repaired passage is about as long as its source.
        let cap = passage.len() / 3 * 13 / 10 + 256 + reasoning;
        let check = |value: &Value| -> Result<(String, Ledger)> {
            let text = clean::unwrap(value.as_str().unwrap_or_default()).map_err(Refused)?;
            let (text, ledger) = clean::settle(before, passage, &text, after);
            if let Verdict::Reject(reason) = clean::verdict(&ledger) {
                return Err(Refused(reason).into());
            }
            Ok((text, ledger))
        };
        let value = self
            .client
            .request_bounded(
                self.library,
                self.job,
                Request {
                    task: "fast_clean",
                    instructions: self.instructions,
                    context: None,
                    input: Value::String(clean::envelope(before, passage, after)),
                    schema: Value::Null,
                    image: None,
                    validate: &|value| check(value).map(|_| ()),
                },
                Some(cap),
            )
            .await
            .map_err(|error| {
                if error.downcast_ref::<OutputLimit>().is_some() {
                    Refused("the response reached the output limit before it was complete".into())
                        .into()
                } else {
                    error
                }
            })?;
        check(&value)
    }

    async fn repair(
        &self,
        before: &str,
        passage: &str,
        after: &str,
        halves: bool,
    ) -> Result<Repaired> {
        let reason = match self.attempt(before, passage, after).await {
            Ok((text, ledger)) => {
                return Ok(Repaired {
                    text,
                    ledger,
                    refused: vec![],
                });
            }
            Err(error) => match error.downcast::<Refused>() {
                Ok(refused) => refused.0,
                Err(error) => return Err(error),
            },
        };
        let cut = if halves && passage.len() > 600 {
            librarian_core::corpus::split_text(passage, passage.len().div_ceil(2).max(4))
                .first()
                .map(|range| range.1)
                .filter(|cut| *cut > 0 && *cut < passage.len())
        } else {
            None
        };
        let Some(cut) = cut else {
            // Nothing is lost: the passage stays as it was extracted.
            return Ok(Repaired {
                text: passage.to_owned(),
                ledger: clean::account(passage, passage),
                refused: vec![reason],
            });
        };
        self.library.event(
            self.job,
            "warning",
            &format!(
                "A cleaning response was refused ({reason}); trying the passage in two halves"
            ),
        )?;
        let (left, right) = passage.split_at(cut);
        let first = Box::pin(self.repair(before, left, &edge(right, false), false)).await?;
        let second = Box::pin(self.repair(&edge(left, true), right, after, false)).await?;
        let mut ledger = first.ledger;
        ledger.add(&second.ledger);
        ledger.changes.extend(second.ledger.changes);
        ledger.numbers_lost.extend(second.ledger.numbers_lost);
        ledger.numbers_added.extend(second.ledger.numbers_added);
        let joint = if left.trim_end_matches([' ', '\t']).ends_with('\n') {
            "\n\n"
        } else {
            " "
        };
        Ok(Repaired {
            text: format!(
                "{}{joint}{}",
                first.text.trim_end(),
                second.text.trim_start()
            ),
            ledger,
            refused: first.refused.into_iter().chain(second.refused).collect(),
        })
    }

    async fn passage(&self, ordinal: u64, segment: &Segment) -> Result<()> {
        let source = clean::tidy(segment.content.editable_text().unwrap_or_default());
        let neighbour = |ordinal: Option<u64>, tail: bool| -> Result<String> {
            Ok(match ordinal {
                Some(ordinal) => job_segment(self.library, self.job, ordinal)?
                    .map(|s| edge(&s.content.searchable_text(), tail))
                    .unwrap_or_default(),
                None => String::new(),
            })
        };
        let before = neighbour(ordinal.checked_sub(1), true)?;
        let after = neighbour(Some(ordinal + 1), false)?;
        let repaired = self.repair(&before, &source, &after, true).await?;
        let mut accepted = segment.clone();
        accepted.content = segment.content.with_edit(repaired.text);
        for reason in &repaired.refused {
            accepted.findings.push(format!(
                "Kept as extracted. The cleaning response was refused: {reason}"
            ));
        }
        let notes = match clean::verdict(&repaired.ledger) {
            Verdict::Review(notes) => notes,
            _ => vec![],
        };
        let record = Record {
            ledger: repaired.ledger,
            notes,
            refused: repaired.refused,
        };
        let transaction = self.library.db.unchecked_transaction()?;
        if self.library.accept(self.job, ordinal, &accepted)? {
            self.library.db.execute(
                "INSERT OR REPLACE INTO clean_ledger(job,ordinal,payload,created) VALUES(?,?,?,?)",
                params![
                    self.job,
                    ordinal as i64,
                    serde_json::to_string(&record)?,
                    now()
                ],
            )?;
        }
        transaction.commit()?;
        Ok(())
    }
}

/// What the cleaning of one passage did, as stored.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Record {
    pub ledger: Ledger,
    pub notes: Vec<String>,
    pub refused: Vec<String>,
}

/// The account of a job: totals, and the passages an owner may want to read.
#[derive(Debug, Clone, Default, Serialize)]
pub struct Report {
    pub passages: u64,
    pub totals: Ledger,
    pub kept_as_extracted: u64,
    pub items: Vec<Item>,
}
#[derive(Debug, Clone, Default, Serialize)]
pub struct Item {
    pub passage: u64,
    pub notes: Vec<String>,
    pub refused: Vec<String>,
    pub changes: Vec<clean::Change>,
}

pub fn report(library: &Library, job: i64) -> Result<Report> {
    let mut query = library
        .db
        .prepare("SELECT ordinal,payload FROM clean_ledger WHERE job=? ORDER BY ordinal")?;
    let rows = query.query_map([job], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?)))?;
    let mut report = Report::default();
    for row in rows {
        let (ordinal, payload) = row?;
        let record: Record = serde_json::from_str(&payload)?;
        report.passages += 1;
        report.totals.add(&record.ledger);
        report.kept_as_extracted += u64::from(!record.refused.is_empty());
        let changes: Vec<clean::Change> = record
            .ledger
            .changes
            .into_iter()
            .filter(|c| c.kind != "inserted" || c.after.split_whitespace().count() >= 3)
            .collect();
        if !(record.notes.is_empty() && record.refused.is_empty() && changes.is_empty()) {
            report.items.push(Item {
                passage: ordinal as u64 + 1,
                notes: record.notes,
                refused: record.refused,
                changes,
            });
        }
    }
    Ok(report)
}

/// The report as plain text, for the command line and the reading screen.
pub fn report_text(report: &Report) -> String {
    let t = &report.totals;
    if report.passages == 0 {
        return "No cleaning account is recorded for this job. The account is kept by the fast pipeline.".into();
    }
    let mut text = format!(
        "Passages cleaned: {}\nSource words: {}\nWords in the result: {}\n\n\
         Kept unchanged: {}\nSpelling repaired: {}\nSplit or joined words repaired: {}\n\
         Replaced by a different word: {}\nFillers and stutters removed: {}\n\
         Repeated runs removed: {}\nOther words removed: {}\nWords added: {}\n\
         Passages kept as extracted after a refused response: {}\n",
        report.passages,
        t.source_words,
        t.output_words,
        t.kept,
        t.respelled,
        t.joined,
        t.replaced,
        t.fillers_removed,
        t.repeats_removed,
        t.removed,
        t.inserted,
        report.kept_as_extracted,
    );
    for item in &report.items {
        text.push_str(&format!("\nPassage {}\n", item.passage));
        for reason in &item.refused {
            text.push_str(&format!("  Kept as extracted: {reason}\n"));
        }
        for note in &item.notes {
            text.push_str(&format!("  {note}\n"));
        }
        for change in &item.changes {
            match change.kind.as_str() {
                "replaced" => text.push_str(&format!(
                    "  \"{}\" became \"{}\"\n",
                    change.before, change.after
                )),
                "removed" => text.push_str(&format!("  removed \"{}\"\n", change.before)),
                _ => text.push_str(&format!("  added \"{}\"\n", change.after)),
            }
        }
    }
    text
}

/// Clean every planned passage that is not yet accepted. Accepted passages are
/// committed one by one, so a stopped job resumes with only the remainder.
pub async fn clean(
    library: &Library,
    job: i64,
    client: &ModelClient,
    config: &Config,
    count: u64,
    stop: &AtomicBool,
) -> Result<()> {
    let glossary: Vec<(String, String)> = config
        .processing
        .glossary
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let instructions = clean::instructions("", &config.processing.house_style, &glossary);
    let task = Task {
        library,
        job,
        client,
        config,
        instructions: &instructions,
    };
    let mut pending = Vec::new();
    for ordinal in 0..count {
        if library.accepted(job, ordinal)?.is_some() {
            continue;
        }
        let segment = job_segment(library, job, ordinal)?.context("Missing planned passage")?;
        let editable = segment
            .content
            .editable_text()
            .is_some_and(|text| text.chars().any(char::is_alphanumeric));
        if editable && config.processing.edit {
            pending.push((ordinal, segment));
        } else {
            library.accept(job, ordinal, &segment)?;
        }
    }
    let started = std::time::Instant::now();
    let sent = pending.len();
    use futures::StreamExt;
    let mut running = futures::stream::iter(
        pending
            .iter()
            .map(|(ordinal, segment)| task.passage(*ordinal, segment)),
    )
    .buffer_unordered(config.provider.concurrency);
    while let Some(result) = running.next().await {
        result?;
        if stop.load(Ordering::Relaxed) || !library.active(job)? {
            anyhow::bail!("Processing stopped; committed work is retained");
        }
        let done: i64 =
            library
                .db
                .query_row("SELECT count(*) FROM accepted WHERE job=?", [job], |r| {
                    r.get(0)
                })?;
        library.progress(job, "cleaning", done as u64, count)?;
    }
    drop(running);
    let report = report(library, job)?;
    let t = &report.totals;
    library.event(
        job,
        "info",
        &format!(
            "Cleaned {sent} passages in {:.1} s. Of {} source words: {} kept, {} spelling repaired, {} joined, {} replaced, {} fillers removed, {} repeats removed, {} removed; {} added. {} passages kept as extracted.",
            started.elapsed().as_secs_f64(),
            t.source_words,
            t.kept,
            t.respelled,
            t.joined,
            t.replaced,
            t.fillers_removed,
            t.repeats_removed,
            t.removed,
            t.inserted,
            report.kept_as_extracted
        ),
    )?;
    Ok(())
}
