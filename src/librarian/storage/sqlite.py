"""SQLite adapter foundation."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Sequence
from datetime import timedelta
from importlib.resources import files
from pathlib import Path

from librarian.application.clean_chunks import CleanedChunk
from librarian.application.jobs import QueuedRun, QueueStatus
from librarian.domain.ids import ChunkId, DocumentId, RunId
from librarian.domain.models import (
    Chunk,
    Classification,
    CleanedOutput,
    Document,
    DocumentStatus,
    ProcessingRun,
    RunStage,
    RunStatus,
    SourceFile,
    utc_now,
)


class SQLiteDatabase:
    """Small async-friendly SQLite wrapper for initialization."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                  version TEXT PRIMARY KEY,
                  applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                str(row[0])
                for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
            migration_root = files("librarian.storage.migrations")
            for migration in sorted(migration_root.iterdir(), key=lambda item: item.name):
                if not migration.name.endswith(".sql") or migration.name in applied:
                    continue
                connection.executescript(migration.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (migration.name, utc_now().isoformat()),
                )

    def connect(self) -> sqlite3.Connection:
        """Open a configured SQLite connection."""
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


class SQLiteRepository:
    """SQLite implementation of Librarian persistence ports."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    async def save_document(self, document: Document) -> None:
        """Save a document."""
        await asyncio.to_thread(self._save_document_sync, document)

    async def save_run(self, run: ProcessingRun) -> None:
        """Save a processing run."""
        await asyncio.to_thread(self._save_run_sync, run)

    async def get(self, item_id: DocumentId | RunId) -> Document | ProcessingRun | None:
        """Get a document or processing run by ID."""
        raw_id = str(item_id)
        if raw_id.startswith("run_"):
            return await asyncio.to_thread(self._get_run_sync, RunId(raw_id))
        return await asyncio.to_thread(self._get_document_sync, DocumentId(raw_id))

    async def get_document(self, document_id: DocumentId) -> Document | None:
        """Get a document by ID."""
        return await asyncio.to_thread(self._get_document_sync, document_id)

    async def get_run(self, run_id: RunId) -> ProcessingRun | None:
        """Get a processing run by ID."""
        return await asyncio.to_thread(self._get_run_sync, run_id)

    async def is_run_canceled(self, run_id: RunId) -> bool:
        """Return true when a run has been canceled."""
        return await asyncio.to_thread(self._is_run_canceled_sync, run_id)

    async def list_runs(self, *, limit: int = 100, offset: int = 0) -> Sequence[ProcessingRun]:
        """List processing runs."""
        return await asyncio.to_thread(self._list_runs_sync, limit, offset)

    async def list(self) -> Sequence[Document]:
        """List documents."""
        return await asyncio.to_thread(self._list_documents_sync)

    async def update_document_status(
        self, document_id: DocumentId, status: DocumentStatus
    ) -> None:
        """Update document status."""
        await asyncio.to_thread(self._update_document_status_sync, document_id, status)

    async def delete_document(self, document_id: DocumentId) -> None:
        """Delete a document and dependent records."""
        await asyncio.to_thread(self._delete_document_sync, document_id)

    async def update_status(
        self,
        run_id: RunId,
        *,
        status: RunStatus,
        stage: RunStage,
        error: str | None = None,
    ) -> None:
        """Update processing run status."""
        await asyncio.to_thread(self._update_run_status_sync, run_id, status, stage, error)

    async def update_run_progress(
        self,
        run_id: RunId,
        *,
        completed_chunks: int,
        failed_chunks: int,
        stage: RunStage,
        status: RunStatus = RunStatus.RUNNING,
    ) -> None:
        """Update processing run progress counters."""
        await asyncio.to_thread(
            self._update_run_progress_sync,
            run_id,
            completed_chunks,
            failed_chunks,
            stage,
            status,
        )

    async def put_text(self, key: str, text: str) -> str:
        """Store text content under a stable key."""
        await asyncio.to_thread(self._put_text_sync, key, text)
        return key

    async def get_text(self, key: str) -> str:
        """Read text content by key."""
        return await asyncio.to_thread(self._get_text_sync, key)

    async def save_many(self, chunks: Sequence[Chunk]) -> None:
        """Save chunks."""
        await asyncio.to_thread(self._save_chunks_sync, chunks)

    async def list_for_document(self, document_id: DocumentId) -> Sequence[Chunk]:
        """List chunks for a document."""
        return await asyncio.to_thread(self._list_chunks_sync, document_id)

    async def save_cleaned_chunks(
        self,
        run_id: RunId,
        chunks: Sequence[CleanedChunk],
    ) -> None:
        """Save chunk-level clean outputs."""
        await asyncio.to_thread(self._save_cleaned_chunks_sync, run_id, chunks)

    async def get_cached_cleaned_chunks(
        self,
        chunks: Sequence[Chunk],
        *,
        prompt_version: str,
        model_provider: str,
        model_name: str,
    ) -> Sequence[CleanedChunk]:
        """Get cached cleaned chunks for matching chunk hashes and model settings."""
        return await asyncio.to_thread(
            self._get_cached_cleaned_chunks_sync,
            chunks,
            prompt_version,
            model_provider,
            model_name,
        )

    async def save_cleaned_chunk_cache(
        self,
        chunks: Sequence[CleanedChunk],
        *,
        prompt_version: str,
        model_provider: str,
        model_name: str,
    ) -> None:
        """Cache cleaned chunks by chunk hash and model settings."""
        await asyncio.to_thread(
            self._save_cleaned_chunk_cache_sync,
            chunks,
            prompt_version,
            model_provider,
            model_name,
        )

    async def save_cleaned_output(self, output: CleanedOutput) -> None:
        """Save final cleaned output."""
        await asyncio.to_thread(self._save_cleaned_output_sync, output)

    async def get_cleaned_output(self, document_id: DocumentId) -> CleanedOutput | None:
        """Get latest cleaned output for a document."""
        return await asyncio.to_thread(self._get_cleaned_output_sync, document_id)

    async def save_classification(self, classification: Classification) -> None:
        """Save classification."""
        await asyncio.to_thread(self._save_classification_sync, classification)

    async def get_classification(self, document_id: DocumentId) -> Classification | None:
        """Get classification."""
        return await asyncio.to_thread(self._get_classification_sync, document_id)

    async def index(
        self,
        output: CleanedOutput,
        classification: Classification | None,
    ) -> None:
        """Index cleaned output in SQLite FTS."""
        del classification
        await asyncio.to_thread(self._index_sync, output)

    async def search(self, query: str, *, limit: int = 20) -> Sequence[DocumentId]:
        """Search cleaned outputs."""
        return await asyncio.to_thread(self._search_sync, query, limit)

    async def emit(self, run_id: RunId, stage: RunStage, message: str) -> None:
        """Persist a run event."""
        await asyncio.to_thread(self._emit_sync, run_id, stage, message)

    async def list_events(self, run_id: RunId) -> Sequence[str]:
        """List run event messages."""
        return await asyncio.to_thread(self._list_events_sync, run_id)

    async def stream(self, run_id: RunId):
        """Yield persisted run event messages."""
        for message in await self.list_events(run_id):
            yield message

    def _save_document_sync(self, document: Document) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO documents (
                  id, source_path, filename, media_type, byte_size, sha256,
                  status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  source_path = excluded.source_path,
                  filename = excluded.filename,
                  media_type = excluded.media_type,
                  byte_size = excluded.byte_size,
                  sha256 = excluded.sha256,
                  status = excluded.status,
                  updated_at = excluded.updated_at
                """,
                (
                    str(document.id),
                    str(document.source.path),
                    document.source.filename,
                    document.source.media_type,
                    document.source.byte_size,
                    document.source.sha256,
                    document.status.value,
                    document.created_at.isoformat(),
                    document.updated_at.isoformat(),
                ),
            )

    def _get_document_sync(self, document_id: DocumentId) -> Document | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM documents WHERE id = ?",
                (str(document_id),),
            ).fetchone()
        return _document_from_row(row) if row else None

    def _list_documents_sync(self) -> list[Document]:
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM documents ORDER BY created_at DESC").fetchall()
        return [_document_from_row(row) for row in rows]

    def _update_document_status_sync(
        self,
        document_id: DocumentId,
        status: DocumentStatus,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE documents SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, utc_now().isoformat(), str(document_id)),
            )

    def _delete_document_sync(self, document_id: DocumentId) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM cleaned_outputs_fts WHERE document_id = ?",
                (str(document_id),),
            )
            connection.execute(
                "DELETE FROM content_blobs WHERE key = ?",
                (f"raw:{document_id}",),
            )
            connection.execute(
                """
                DELETE FROM cleaned_chunk_cache
                WHERE chunk_sha256 IN (
                  SELECT sha256 FROM chunks WHERE document_id = ?
                )
                """,
                (str(document_id),),
            )
            connection.execute("DELETE FROM documents WHERE id = ?", (str(document_id),))

    def _save_run_sync(self, run: ProcessingRun) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO runs (
                  id, document_id, status, stage, total_chunks, completed_chunks,
                  failed_chunks, created_at, updated_at, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  status = excluded.status,
                  stage = excluded.stage,
                  total_chunks = excluded.total_chunks,
                  completed_chunks = excluded.completed_chunks,
                  failed_chunks = excluded.failed_chunks,
                  updated_at = excluded.updated_at,
                  error = excluded.error
                WHERE runs.status != 'canceled'
                """,
                (
                    str(run.id),
                    str(run.document_id),
                    run.status.value,
                    run.stage.value,
                    run.total_chunks,
                    run.completed_chunks,
                    run.failed_chunks,
                    run.created_at.isoformat(),
                    run.updated_at.isoformat(),
                    run.error,
                ),
            )

    def _get_run_sync(self, run_id: RunId) -> ProcessingRun | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (str(run_id),)).fetchone()
        return _run_from_row(row) if row else None

    def _is_run_canceled_sync(self, run_id: RunId) -> bool:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM runs WHERE id = ? AND status = ?",
                (str(run_id), RunStatus.CANCELED.value),
            ).fetchone()
        return row is not None

    def _list_runs_sync(self, limit: int, offset: int) -> list[ProcessingRun]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if offset < 0:
            raise ValueError("offset must be greater than or equal to 0")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM runs
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def _update_run_status_sync(
        self,
        run_id: RunId,
        status: RunStatus,
        stage: RunStage,
        error: str | None,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = ?, stage = ?, error = ?, updated_at = ?
                WHERE id = ?
                  AND (
                    status NOT IN ('canceled', 'succeeded')
                    OR (status = 'canceled' AND ? = 'canceled')
                  )
                """,
                (
                    status.value,
                    stage.value,
                    error,
                    utc_now().isoformat(),
                    str(run_id),
                    status.value,
                ),
            )

    def _update_run_progress_sync(
        self,
        run_id: RunId,
        completed_chunks: int,
        failed_chunks: int,
        stage: RunStage,
        status: RunStatus,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET status = ?, stage = ?, completed_chunks = ?, failed_chunks = ?, updated_at = ?
                WHERE id = ?
                  AND status != 'canceled'
                """,
                (
                    status.value,
                    stage.value,
                    completed_chunks,
                    failed_chunks,
                    utc_now().isoformat(),
                    str(run_id),
                ),
            )

    def _put_text_sync(self, key: str, text: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO content_blobs (key, text, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET text = excluded.text
                """,
                (key, text, utc_now().isoformat()),
            )

    def _get_text_sync(self, key: str) -> str:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT text FROM content_blobs WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            raise KeyError(key)
        return str(row["text"])

    def _save_chunks_sync(self, chunks: Sequence[Chunk]) -> None:
        with self.database.connect() as connection:
            connection.executemany(
                """
                INSERT INTO chunks (
                  id, document_id, ordinal, text, start_char, end_char, sha256
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  text = excluded.text,
                  start_char = excluded.start_char,
                  end_char = excluded.end_char,
                  sha256 = excluded.sha256
                """,
                [
                    (
                        str(chunk.id),
                        str(chunk.document_id),
                        chunk.ordinal,
                        chunk.text,
                        chunk.start_char,
                        chunk.end_char,
                        chunk.sha256,
                    )
                    for chunk in chunks
                ],
            )

    def _list_chunks_sync(self, document_id: DocumentId) -> list[Chunk]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM chunks WHERE document_id = ? ORDER BY ordinal ASC",
                (str(document_id),),
            ).fetchall()
        return [_chunk_from_row(row) for row in rows]

    def _save_cleaned_chunks_sync(
        self,
        run_id: RunId,
        chunks: Sequence[CleanedChunk],
    ) -> None:
        with self.database.connect() as connection:
            connection.executemany(
                """
                INSERT INTO cleaned_chunks (run_id, chunk_id, text, warnings, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id, chunk_id) DO UPDATE SET
                  text = excluded.text,
                  warnings = excluded.warnings
                """,
                [
                    (
                        str(run_id),
                        str(chunk.chunk.id),
                        chunk.text,
                        json.dumps(list(chunk.warnings)),
                        utc_now().isoformat(),
                    )
                    for chunk in chunks
                ],
            )

    def _get_cached_cleaned_chunks_sync(
        self,
        chunks: Sequence[Chunk],
        prompt_version: str,
        model_provider: str,
        model_name: str,
    ) -> list[CleanedChunk]:
        if not chunks:
            return []

        by_sha = {chunk.sha256: chunk for chunk in chunks}
        with self.database.connect() as connection:
            rows = [
                row
                for sha in by_sha
                if (
                    row := connection.execute(
                        """
                        SELECT chunk_sha256, text, warnings
                        FROM cleaned_chunk_cache
                        WHERE chunk_sha256 = ?
                          AND prompt_version = ?
                          AND model_provider = ?
                          AND model_name = ?
                        """,
                        (sha, prompt_version, model_provider, model_name),
                    ).fetchone()
                )
                is not None
            ]

        cached: list[CleanedChunk] = []
        for row in rows:
            chunk = by_sha[str(row["chunk_sha256"])]
            warnings = tuple(str(item) for item in json.loads(str(row["warnings"])))
            cached.append(
                CleanedChunk(
                    chunk=chunk,
                    text=str(row["text"]),
                    warnings=warnings,
                )
            )
        return cached

    def _save_cleaned_chunk_cache_sync(
        self,
        chunks: Sequence[CleanedChunk],
        prompt_version: str,
        model_provider: str,
        model_name: str,
    ) -> None:
        if not chunks:
            return

        with self.database.connect() as connection:
            connection.executemany(
                """
                INSERT INTO cleaned_chunk_cache (
                  chunk_sha256, prompt_version, model_provider, model_name,
                  text, warnings, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chunk_sha256, prompt_version, model_provider, model_name)
                DO UPDATE SET
                  text = excluded.text,
                  warnings = excluded.warnings
                """,
                [
                    (
                        item.chunk.sha256,
                        prompt_version,
                        model_provider,
                        model_name,
                        item.text,
                        json.dumps(list(item.warnings)),
                        utc_now().isoformat(),
                    )
                    for item in chunks
                ],
            )

    def _save_cleaned_output_sync(self, output: CleanedOutput) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO cleaned_outputs (
                  document_id, run_id, text, prompt_version, model_provider,
                  model_name, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id, run_id) DO UPDATE SET
                  text = excluded.text
                """,
                (
                    str(output.document_id),
                    str(output.run_id),
                    output.text,
                    output.prompt_version,
                    output.model_provider,
                    output.model_name,
                    output.created_at.isoformat(),
                ),
            )

    def _get_cleaned_output_sync(self, document_id: DocumentId) -> CleanedOutput | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT cleaned_outputs.*
                FROM cleaned_outputs
                JOIN runs ON runs.id = cleaned_outputs.run_id
                WHERE cleaned_outputs.document_id = ?
                  AND runs.status = ?
                ORDER BY cleaned_outputs.created_at DESC
                LIMIT 1
                """,
                (str(document_id), RunStatus.SUCCEEDED.value),
            ).fetchone()
        return _cleaned_output_from_row(row) if row else None

    def _save_classification_sync(self, classification: Classification) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO classifications (
                  document_id, code, label, summary, taxonomy, confidence
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                  code = excluded.code,
                  label = excluded.label,
                  summary = excluded.summary,
                  taxonomy = excluded.taxonomy,
                  confidence = excluded.confidence
                """,
                (
                    str(classification.document_id),
                    classification.code,
                    classification.label,
                    classification.summary,
                    classification.taxonomy,
                    classification.confidence,
                ),
            )

    def _get_classification_sync(self, document_id: DocumentId) -> Classification | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM classifications WHERE document_id = ?",
                (str(document_id),),
            ).fetchone()
        return _classification_from_row(row) if row else None

    def _index_sync(self, output: CleanedOutput) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM cleaned_outputs_fts WHERE document_id = ? AND run_id = ?",
                (str(output.document_id), str(output.run_id)),
            )
            connection.execute(
                """
                INSERT INTO cleaned_outputs_fts (document_id, run_id, text)
                VALUES (?, ?, ?)
                """,
                (str(output.document_id), str(output.run_id), output.text),
            )

    def _search_sync(self, query: str, limit: int) -> list[DocumentId]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self.database.connect() as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT DISTINCT cleaned_outputs_fts.document_id
                    FROM cleaned_outputs_fts
                    JOIN runs ON runs.id = cleaned_outputs_fts.run_id
                    WHERE cleaned_outputs_fts MATCH ?
                      AND runs.status = ?
                    LIMIT ?
                    """,
                    (query, RunStatus.SUCCEEDED.value, limit),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                raise ValueError("Invalid search query") from exc
        return [DocumentId(str(row["document_id"])) for row in rows]

    def _emit_sync(self, run_id: RunId, stage: RunStage, message: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO run_events (run_id, stage, message, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(run_id), stage.value, message, utc_now().isoformat()),
            )

    def _list_events_sync(self, run_id: RunId) -> list[str]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT stage, message FROM run_events WHERE run_id = ? ORDER BY id ASC",
                (str(run_id),),
            ).fetchall()
        return [f"{row['stage']}: {row['message']}" for row in rows]


