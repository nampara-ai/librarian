CREATE TABLE IF NOT EXISTS api_audit_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event TEXT NOT NULL,
  method TEXT NOT NULL,
  path TEXT NOT NULL,
  client_host TEXT NOT NULL,
  credential_present INTEGER NOT NULL DEFAULT 0,
  credential_scope TEXT,
  retry_after_seconds INTEGER,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_api_audit_events_created_at
ON api_audit_events(created_at);

CREATE INDEX IF NOT EXISTS idx_api_audit_events_event
ON api_audit_events(event);
