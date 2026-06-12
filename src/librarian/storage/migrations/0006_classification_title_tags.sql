ALTER TABLE classifications ADD COLUMN title TEXT;

ALTER TABLE classifications ADD COLUMN tags TEXT NOT NULL DEFAULT '[]';