class SQLiteRunQueue:
    """SQLite-backed durable processing queue."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    async def enqueue(self, run_id: RunId) -> None:
        """Enqueue a run for external worker processing."""
        await asyncio.to_thread(self._enqueue_sync, run_id)

    async def claim(self, *, worker_id: str, lease_seconds: int) -> QueuedRun | None:
        """Claim one available run for a worker."""
        return await asyncio.to_thread(self._claim_sync, worker_id, lease_seconds)

    async def complete(self, run_id: RunId) -> None:
        """Mark a queued run complete."""
        await asyncio.to_thread(self._complete_sync, run_id)

    async def fail(self, run_id: RunId, *, error: str, max_attempts: int) -> None:
        """Mark a queued run failed or schedule a retry."""
        await asyncio.to_thread(self._fail_sync, run_id, error, max_attempts)

    async def cancel(self, run_id: RunId, *, error: str | None = None) -> None:
        """Mark a queued run canceled."""
        await asyncio.to_thread(self._cancel_sync, run_id, error)

    async def list(self, *, limit: int = 100) -> tuple[QueuedRun, ...]:
        """List queued run state."""
        return await asyncio.to_thread(self._list_sync, limit)

    def _enqueue_sync(self, run_id: RunId) -> None:
        now = utc_now().isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO run_queue (
                  run_id, status, attempts, available_at, created_at, updated_at
                )
                VALUES (?, ?, 0, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  status = excluded.status,
                  available_at = excluded.available_at,
                  locked_at = NULL,
                  locked_by = NULL,
                  updated_at = excluded.updated_at,
                  last_error = NULL
                WHERE run_queue.status IN ('failed', 'succeeded')
                """,
                (str(run_id), QueueStatus.QUEUED.value, now, now, now),
            )

    def _claim_sync(self, worker_id: str, lease_seconds: int) -> QueuedRun | None:
        now = utc_now()
        lease_cutoff = now - timedelta(seconds=lease_seconds)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM run_queue
                WHERE status IN (?, ?, ?)
                  AND available_at <= ?
                  AND (locked_at IS NULL OR locked_at <= ?)
                ORDER BY updated_at ASC
                LIMIT 1
                """,
                (
                    QueueStatus.QUEUED.value,
                    QueueStatus.RETRY.value,
                    QueueStatus.RUNNING.value,
                    now.isoformat(),
                    lease_cutoff.isoformat(),
                ),
            ).fetchone()
            if row is None:
                connection.commit()
                return None

            connection.execute(
                """
                UPDATE run_queue
                SET status = ?, attempts = attempts + 1, locked_at = ?, locked_by = ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (
                    QueueStatus.RUNNING.value,
                    now.isoformat(),
                    worker_id,
                    now.isoformat(),
                    str(row["run_id"]),
                ),
            )
            updated = connection.execute(
                "SELECT * FROM run_queue WHERE run_id = ?",
                (str(row["run_id"]),),
            ).fetchone()
            connection.commit()

        return _queued_run_from_row(updated) if updated else None

    def _complete_sync(self, run_id: RunId) -> None:
        now = utc_now().isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE run_queue
                SET status = ?, locked_at = NULL, locked_by = NULL, updated_at = ?
                WHERE run_id = ?
                  AND status = ?
                """,
                (
                    QueueStatus.SUCCEEDED.value,
                    now,
                    str(run_id),
                    QueueStatus.RUNNING.value,
                ),
            )

    def _fail_sync(self, run_id: RunId, error: str, max_attempts: int) -> None:
        now = utc_now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT attempts FROM run_queue WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            attempts = int(row["attempts"]) if row else 0
            status = QueueStatus.FAILED if attempts >= max_attempts else QueueStatus.RETRY
            backoff_seconds = min(300, max(1, 2 ** max(attempts - 1, 0)))
            available_at = now if status is QueueStatus.FAILED else now + timedelta(
                seconds=backoff_seconds
            )
            connection.execute(
                """
                UPDATE run_queue
                SET status = ?, available_at = ?, locked_at = NULL, locked_by = NULL,
                    updated_at = ?, last_error = ?
                WHERE run_id = ?
                  AND status = ?
                """,
                (
                    status.value,
                    available_at.isoformat(),
                    now.isoformat(),
                    error,
                    str(run_id),
                    QueueStatus.RUNNING.value,
                ),
            )

    def _cancel_sync(self, run_id: RunId, error: str | None) -> None:
        now = utc_now().isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE run_queue
                SET status = ?, locked_at = NULL, locked_by = NULL,
                    updated_at = ?, last_error = ?
                WHERE run_id = ?
                """,
                (
                    QueueStatus.CANCELED.value,
                    now,
                    error,
                    str(run_id),
                ),
            )

    def _list_sync(self, limit: int) -> tuple[QueuedRun, ...]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM run_queue
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(_queued_run_from_row(row) for row in rows)


