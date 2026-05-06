PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS chunks_new (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  text TEXT NOT NULL,
  start_char INTEGER NOT NULL,
  end_char INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  UNIQUE(document_id, ordinal)
);

CREATE TEMP TABLE chunk_id_map AS
SELECT
  id AS old_id,
  'chk_' || substr(hex(randomblob(4)), 1, 8) || '_' || substr(document_id, 5, 8) || '_' || ordinal AS new_id
FROM chunks;

INSERT INTO chunks_new (id, document_id, ordinal, text, start_char, end_char, sha256)
SELECT chunk_id_map.new_id, chunks.document_id, chunks.ordinal, chunks.text,
       chunks.start_char, chunks.end_char, chunks.sha256
FROM chunks
JOIN chunk_id_map ON chunk_id_map.old_id = chunks.id;

CREATE TABLE IF NOT EXISTS cleaned_chunks_new (
  run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  chunk_id TEXT NOT NULL REFERENCES chunks_new(id) ON DELETE CASCADE,
  text TEXT NOT NULL,
  warnings TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(run_id, chunk_id)
);

INSERT INTO cleaned_chunks_new (run_id, chunk_id, text, warnings, created_at)
SELECT cleaned_chunks.run_id, chunk_id_map.new_id, cleaned_chunks.text,
       cleaned_chunks.warnings, cleaned_chunks.created_at
FROM cleaned_chunks
JOIN chunk_id_map ON chunk_id_map.old_id = cleaned_chunks.chunk_id;

DROP TABLE cleaned_chunks;
DROP TABLE chunks;
ALTER TABLE chunks_new RENAME TO chunks;
ALTER TABLE cleaned_chunks_new RENAME TO cleaned_chunks;
DROP TABLE chunk_id_map;

PRAGMA foreign_keys = ON;
