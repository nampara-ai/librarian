-- Indexes for the hot read paths that previously forced table scans.

-- list_events / SSE streaming: filter by run_id, order by id.
CREATE INDEX IF NOT EXISTS idx_run_events_run_id
  ON run_events(run_id, id);

-- Runs by document (history, reprocess lookups) and the runs list ordering.
CREATE INDEX IF NOT EXISTS idx_runs_document_id
  ON runs(document_id);
CREATE INDEX IF NOT EXISTS idx_runs_created_at
  ON runs(created_at DESC);

-- Document listing is ordered newest-first.
CREATE INDEX IF NOT EXISTS idx_documents_created_at
  ON documents(created_at DESC);

-- cleaned_outputs' primary key is (document_id, run_id), so lookups and
-- cascade cleanup keyed on run_id alone had no supporting index.
CREATE INDEX IF NOT EXISTS idx_cleaned_outputs_run_id
  ON cleaned_outputs(run_id);

-- cleaned_chunks joins runs back to chunk output on chunk_id.
CREATE INDEX IF NOT EXISTS idx_cleaned_chunks_chunk_id
  ON cleaned_chunks(chunk_id);