def _document_from_row(row: sqlite3.Row) -> Document:
    return Document(
        id=DocumentId(str(row["id"])),
        source=SourceFile(
            path=Path(str(row["source_path"])),
            filename=str(row["filename"]),
            media_type=str(row["media_type"]),
            byte_size=int(row["byte_size"]),
            sha256=str(row["sha256"]),
        ),
        status=DocumentStatus(str(row["status"])),
        created_at=_parse_datetime(str(row["created_at"])),
        updated_at=_parse_datetime(str(row["updated_at"])),
    )


def _chunk_from_row(row: sqlite3.Row) -> Chunk:
    return Chunk(
        id=ChunkId(str(row["id"])),
        document_id=DocumentId(str(row["document_id"])),
        ordinal=int(row["ordinal"]),
        text=str(row["text"]),
        start_char=int(row["start_char"]),
        end_char=int(row["end_char"]),
        sha256=str(row["sha256"]),
    )


def _run_from_row(row: sqlite3.Row) -> ProcessingRun:
    return ProcessingRun(
        id=RunId(str(row["id"])),
        document_id=DocumentId(str(row["document_id"])),
        status=RunStatus(str(row["status"])),
        stage=RunStage(str(row["stage"])),
        total_chunks=int(row["total_chunks"]),
        completed_chunks=int(row["completed_chunks"]),
        failed_chunks=int(row["failed_chunks"]),
        created_at=_parse_datetime(str(row["created_at"])),
        updated_at=_parse_datetime(str(row["updated_at"])),
        error=str(row["error"]) if row["error"] is not None else None,
    )


