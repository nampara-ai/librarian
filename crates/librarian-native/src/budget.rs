//! Per-job ceilings: what was allowed, what was used, and how an owner raises
//! a ceiling so that a stopped job can continue. Counters are never reset.
use crate::store::{Library, now};
use anyhow::{Context, Result, bail};
use rusqlite::params;
use serde::Serialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Limit {
    Requests,
    GeneratedTokens,
    Cost,
}

impl Limit {
    /// The configuration key under `[processing]`.
    pub fn key(self) -> &'static str {
        match self {
            Limit::Requests => "max_requests",
            Limit::GeneratedTokens => "max_generated_tokens",
            Limit::Cost => "max_cost",
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            Limit::Requests => "request ceiling",
            Limit::GeneratedTokens => "output-token ceiling",
            Limit::Cost => "cost ceiling",
        }
    }
}

/// The counters a reservation is judged against.
#[derive(Debug, Clone, Copy, Default, Serialize)]
pub struct Used {
    pub requests: u64,
    pub output_tokens: u64,
    /// Output tokens held for requests whose outcome was never reported.
    pub held_tokens: u64,
    pub cost: f64,
    /// Cost held for requests whose outcome was never reported.
    pub held_cost: f64,
}

#[derive(Debug, Clone, Copy, Serialize)]
pub struct Ceilings {
    pub max_requests: u64,
    pub max_generated_tokens: u64,
    pub max_cost: Option<f64>,
}

/// The one rule for whether another request fits. `next_tokens` is the output
/// allowance of the request; `next_cost` its estimated cost when prices are set.
pub fn exceeded(
    ceilings: &Ceilings,
    used: &Used,
    next_tokens: u64,
    next_cost: Option<f64>,
) -> Option<Limit> {
    if used.requests >= ceilings.max_requests {
        return Some(Limit::Requests);
    }
    if used.output_tokens + used.held_tokens + next_tokens > ceilings.max_generated_tokens {
        return Some(Limit::GeneratedTokens);
    }
    if ceilings
        .max_cost
        .zip(next_cost)
        .is_some_and(|(cap, next)| used.cost + used.held_cost + next > cap)
    {
        return Some(Limit::Cost);
    }
    None
}

