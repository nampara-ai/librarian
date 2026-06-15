ALTER TABLE classifications ADD COLUMN issuer TEXT;

ALTER TABLE classifications ADD COLUMN series_key TEXT;

ALTER TABLE classifications ADD COLUMN series_title TEXT;

ALTER TABLE classifications ADD COLUMN period TEXT;

CREATE INDEX IF NOT EXISTS idx_classifications_series_key
  ON classifications (series_key);