def _cleaned_output_from_row(row: sqlite3.Row) -> CleanedOutput:
    return CleanedOutput(
        document_id=DocumentId(str(row["document_id"])),
        run_id=RunId(str(row["run_id"])),
        text=str(row["text"]),
        prompt_version=str(row["prompt_version"]),
        model_provider=str(row["model_provider"]),
        model_name=str(row["model_name"]),
        created_at=_parse_datetime(str(row["created_at"])),
    )


def _classification_from_row(row: sqlite3.Row) -> Classification:
    confidence = row["confidence"]
    return Classification(
        document_id=DocumentId(str(row["document_id"])),
        code=str(row["code"]),
        label=str(row["label"]),
        summary=str(row["summary"]),
        taxonomy=str(row["taxonomy"]),
        confidence=float(confidence) if confidence is not None else None,
    )


def _queued_run_from_row(row: sqlite3.Row) -> QueuedRun:
    locked_at = row["locked_at"]
    return QueuedRun(
        run_id=RunId(str(row["run_id"])),
        status=QueueStatus(str(row["status"])),
        attempts=int(row["attempts"]),
        available_at=_parse_datetime(str(row["available_at"])),
        locked_at=_parse_datetime(str(locked_at)) if locked_at is not None else None,
        locked_by=str(row["locked_by"]) if row["locked_by"] is not None else None,
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
    )


def _parse_datetime(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)