/// The message stored with a job that stops at a ceiling. The pipeline
/// recognises the prefix; the rest names the ceiling for the reader.
pub fn stop_message(limit: Limit, ceilings: &Ceilings, used: &Used) -> String {
    match limit {
        Limit::Requests => format!(
            "budget_exhausted: request ceiling reached ({} of {} requests used)",
            used.requests, ceilings.max_requests
        ),
        Limit::GeneratedTokens => format!(
            "budget_exhausted: output-token ceiling reached ({} used and {} held of {})",
            used.output_tokens, used.held_tokens, ceilings.max_generated_tokens
        ),
        Limit::Cost => format!(
            "budget_exhausted: cost ceiling reached (${:.4} used and ${:.4} held of ${:.4}; the next request would exceed it)",
            used.cost,
            used.held_cost,
            ceilings.max_cost.unwrap_or(0.0)
        ),
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct State {
    pub job: i64,
    pub status: String,
    pub ceilings: Ceilings,
    pub used: Used,
    /// Output allowance one more request would reserve.
    pub next_request_tokens: u64,
    /// Whether token prices are configured, so that cost can be limited.
    pub priced: bool,
    /// Ceilings that stop the next request.
    pub exhausted: Vec<Limit>,
}

pub fn state(library: &Library, job: i64) -> Result<State> {
    let row = library.job(job)?;
    let config = library.config(job)?;
    let (held_tokens, held_cost): (i64, f64) = library.db.query_row(
        "SELECT COALESCE(SUM(output_tokens),0),COALESCE(SUM(cost),0) FROM request_reservations WHERE job=? AND settled=0",
        [job],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    let ceilings = Ceilings {
        max_requests: config.processing.max_requests,
        max_generated_tokens: config.processing.max_generated_tokens,
        max_cost: config.processing.max_cost,
    };
    let used = Used {
        requests: row.requests.max(0) as u64,
        output_tokens: row.output_tokens.max(0) as u64,
        held_tokens: held_tokens.max(0) as u64,
        cost: row.cost.unwrap_or(0.0),
        held_cost,
    };
    let next_request_tokens = config.provider.max_output_tokens as u64;
    let mut exhausted = Vec::new();
    if used.requests >= ceilings.max_requests {
        exhausted.push(Limit::Requests);
    }
    if used.output_tokens + used.held_tokens + next_request_tokens > ceilings.max_generated_tokens {
        exhausted.push(Limit::GeneratedTokens);
    }
    // The size of the next request is not known here. A job stopped by the
    // cost ceiling says so in its error; otherwise compare what is spent.
    // A cost ceiling raised after the stop makes that error out of date.
    let raised_since_stop: bool = library.db.query_row(
        "SELECT EXISTS(SELECT 1 FROM budget_amendments WHERE job=? AND field='max_cost' AND created>=?)",
        params![job, row.updated],
        |r| r.get(0),
    )?;
    let stopped_by_cost = row.status == "budget_exhausted"
        && !raised_since_stop
        && row
            .error
            .as_deref()
            .is_some_and(|e| e.contains("cost ceiling"));
    if ceilings
        .max_cost
        .is_some_and(|cap| stopped_by_cost || used.cost + used.held_cost >= cap)
    {
        exhausted.push(Limit::Cost);
    }
    Ok(State {
        job,
        status: row.status,
        ceilings,
        used,
        next_request_tokens,
        priced: config.provider.input_price_per_million.is_some()
            && config.provider.output_price_per_million.is_some(),
        exhausted,
    })
}

/// New ceilings for one job. A field left as `None` is not changed.
#[derive(Debug, Clone, Copy, Default)]
pub struct Amendment {
    pub max_requests: Option<u64>,
    pub max_generated_tokens: Option<u64>,
    pub max_cost: Option<f64>,
}

impl Amendment {
    pub fn is_empty(&self) -> bool {
        self.max_requests.is_none()
            && self.max_generated_tokens.is_none()
            && self.max_cost.is_none()
    }
}

/// Raise ceilings of a job that is not running. The job's stored configuration
/// is changed for these keys only; its counters, cache entries and committed
/// work are untouched. Every change is recorded with the consumption at the
/// time and with who authorized it.
pub fn amend(library: &Library, job: i64, change: &Amendment, origin: &str) -> Result<State> {
    if change.is_empty() {
        bail!("Name at least one ceiling to raise");
    }
    let transaction = rusqlite::Transaction::new_unchecked(
        &library.db,
        rusqlite::TransactionBehavior::Immediate,
    )?;
    let before = state(library, job)?;
    if before.status == "running" {
        bail!("Pause job {job} before changing its ceilings");
    }
    let raw: String = library
        .db
        .query_row("SELECT config FROM jobs WHERE id=?", [job], |r| r.get(0))?;
    let mut value: serde_json::Value = serde_json::from_str(&raw)?;
    let processing = value["processing"]
        .as_object_mut()
        .context("Job has no stored processing configuration")?;
    let mut records: Vec<(Limit, String, String)> = Vec::new();
    if let Some(next) = change.max_requests {
        if next <= before.ceilings.max_requests {
            bail!(
                "The request ceiling is {}; a new ceiling must be higher",
                before.ceilings.max_requests
            );
        }
        processing.insert("max_requests".into(), next.into());
        records.push((
            Limit::Requests,
            before.ceilings.max_requests.to_string(),
            next.to_string(),
        ));
    }
    if let Some(next) = change.max_generated_tokens {
        if next <= before.ceilings.max_generated_tokens {
            bail!(
                "The output-token ceiling is {}; a new ceiling must be higher",
                before.ceilings.max_generated_tokens
            );
        }
        processing.insert("max_generated_tokens".into(), next.into());
        records.push((
            Limit::GeneratedTokens,
            before.ceilings.max_generated_tokens.to_string(),
            next.to_string(),
        ));
    }
    if let Some(next) = change.max_cost {
        let Some(previous) = before.ceilings.max_cost else {
            bail!("Job {job} has no cost ceiling to raise");
        };
        if !next.is_finite() || next <= previous {
            bail!("The cost ceiling is ${previous:.4}; a new ceiling must be higher");
        }
        processing.insert("max_cost".into(), next.into());
        records.push((Limit::Cost, format!("{previous}"), format!("{next}")));
    }
    // The amended configuration must still be one the program accepts.
    let amended: crate::config::Config = serde_json::from_value(value.clone())?;
    amended.validate()?;
    // `updated` is left alone: it records when the job stopped.
    library.db.execute(
        "UPDATE jobs SET config=? WHERE id=?",
        params![serde_json::to_string(&value)?, job],
    )?;
    for (limit, previous, next) in &records {
        library.db.execute(
            "INSERT INTO budget_amendments(job,field,previous,amended,requests,output_tokens,cost,origin,created) VALUES(?,?,?,?,?,?,?,?,?)",
            params![
                job,
                limit.key(),
                previous,
                next,
                before.used.requests as i64,
                before.used.output_tokens as i64,
                library.job(job)?.cost,
                origin,
                now()
            ],
        )?;
        library.db.execute(
            "INSERT INTO events(job,level,message,created) VALUES(?,?,?,?)",
            params![
                job,
                "budget",
                format!(
                    "{} raised from {previous} to {next}, authorized {origin}. Used so far: {} requests, {} output tokens{}. Counters were not reset.",
                    limit.key(),
                    before.used.requests,
                    before.used.output_tokens,
                    library
                        .job(job)?
                        .cost
                        .map(|c| format!(", ${c:.4}"))
                        .unwrap_or_default()
                ),
                now()
            ],
        )?;
    }
    transaction.commit()?;
    state(library, job)
}

/// The recorded amendments of a job, oldest first.
pub fn amendments(library: &Library, job: i64) -> Result<Vec<serde_json::Value>> {
    let mut query = library.db.prepare(
        "SELECT field,previous,amended,requests,output_tokens,cost,origin,created FROM budget_amendments WHERE job=? ORDER BY id",
    )?;
    let rows = query
        .query_map([job], |r| {
            Ok(serde_json::json!({
                "ceiling": r.get::<_, String>(0)?,
                "previous": r.get::<_, String>(1)?,
                "amended": r.get::<_, String>(2)?,
                "requests_used": r.get::<_, i64>(3)?,
                "output_tokens_used": r.get::<_, i64>(4)?,
                "cost_used": r.get::<_, Option<f64>>(5)?,
                "authorized": r.get::<_, String>(6)?,
                "at": r.get::<_, i64>(7)?,
            }))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(rows)
}

#[derive(Debug, Default, Serialize)]
pub struct Resumed {
    pub resumed: usize,
    /// Jobs left as they are because a ceiling still stops their next request.
    pub blocked: Vec<State>,
}

/// Return paused, cancelled and failed jobs to the queue. A job stopped at a
/// ceiling is returned only if its ceilings now allow another request;
/// otherwise it is reported and left as it is, because it would stop again at
/// once.
pub fn resume(library: &Library, id: Option<i64>) -> Result<Resumed> {
    let mut outcome = Resumed::default();
    let candidates: Vec<i64> = {
        let mut query = library.db.prepare(
            "SELECT id FROM jobs WHERE status IN ('paused','cancelled','failed','budget_exhausted') AND (?1 IS NULL OR id=?1) ORDER BY id",
        )?;
        query
            .query_map([id], |r| r.get(0))?
            .collect::<rusqlite::Result<Vec<_>>>()?
    };
    for job in candidates {
        let current = state(library, job)?;
        if current.status == "budget_exhausted" && !current.exhausted.is_empty() {
            outcome.blocked.push(current);
            continue;
        }
        outcome.resumed += library.db.execute(
            "UPDATE jobs SET status='queued',updated=? WHERE id=? AND status IN ('paused','cancelled','failed','budget_exhausted')",
            params![now(), job],
        )?;
    }
    Ok(outcome)
}

/// One sentence per ceiling that stops the job, for a person to read.
pub fn explain(state: &State) -> String {
    state
        .exhausted
        .iter()
        .map(|limit| match limit {
            Limit::Requests => format!(
                "The request ceiling is reached: {} of {} requests are used.",
                state.used.requests, state.ceilings.max_requests
            ),
            Limit::GeneratedTokens => format!(
                "The output-token ceiling is reached: {} of {} tokens are used{}, and one more request needs room for {}.",
                state.used.output_tokens,
                state.ceilings.max_generated_tokens,
                if state.used.held_tokens > 0 {
                    format!(" and {} are held", state.used.held_tokens)
                } else {
                    String::new()
                },
                state.next_request_tokens
            ),
            Limit::Cost => format!(
                "The cost ceiling is reached: ${:.4} of ${:.4} is used{}.",
                state.used.cost,
                state.ceilings.max_cost.unwrap_or(0.0),
                if state.used.held_cost > 0.0 {
                    format!(" and ${:.4} is held", state.used.held_cost)
                } else {
                    String::new()
                }
            ),
        })
        .collect::<Vec<_>>()
        .join(" ")
}
