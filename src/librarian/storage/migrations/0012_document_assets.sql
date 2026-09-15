-- Binary assets referenced by extracted Markdown. The asset-set marker records
-- a completed extraction even when a document contains no assets, allowing old
-- rows to be upgraded exactly once without repeatedly re-running extraction.
CREATE TABLE IF NOT EXISTS document_asset_sets (
  document_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_assets (
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  media_type TEXT NOT NULL,
  data BLOB NOT NULL,
  sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (document_id, filename)
);

CREATE INDEX IF NOT EXISTS idx_document_assets_document_id
  ON document_assets(document_id);
