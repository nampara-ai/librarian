CREATE TABLE IF NOT EXISTS run_queue (
  run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at TEXT NOT NULL,
  locked_at TEXT,
  locked_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_queue_claim
ON run_queue(status, available_at, updated_at);
