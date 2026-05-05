PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  source_path TEXT NOT NULL,
  filename TEXT NOT NULL,
  media_type TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  text TEXT NOT NULL,
  start_char INTEGER NOT NULL,
  end_char INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  UNIQUE(document_id, ordinal)
);

CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  total_chunks INTEGER NOT NULL,
  completed_chunks INTEGER NOT NULL,
  failed_chunks INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  error TEXT
);

CREATE TABLE IF NOT EXISTS cleaned_outputs (
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  model_provider TEXT NOT NULL,
  model_name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(document_id, run_id)
);

CREATE TABLE IF NOT EXISTS classifications (
  document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  code TEXT NOT NULL,
  label TEXT NOT NULL,
  summary TEXT NOT NULL,
  taxonomy TEXT NOT NULL,
  confidence REAL
);

CREATE VIRTUAL TABLE IF NOT EXISTS cleaned_outputs_fts
USING fts5(document_id UNINDEXED, run_id UNINDEXED, text);
