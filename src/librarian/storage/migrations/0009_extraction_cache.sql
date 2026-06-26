-- Content-hash extraction cache.
--
-- Caches extracted Markdown keyed by the source file's SHA-256 digest plus a
-- signature of the extraction configuration (engine + options that affect the
-- output). Unlike the document-level dedup, this is config-aware: changing the
-- PDF engine or OCR settings produces a new signature and re-extracts, and the
-- cache is shared across documents that happen to have identical bytes.
CREATE TABLE IF NOT EXISTS extraction_cache (
  content_sha256 TEXT NOT NULL,
  config_signature TEXT NOT NULL,
  source_extension TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (content_sha256, config_signature)
);
