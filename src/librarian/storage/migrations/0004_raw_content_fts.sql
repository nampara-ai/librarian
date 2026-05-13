CREATE VIRTUAL TABLE IF NOT EXISTS raw_content_fts
USING fts5(document_id UNINDEXED, text);

INSERT INTO raw_content_fts (document_id, text)
SELECT substr(key, 5), text
FROM content_blobs
WHERE key LIKE 'raw:doc_%'
  AND NOT EXISTS (
    SELECT 1
    FROM raw_content_fts
    WHERE raw_content_fts.document_id = substr(content_blobs.key, 5)
  );
