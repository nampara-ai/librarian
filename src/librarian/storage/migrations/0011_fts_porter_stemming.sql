-- Rebuild the full-text indexes with Porter stemming so queries match
-- inflected forms (search "dividends" -> matches "dividend", "running" ->
-- "run"). The tables are repopulated from their source-of-truth rows, so no
-- content is lost; only the tokenizer changes.

DROP TABLE IF EXISTS cleaned_outputs_fts;
CREATE VIRTUAL TABLE cleaned_outputs_fts
USING fts5(
  document_id UNINDEXED,
  run_id UNINDEXED,
  text,
  tokenize = 'porter unicode61'
);
INSERT INTO cleaned_outputs_fts (document_id, run_id, text)
SELECT document_id, run_id, text FROM cleaned_outputs;

DROP TABLE IF EXISTS raw_content_fts;
CREATE VIRTUAL TABLE raw_content_fts
USING fts5(
  document_id UNINDEXED,
  text,
  tokenize = 'porter unicode61'
);
INSERT INTO raw_content_fts (document_id, text)
SELECT substr(key, 5), text
FROM content_blobs
WHERE key LIKE 'raw:doc_%';
