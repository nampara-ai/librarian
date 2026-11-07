use anyhow::{Context, Result, bail};
use fs2::FileExt;
use librarian_core::corpus::{Metadata, Segment};
pub use rusqlite::params as sql_params;
use rusqlite::{Connection, OptionalExtension, params};
use serde::{Deserialize, Serialize};
use std::{
    fs::{File, OpenOptions},
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

pub fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Job {
    pub id: i64,
    pub source: String,
    pub destination: String,
    pub status: String,
    pub stage: String,
    pub done: i64,
    pub total: i64,
    pub document: Option<String>,
    pub output: Option<String>,
    pub error: Option<String>,
    pub requests: i64,
    pub cache_hits: i64,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cached_tokens: i64,
    pub cost: Option<f64>,
    pub created: i64,
    pub updated: i64,
}

pub struct Library {
    pub root: PathBuf,
    pub db: Connection,
}
pub struct WorkerLock {
    _file: File,
}

impl Library {
    pub fn open(root: &Path) -> Result<Self> {
        std::fs::create_dir_all(root)?;
        let root = root.canonicalize()?;
        for folder in ["originals", "assets", "engines", "scratch"] {
            std::fs::create_dir_all(root.join(folder))?;
        }
        let db = Connection::open(root.join("library.sqlite3"))?;
        db.busy_timeout(Duration::from_secs(15))?;
        db.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON; PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
            INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
            CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, destination TEXT NOT NULL,
              config TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', stage TEXT NOT NULL DEFAULT 'queued', done INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
              document TEXT, output TEXT, error TEXT, requests INTEGER NOT NULL DEFAULT 0, cache_hits INTEGER NOT NULL DEFAULT 0, input_tokens INTEGER NOT NULL DEFAULT 0,
              output_tokens INTEGER NOT NULL DEFAULT 0, cached_tokens INTEGER NOT NULL DEFAULT 0, cost REAL, created INTEGER NOT NULL, updated INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, digest TEXT NOT NULL, original TEXT NOT NULL, format TEXT NOT NULL, metadata TEXT NOT NULL,
              extraction_complete INTEGER NOT NULL DEFAULT 0, extraction_key TEXT NOT NULL, created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS job_inputs(job INTEGER PRIMARY KEY REFERENCES jobs(id), digest TEXT NOT NULL, original TEXT NOT NULL, format TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations(document TEXT NOT NULL REFERENCES documents(id), ordinal INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(document,ordinal));
            CREATE TABLE IF NOT EXISTS segments(document TEXT NOT NULL REFERENCES documents(id), ordinal INTEGER NOT NULL, source TEXT NOT NULL, PRIMARY KEY(document,ordinal));
            CREATE TABLE IF NOT EXISTS accepted(job INTEGER NOT NULL REFERENCES jobs(id), ordinal INTEGER NOT NULL, payload TEXT NOT NULL, needs_review INTEGER NOT NULL, PRIMARY KEY(job,ordinal));
            CREATE TABLE IF NOT EXISTS job_metadata(job INTEGER PRIMARY KEY REFERENCES jobs(id), payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS planned(job INTEGER NOT NULL REFERENCES jobs(id), ordinal INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(job,ordinal));
            CREATE TABLE IF NOT EXISTS publications(job INTEGER PRIMARY KEY REFERENCES jobs(id), path TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS interpreted(document TEXT NOT NULL REFERENCES documents(id), ordinal INTEGER NOT NULL, profile TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(document,ordinal,profile));
            CREATE TABLE IF NOT EXISTS inference_cache(key TEXT PRIMARY KEY, payload TEXT NOT NULL, created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, job INTEGER, level TEXT NOT NULL, message TEXT NOT NULL, created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, job INTEGER NOT NULL, cache_key TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, created INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS request_reservations(attempt INTEGER PRIMARY KEY REFERENCES attempts(id), job INTEGER NOT NULL REFERENCES jobs(id), output_tokens INTEGER NOT NULL, cost REAL, settled INTEGER NOT NULL DEFAULT 0);
            CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(job UNINDEXED, ordinal UNINDEXED, title, body, tokenize='unicode61 remove_diacritics 2');")?;
        let version: i64 = db.query_row("SELECT version FROM schema_version", [], |r| r.get(0))?;
        db.execute_batch("CREATE TABLE IF NOT EXISTS editorial_runs(job INTEGER PRIMARY KEY REFERENCES jobs(id), version TEXT NOT NULL, extraction_done INTEGER NOT NULL DEFAULT 0, extraction_error TEXT, provisional TEXT);
            CREATE TABLE IF NOT EXISTS editorial_pages(job INTEGER NOT NULL REFERENCES jobs(id), page INTEGER NOT NULL, observation INTEGER NOT NULL, part INTEGER NOT NULL, image TEXT NOT NULL, native TEXT NOT NULL, ocr TEXT, reading TEXT, cleaned TEXT, PRIMARY KEY(job,page), UNIQUE(job,observation,part));
            CREATE TABLE IF NOT EXISTS editorial_observations(job INTEGER NOT NULL, ordinal INTEGER NOT NULL, PRIMARY KEY(job,ordinal));
            CREATE TABLE IF NOT EXISTS editorial_stages(job INTEGER NOT NULL REFERENCES jobs(id), stage TEXT NOT NULL, unit TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(job,stage,unit));
            CREATE TABLE IF NOT EXISTS category_sequences(class TEXT PRIMARY KEY, next INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS accessions(job INTEGER PRIMARY KEY REFERENCES jobs(id), class TEXT NOT NULL, sequence INTEGER NOT NULL);")?;
        db.execute_batch("CREATE TABLE IF NOT EXISTS editorial_sources(job INTEGER NOT NULL REFERENCES jobs(id), id TEXT NOT NULL, page INTEGER NOT NULL, kind TEXT NOT NULL, parent TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL, sha256 TEXT NOT NULL, content TEXT NOT NULL, PRIMARY KEY(job,id));
            CREATE INDEX IF NOT EXISTS editorial_sources_page ON editorial_sources(job,page,kind);
            CREATE TABLE IF NOT EXISTS editorial_source_links(job INTEGER NOT NULL,child TEXT NOT NULL,parent TEXT NOT NULL,relation TEXT NOT NULL,PRIMARY KEY(job,child,parent,relation));
            CREATE TABLE IF NOT EXISTS editorial_packets(job INTEGER NOT NULL, stage TEXT NOT NULL, unit TEXT NOT NULL, round INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(job,stage,unit,round));
            CREATE TABLE IF NOT EXISTS editorial_results(job INTEGER NOT NULL, stage TEXT NOT NULL, unit TEXT NOT NULL, input_hash TEXT NOT NULL, markdown TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(job,stage,unit));
            CREATE TABLE IF NOT EXISTS editorial_ledger(job INTEGER NOT NULL, stage TEXT NOT NULL, category TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL, updated INTEGER NOT NULL, PRIMARY KEY(job,stage,category,key));
            CREATE TABLE IF NOT EXISTS editorial_coverage(job INTEGER NOT NULL, stage TEXT NOT NULL, unit TEXT NOT NULL, source_id TEXT NOT NULL, disposition TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(job,stage,unit,source_id));
            CREATE TABLE IF NOT EXISTS editorial_owned(job INTEGER NOT NULL, stage TEXT NOT NULL, unit TEXT NOT NULL, source_id TEXT NOT NULL, is_primary INTEGER NOT NULL, status TEXT NOT NULL, PRIMARY KEY(job,stage,unit,source_id));
            CREATE UNIQUE INDEX IF NOT EXISTS editorial_unique_ownership ON editorial_owned(job,stage,source_id) WHERE status='complete' AND is_primary=1;
            CREATE TABLE IF NOT EXISTS editorial_revisions(id INTEGER PRIMARY KEY, job INTEGER NOT NULL, unit TEXT NOT NULL, target TEXT NOT NULL, before_hash TEXT NOT NULL, after_hash TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS editorial_interventions(id INTEGER PRIMARY KEY, job INTEGER NOT NULL, stage TEXT NOT NULL, unit TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS editorial_feedback(id INTEGER PRIMARY KEY, job INTEGER NOT NULL REFERENCES jobs(id), segment TEXT NOT NULL, original_source TEXT NOT NULL, model_text TEXT NOT NULL, corrected_text TEXT NOT NULL, reason TEXT NOT NULL, created INTEGER NOT NULL);")?;
        if version != 1 {
            bail!("Library schema {version} is unsupported by this binary");
        }
        Ok(Self { root, db })
    }
    pub fn lock_worker(&self) -> Result<WorkerLock> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(self.root.join("worker.lock"))?;
        file.try_lock_exclusive()
            .context("Another Librarian worker is already processing this library")?;
        // Acquiring the OS lock establishes that no previous worker remains alive.
        self.db.execute(
            "UPDATE jobs SET status='queued', stage='recovering', updated=? WHERE status='running'",
            [now()],
        )?;
        Ok(WorkerLock { _file: file })
    }
    pub fn worker_running(&self) -> Result<bool> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(self.root.join("worker.lock"))?;
        match file.try_lock_exclusive() {
            Ok(()) => Ok(false),
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => Ok(true),
            Err(e) => Err(e.into()),
        }
    }
    pub fn enqueue(
        &self,
        source: &Path,
        destination: &Path,
        config: &crate::config::Config,
    ) -> Result<i64> {
        self.db.execute(
            "INSERT INTO jobs(source,destination,config,created,updated) VALUES(?,?,?,?,?)",
            params![
                source.to_string_lossy(),
                destination.to_string_lossy(),
                serde_json::to_string(config)?,
                now(),
                now()
            ],
        )?;
        Ok(self.db.last_insert_rowid())
    }
    pub fn config(&self, id: i64) -> Result<crate::config::Config> {
        let s: String = self
            .db
            .query_row("SELECT config FROM jobs WHERE id=?", [id], |r| r.get(0))?;
        let value: serde_json::Value = serde_json::from_str(&s)?;
        let mut config: crate::config::Config = serde_json::from_value(value.clone())?;
        if value["processing"].get("pipeline").is_none() {
            config.processing.pipeline = "archival".into();
        }
        Ok(config)
    }
    pub fn jobs(&self, limit: usize) -> Result<Vec<Job>> {
        let mut q = self.db.prepare("SELECT id,source,destination,status,stage,done,total,document,output,error,requests,cache_hits,input_tokens,output_tokens,cached_tokens,cost,created,updated FROM jobs ORDER BY id DESC LIMIT ?")?;
        Ok(q.query_map([limit as i64], job_row)?
            .collect::<rusqlite::Result<Vec<_>>>()?)
    }
    pub fn job(&self, id: i64) -> Result<Job> {
        Ok(self.db.query_row("SELECT id,source,destination,status,stage,done,total,document,output,error,requests,cache_hits,input_tokens,output_tokens,cached_tokens,cost,created,updated FROM jobs WHERE id=?",[id],job_row)?)
    }
    pub fn next_job(&self) -> Result<Option<i64>> {
        Ok(self
            .db
            .query_row(
                "SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1",
                [],
                |r| r.get(0),
            )
            .optional()?)
    }
    pub fn control(&self, id: Option<i64>, action: &str) -> Result<usize> {
        let (status, eligible) = match action {
            "pause" => ("paused", "('queued','running')"),
            "cancel" => ("cancelled", "('queued','running','paused')"),
            "resume" => (
                "queued",
                "('paused','cancelled','failed','budget_exhausted')",
            ),
            _ => bail!("Unknown job action"),
        };
        Ok(self.db.execute(&format!("UPDATE jobs SET status=?,updated=? WHERE status IN {eligible} AND (? IS NULL OR id=?)"), params![status,now(),id,id])?)
    }
    pub fn active(&self, id: i64) -> Result<bool> {
        Ok(self.job(id)?.status == "running")
    }
    pub fn start(&self, id: i64) -> Result<()> {
        self.db.execute(
            "UPDATE jobs SET status='running',error=NULL,updated=? WHERE id=? AND status='queued'",
            params![now(), id],
        )?;
        Ok(())
    }
    pub fn progress(&self, id: i64, stage: &str, done: u64, total: u64) -> Result<()> {
        self.db.execute(
            "UPDATE jobs SET stage=?,done=?,total=?,updated=? WHERE id=? AND status='running'",
            params![
                stage,
                i64::try_from(done)?,
                i64::try_from(total)?,
                now(),
                id
            ],
        )?;
        Ok(())
    }
    pub fn event(&self, id: i64, level: &str, message: &str) -> Result<()> {
        self.db.execute(
            "INSERT INTO events(job,level,message,created) VALUES(?,?,?,?)",
            params![id, level, message, now()],
        )?;
        Ok(())
    }
    pub fn finish(
        &self,
        id: i64,
        status: &str,
        output: Option<&str>,
        error: Option<&str>,
    ) -> Result<()> {
        self.db.execute("UPDATE jobs SET status=?,stage=?,output=COALESCE(?,output),error=?,updated=? WHERE id=? AND status='running'",params![status,status,output,error,now(),id])?;
        Ok(())
    }
    pub fn events(&self, id: Option<i64>, limit: usize) -> Result<Vec<(i64, String, String)>> {
        let mut s = self.db.prepare("SELECT created,level,message FROM events WHERE (? IS NULL OR job=?) ORDER BY id DESC LIMIT ?")?;
        Ok(s.query_map(params![id, id, limit as i64], |r| {
            Ok((r.get(0)?, r.get(1)?, r.get(2)?))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?)
    }
    pub fn metadata(&self, document: &str) -> Result<Metadata> {
        let s: String = self.db.query_row(
            "SELECT metadata FROM documents WHERE id=?",
            [document],
            |r| r.get(0),
        )?;
        Ok(serde_json::from_str(&s)?)
    }
    pub fn segment(&self, document: &str, ordinal: u64) -> Result<Option<Segment>> {
        let raw: Option<String> = self
            .db
            .query_row(
                "SELECT source FROM segments WHERE document=? AND ordinal=?",
                params![document, i64::try_from(ordinal)?],
                |r| r.get(0),
            )
            .optional()?;
        raw.map(|s| serde_json::from_str(&s).map_err(Into::into))
            .transpose()
    }
    pub fn segment_count(&self, document: &str) -> Result<u64> {
        Ok(self.db.query_row(
            "SELECT count(*) FROM segments WHERE document=?",
            [document],
            |r| r.get::<_, i64>(0),
        )? as u64)
    }
    pub fn accepted(&self, job: i64, ordinal: u64) -> Result<Option<Segment>> {
        let raw: Option<String> = self
            .db
            .query_row(
                "SELECT payload FROM accepted WHERE job=? AND ordinal=?",
                params![job, i64::try_from(ordinal)?],
                |r| r.get(0),
            )
            .optional()?;
        raw.map(|s| serde_json::from_str(&s).map_err(Into::into))
            .transpose()
    }
    pub fn accept(&self, job: i64, ordinal: u64, segment: &Segment) -> Result<bool> {
        let changed = self.db.execute("INSERT OR REPLACE INTO accepted(job,ordinal,payload,needs_review) SELECT ?,?,?,? WHERE EXISTS(SELECT 1 FROM jobs WHERE id=? AND status='running')", params![job,i64::try_from(ordinal)?,serde_json::to_string(segment)?,!segment.findings.is_empty(),job])?;
        Ok(changed != 0)
    }
    pub fn cache(&self, key: &str, ttl_hours: u64) -> Result<Option<serde_json::Value>> {
        if ttl_hours == 0 {
            return Ok(None);
        }
        let raw: Option<String> = self
            .db
            .query_row(
                "SELECT payload FROM inference_cache WHERE key=? AND created>=?",
                params![
                    key,
                    now()
                        .saturating_sub(ttl_hours.saturating_mul(3600).min(i64::MAX as u64) as i64)
                ],
                |r| r.get(0),
            )
            .optional()?;
        raw.map(|s| serde_json::from_str(&s).map_err(Into::into))
            .transpose()
    }
    pub fn cache_put(&self, key: &str, payload: &serde_json::Value) -> Result<()> {
        self.db.execute(
            "INSERT OR REPLACE INTO inference_cache(key,payload,created) VALUES(?,?,?)",
            params![key, serde_json::to_string(payload)?, now()],
        )?;
        Ok(())
    }
    pub fn search(&self, query: &str, limit: usize) -> Result<Vec<serde_json::Value>> {
        // Treat the query as ordinary words, not raw FTS syntax.
        let query = query
            .split_whitespace()
            .map(|s| format!("\"{}\"", s.replace('"', "\"\"")))
            .collect::<Vec<_>>()
            .join(" AND ");
        if query.is_empty() {
            return Ok(vec![]);
        }
        let mut q = self.db.prepare("SELECT job,ordinal,title,snippet(search_index,3,'[',']',' … ',32) FROM search_index WHERE search_index MATCH ? ORDER BY rank LIMIT ?")?;
        Ok(q.query_map(params![query,limit.min(1000) as i64],|r|Ok(serde_json::json!({"job":r.get::<_,i64>(0)?,"block":r.get::<_,i64>(1)?,"title":r.get::<_,String>(2)?,"snippet":r.get::<_,String>(3)?})))?.collect::<rusqlite::Result<Vec<_>>>()?)
    }
}

fn job_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<Job> {
    Ok(Job {
        id: r.get(0)?,
        source: r.get(1)?,
        destination: r.get(2)?,
        status: r.get(3)?,
        stage: r.get(4)?,
        done: r.get(5)?,
        total: r.get(6)?,
        document: r.get(7)?,
        output: r.get(8)?,
        error: r.get(9)?,
        requests: r.get(10)?,
        cache_hits: r.get(11)?,
        input_tokens: r.get(12)?,
        output_tokens: r.get(13)?,
        cached_tokens: r.get(14)?,
        cost: r.get(15)?,
        created: r.get(16)?,
        updated: r.get(17)?,
    })
}
