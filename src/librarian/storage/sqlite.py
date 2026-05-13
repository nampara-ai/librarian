"""SQLite adapter foundation."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import LiteralString

from librarian.application.clean_chunks import CleanedChunk
from librarian.application.jobs import QueuedRun, QueueStatus
from librarian.application.ports import SearchScope
from librarian.domain.ids import ChunkId, DocumentId, RunId
from librarian.domain.models import (
    Chunk,
    Classification,
    CleanedOutput,
    Document,
    DocumentStatus,
    ProcessingRun,
    RunEvent,
    RunStage,
    RunStatus,
    SearchFacets,
    SearchFacetValue,
    SearchResult,
    SourceFile,
    utc_now,
)

_SQLITE_BUSY_TIMEOUT_MS = 5_000
_MAX_SEARCH_QUERY_CHARS = 4_096
_MAX_EVENT_PAGE_SIZE = 1_000
_SEARCH_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


def _path_crosses_symlink(path: Path) -> bool:
    for current in reversed(path.parents):
        if current.is_absolute() and len(current.parts) <= 2:
            continue
        if current.exists() and current.is_symlink():
            return True
    return False


def _path_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


@dataclass(frozen=True, slots=True)
class SQLiteMaintenanceResult:
    """Result from an operator-triggered SQLite maintenance run."""

    checkpoint_busy: int
    checkpoint_log_frames: int
    checkpoint_checkpointed_frames: int
    vacuumed: bool


@dataclass(frozen=True, slots=True)
class SQLiteStorageStats:
    """Sizing summary for an operator-inspected SQLite database."""

    database_path: Path
    database_file_bytes: int
    wal_file_bytes: int
    shm_file_bytes: int
    total_sqlite_bytes: int
    page_size_bytes: int
    page_count: int
    freelist_count: int
    used_page_bytes: int
    free_page_bytes: int
    table_counts: dict[str, int]
    source_file_bytes: int
    content_blob_text_bytes: int
    chunk_text_bytes: int
    cleaned_chunk_text_bytes: int
    cleaned_cache_text_bytes: int
    cleaned_output_text_bytes: int


@dataclass(frozen=True, slots=True)
class SQLiteBackupResult:
    """Result from an operator-triggered SQLite backup."""

    source_path: Path
    destination_path: Path
    byte_size: int


@dataclass(frozen=True, slots=True)
class SQLiteRestoreResult:
    """Result from an operator-triggered SQLite restore."""

    source_path: Path
    destination_path: Path
    byte_size: int


@dataclass(frozen=True, slots=True)
class SQLiteVerifyResult:
    """Result from an operator-triggered SQLite verification."""

    database_path: Path
    integrity_ok: bool
    foreign_key_violations: int
    applied_migrations: int

    @property
    def ok(self) -> bool:
        """Return true when the database passes all verification checks."""
        return self.integrity_ok and self.foreign_key_violations == 0


class SQLiteDatabase:
    """Small async-friendly SQLite wrapper for initialization."""

    def __init__(self, path: Path) -> None:
        self.path = path

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def maintain(self, *, vacuum: bool = False) -> SQLiteMaintenanceResult:
        """Run lightweight SQLite maintenance for long-lived local databases."""
        return await asyncio.to_thread(self._maintain_sync, vacuum)

    async def stats(self) -> SQLiteStorageStats:
        """Return SQLite file, page, row, and stored-text sizing statistics."""
        return await asyncio.to_thread(self._stats_sync)

    async def backup(self, destination: Path, *, overwrite: bool = False) -> SQLiteBackupResult:
        """Create a consistent online SQLite backup."""
        return await asyncio.to_thread(self._backup_sync, destination, overwrite)

    async def restore(self, source: Path) -> SQLiteRestoreResult:
        """Restore the database from a verified SQLite backup."""
        return await asyncio.to_thread(self._restore_sync, source)

    async def verify(self) -> SQLiteVerifyResult:
        """Verify SQLite integrity, foreign keys, and migration metadata."""
        return await asyncio.to_thread(self._verify_sync)

    def _initialize_sync(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
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
        connection.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def _maintain_sync(self, vacuum: bool) -> SQLiteMaintenanceResult:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA optimize")
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if vacuum:
            with self.connect() as connection:
                connection.execute("VACUUM")
        if row is None:
            return SQLiteMaintenanceResult(
                checkpoint_busy=0,
                checkpoint_log_frames=0,
                checkpoint_checkpointed_frames=0,
                vacuumed=vacuum,
            )
        return SQLiteMaintenanceResult(
            checkpoint_busy=int(row[0]),
            checkpoint_log_frames=int(row[1]),
            checkpoint_checkpointed_frames=int(row[2]),
            vacuumed=vacuum,
        )

    def _stats_sync(self) -> SQLiteStorageStats:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
            table_counts = {
                "documents": self._scalar_int(connection, "SELECT COUNT(*) FROM documents"),
                "content_blobs": self._scalar_int(
                    connection,
                    "SELECT COUNT(*) FROM content_blobs",
                ),
                "chunks": self._scalar_int(connection, "SELECT COUNT(*) FROM chunks"),
                "runs": self._scalar_int(connection, "SELECT COUNT(*) FROM runs"),
                "cleaned_chunks": self._scalar_int(
                    connection,
                    "SELECT COUNT(*) FROM cleaned_chunks",
                ),
                "cleaned_chunk_cache": self._scalar_int(
                    connection,
                    "SELECT COUNT(*) FROM cleaned_chunk_cache",
                ),
                "cleaned_outputs": self._scalar_int(
                    connection,
                    "SELECT COUNT(*) FROM cleaned_outputs",
                ),
                "classifications": self._scalar_int(
                    connection,
                    "SELECT COUNT(*) FROM classifications",
                ),
                "run_events": self._scalar_int(connection, "SELECT COUNT(*) FROM run_events"),
                "run_queue": self._scalar_int(connection, "SELECT COUNT(*) FROM run_queue"),
            }
            source_file_bytes = self._scalar_int(
                connection,
                "SELECT COALESCE(SUM(byte_size), 0) FROM documents",
            )
            content_blob_text_bytes = self._scalar_int(
                connection,
                "SELECT COALESCE(SUM(LENGTH(CAST(text AS BLOB))), 0) FROM content_blobs",
            )
            chunk_text_bytes = self._scalar_int(
                connection,
                "SELECT COALESCE(SUM(LENGTH(CAST(text AS BLOB))), 0) FROM chunks",
            )
            cleaned_chunk_text_bytes = self._scalar_int(
                connection,
                "SELECT COALESCE(SUM(LENGTH(CAST(text AS BLOB))), 0) FROM cleaned_chunks",
            )
            cleaned_cache_text_bytes = self._scalar_int(
                connection,
                "SELECT COALESCE(SUM(LENGTH(CAST(text AS BLOB))), 0) "
                "FROM cleaned_chunk_cache",
            )
            cleaned_output_text_bytes = self._scalar_int(
                connection,
                "SELECT COALESCE(SUM(LENGTH(CAST(text AS BLOB))), 0) FROM cleaned_outputs",
            )
        database_file_bytes = self.path.stat().st_size if self.path.exists() else 0
        wal_file_bytes = _path_size(Path(f"{self.path}-wal"))
        shm_file_bytes = _path_size(Path(f"{self.path}-shm"))
        return SQLiteStorageStats(
            database_path=self.path,
            database_file_bytes=database_file_bytes,
            wal_file_bytes=wal_file_bytes,
            shm_file_bytes=shm_file_bytes,
            total_sqlite_bytes=database_file_bytes + wal_file_bytes + shm_file_bytes,
            page_size_bytes=page_size,
            page_count=page_count,
            freelist_count=freelist_count,
            used_page_bytes=max(page_count - freelist_count, 0) * page_size,
            free_page_bytes=freelist_count * page_size,
            table_counts=table_counts,
            source_file_bytes=source_file_bytes,
            content_blob_text_bytes=content_blob_text_bytes,
            chunk_text_bytes=chunk_text_bytes,
            cleaned_chunk_text_bytes=cleaned_chunk_text_bytes,
            cleaned_cache_text_bytes=cleaned_cache_text_bytes,
            cleaned_output_text_bytes=cleaned_output_text_bytes,
        )

    def _backup_sync(self, destination: Path, overwrite: bool) -> SQLiteBackupResult:
        source_path = self.path.expanduser().resolve()
        expanded_destination = destination.expanduser()
        if expanded_destination.is_symlink():
            raise ValueError("Backup destination must not be a symlink")
        if _path_crosses_symlink(expanded_destination):
            raise ValueError("Backup destination must not cross a symlinked parent")
        destination_path = expanded_destination.resolve()
        if source_path == destination_path:
            raise ValueError("Backup destination must be different from the source database")
        if not source_path.exists():
            raise FileNotFoundError(f"SQLite database does not exist: {source_path}")
        if destination_path.exists() and not overwrite:
            raise FileExistsError(f"Backup destination already exists: {destination_path}")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = destination_path.with_name(f".{destination_path.name}.tmp")
        temporary_path.unlink(missing_ok=True)
        try:
            with self.connect() as source, sqlite3.connect(temporary_path) as target:
                source.backup(target)
                integrity = target.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or str(integrity[0]).lower() != "ok":
                    raise RuntimeError("SQLite backup failed integrity check")
            temporary_path.replace(destination_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        return SQLiteBackupResult(
            source_path=source_path,
            destination_path=destination_path,
            byte_size=destination_path.stat().st_size,
        )

    def _restore_sync(self, source: Path) -> SQLiteRestoreResult:
        source_path = source.expanduser().resolve()
        expanded_destination = self.path.expanduser()
        if expanded_destination.is_symlink():
            raise ValueError("Restore destination must not be a symlink")
        if _path_crosses_symlink(expanded_destination):
            raise ValueError("Restore destination must not cross a symlinked parent")
        destination_path = expanded_destination.resolve()
        if source_path == destination_path:
            raise ValueError("Restore source must be different from the destination database")
        if not source_path.exists():
            raise FileNotFoundError(f"SQLite backup does not exist: {source_path}")
        self._verify_sqlite_file(source_path, "SQLite backup failed integrity check")
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = destination_path.with_name(f".{destination_path.name}.restore.tmp")
        temporary_path.unlink(missing_ok=True)
        try:
            with sqlite3.connect(source_path) as backup, sqlite3.connect(temporary_path) as target:
                backup.backup(target)
                integrity = target.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or str(integrity[0]).lower() != "ok":
                    raise RuntimeError("Restored SQLite database failed integrity check")
            self._remove_sidecars(destination_path)
            temporary_path.replace(destination_path)
            self._remove_sidecars(destination_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        return SQLiteRestoreResult(
            source_path=source_path,
            destination_path=destination_path,
            byte_size=destination_path.stat().st_size,
        )

    def _verify_sync(self) -> SQLiteVerifyResult:
        database_path = self.path.expanduser().resolve()
        if not database_path.exists():
            raise FileNotFoundError(f"SQLite database does not exist: {database_path}")
        with self.connect() as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            integrity_ok = integrity is not None and str(integrity[0]).lower() == "ok"
            foreign_key_violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
            migration_row = connection.execute(
                "SELECT COUNT(*) AS count FROM schema_migrations"
            ).fetchone()
        return SQLiteVerifyResult(
            database_path=database_path,
            integrity_ok=integrity_ok,
            foreign_key_violations=foreign_key_violations,
            applied_migrations=int(migration_row["count"]) if migration_row else 0,
        )

    @staticmethod
    def _verify_sqlite_file(path: Path, failure_message: str) -> None:
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(failure_message) from exc
        if integrity is None or str(integrity[0]).lower() != "ok":
            raise RuntimeError(failure_message)

    @staticmethod
    def _remove_sidecars(path: Path) -> None:
        Path(f"{path}-wal").unlink(missing_ok=True)
        Path(f"{path}-shm").unlink(missing_ok=True)

    @staticmethod
    def _scalar_int(connection: sqlite3.Connection, query: LiteralString) -> int:
        row = connection.execute(query).fetchone()
        return int(row[0]) if row else 0


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

    async def list(self, *, limit: int = 100, offset: int = 0) -> Sequence[Document]:
        """List documents."""
        return await asyncio.to_thread(self._list_documents_sync, limit, offset)

    async def count_documents(self) -> int:
        """Count documents."""
        return await asyncio.to_thread(self._count_documents_sync)

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

    async def publish_successful_run(
        self,
        output: CleanedOutput,
        classification: Classification,
    ) -> None:
        """Atomically publish final output, search, classification, and run status."""
        await asyncio.to_thread(self._publish_successful_run_sync, output, classification)

    async def index(
        self,
        output: CleanedOutput,
        classification: Classification | None,
    ) -> None:
        """Index cleaned output in SQLite FTS."""
        del classification
        await asyncio.to_thread(self._index_sync, output)

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        classification_code: str | None = None,
        document_status: DocumentStatus | None = None,
        filename_contains: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        scope: SearchScope = "cleaned",
        phrase: bool = False,
    ) -> Sequence[DocumentId]:
        """Search cleaned outputs."""
        return await asyncio.to_thread(
            self._search_sync,
            query,
            limit,
            offset,
            classification_code,
            document_status,
            filename_contains,
            created_after,
            created_before,
            scope,
            phrase,
        )

    async def search_results(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        classification_code: str | None = None,
        document_status: DocumentStatus | None = None,
        filename_contains: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        scope: SearchScope = "cleaned",
        phrase: bool = False,
    ) -> Sequence[SearchResult]:
        """Search cleaned outputs with ranking metadata and snippets."""
        return await asyncio.to_thread(
            self._search_results_sync,
            query,
            limit,
            offset,
            classification_code,
            document_status,
            filename_contains,
            created_after,
            created_before,
            scope,
            phrase,
        )

    async def search_count(
        self,
        query: str,
        *,
        classification_code: str | None = None,
        document_status: DocumentStatus | None = None,
        filename_contains: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        scope: SearchScope = "cleaned",
        phrase: bool = False,
    ) -> int:
        """Count documents matching the same filters used by search results."""
        return await asyncio.to_thread(
            self._search_count_sync,
            query,
            classification_code,
            document_status,
            filename_contains,
            created_after,
            created_before,
            scope,
            phrase,
        )

    async def search_facets(
        self,
        query: str,
        *,
        classification_code: str | None = None,
        document_status: DocumentStatus | None = None,
        filename_contains: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        scope: SearchScope = "cleaned",
        phrase: bool = False,
    ) -> SearchFacets:
        """Return facet counts for matching documents."""
        return await asyncio.to_thread(
            self._search_facets_sync,
            query,
            classification_code,
            document_status,
            filename_contains,
            created_after,
            created_before,
            scope,
            phrase,
        )

    async def emit(self, run_id: RunId, stage: RunStage, message: str) -> None:
        """Persist a run event."""
        await asyncio.to_thread(self._emit_sync, run_id, stage, message)

    async def list_events(
        self,
        run_id: RunId,
        *,
        limit: int = _MAX_EVENT_PAGE_SIZE,
        offset: int = 0,
    ) -> Sequence[str]:
        """List run event messages."""
        return await asyncio.to_thread(self._list_events_sync, run_id, limit, offset)

    async def list_event_records(
        self,
        run_id: RunId,
        *,
        limit: int = _MAX_EVENT_PAGE_SIZE,
        offset: int = 0,
    ) -> Sequence[RunEvent]:
        """List structured run events."""
        return await asyncio.to_thread(self._list_event_records_sync, run_id, limit, offset)

    async def stream(self, run_id: RunId):
        """Yield persisted run event messages."""
        offset = 0
        while True:
            messages = await self.list_events(run_id, limit=_MAX_EVENT_PAGE_SIZE, offset=offset)
            if not messages:
                break
            for message in messages:
                yield message
            offset += len(messages)

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

    def _list_documents_sync(self, limit: int, offset: int) -> list[Document]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if offset < 0:
            raise ValueError("offset must be greater than or equal to 0")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM documents
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [_document_from_row(row) for row in rows]

    def _count_documents_sync(self) -> int:
        with self.database.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM documents").fetchone()
        return int(row["count"]) if row else 0

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
                "DELETE FROM raw_content_fts WHERE document_id = ?",
                (str(document_id),),
            )
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
            if key.startswith("raw:"):
                document_id = key.removeprefix("raw:")
                connection.execute(
                    "DELETE FROM raw_content_fts WHERE document_id = ?",
                    (document_id,),
                )
                connection.execute(
                    """
                    INSERT INTO raw_content_fts (document_id, text)
                    VALUES (?, ?)
                    """,
                    (document_id, text),
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

    def _publish_successful_run_sync(
        self,
        output: CleanedOutput,
        classification: Classification,
    ) -> None:
        now = utc_now().isoformat()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_row = connection.execute(
                "SELECT status FROM runs WHERE id = ?",
                (str(output.run_id),),
            ).fetchone()
            if run_row is None:
                raise RuntimeError(f"Run not found: {output.run_id}")
            if str(run_row["status"]) != RunStatus.RUNNING.value:
                raise RuntimeError(f"Run is not running and cannot be published: {output.run_id}")

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
            connection.execute(
                "UPDATE documents SET status = ?, updated_at = ? WHERE id = ?",
                (DocumentStatus.READY.value, now, str(output.document_id)),
            )
            connection.execute(
                """
                UPDATE runs
                SET status = ?, stage = ?, error = NULL, updated_at = ?
                WHERE id = ?
                  AND status = ?
                """,
                (
                    RunStatus.SUCCEEDED.value,
                    RunStage.COMPLETE.value,
                    now,
                    str(output.run_id),
                    RunStatus.RUNNING.value,
                ),
            )
            connection.executemany(
                """
                INSERT INTO run_events (run_id, stage, message, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    (
                        str(output.run_id),
                        RunStage.INDEX.value,
                        "stored output and search index",
                        now,
                    ),
                    (
                        str(output.run_id),
                        RunStage.COMPLETE.value,
                        "processing complete",
                        now,
                    ),
                ),
            )
            connection.commit()

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

    def _search_sync(
        self,
        query: str,
        limit: int,
        offset: int = 0,
        classification_code: str | None = None,
        document_status: DocumentStatus | None = None,
        filename_contains: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        scope: SearchScope = "cleaned",
        phrase: bool = False,
    ) -> list[DocumentId]:
        return [
            result.document_id
            for result in self._search_results_sync(
                query,
                limit,
                offset,
                classification_code,
                document_status,
                filename_contains,
                created_after,
                created_before,
                scope,
                phrase,
            )
        ]

    def _search_count_sync(
        self,
        query: str,
        classification_code: str | None = None,
        document_status: DocumentStatus | None = None,
        filename_contains: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        scope: SearchScope = "cleaned",
        phrase: bool = False,
    ) -> int:
        if scope == "raw":
            return self._raw_search_count_sync(
                query,
                classification_code,
                document_status,
                filename_contains,
                created_after,
                created_before,
                phrase,
            )
        match_query = normalize_search_query(query, phrase=phrase)
        filename_pattern = f"%{_escape_like(filename_contains)}%" if filename_contains else None
        parameters: tuple[object, ...] = (
            match_query,
            RunStatus.SUCCEEDED.value,
            classification_code,
            classification_code,
            document_status.value if document_status else None,
            document_status.value if document_status else None,
            filename_pattern,
            filename_pattern,
            created_after.isoformat() if created_after else None,
            created_after.isoformat() if created_after else None,
            created_before.isoformat() if created_before else None,
            created_before.isoformat() if created_before else None,
        )
        with self.database.connect() as connection:
            try:
                row = connection.execute(
                    """
                    SELECT COUNT(DISTINCT cleaned_outputs_fts.document_id) AS count
                    FROM cleaned_outputs_fts
                    JOIN runs ON runs.id = cleaned_outputs_fts.run_id
                    JOIN documents ON documents.id = cleaned_outputs_fts.document_id
                    JOIN cleaned_outputs
                      ON cleaned_outputs.document_id = cleaned_outputs_fts.document_id
                     AND cleaned_outputs.run_id = cleaned_outputs_fts.run_id
                    LEFT JOIN classifications
                      ON classifications.document_id = cleaned_outputs_fts.document_id
                    WHERE cleaned_outputs_fts MATCH ?
                      AND runs.status = ?
                      AND cleaned_outputs.created_at = (
                        SELECT MAX(latest.created_at)
                        FROM cleaned_outputs AS latest
                        WHERE latest.document_id = cleaned_outputs.document_id
                      )
                      AND (? IS NULL OR classifications.code = ?)
                      AND (? IS NULL OR documents.status = ?)
                      AND (? IS NULL OR documents.filename LIKE ? ESCAPE '~')
                      AND (? IS NULL OR documents.created_at >= ?)
                      AND (? IS NULL OR documents.created_at <= ?)
                    """,
                    parameters,
                ).fetchone()
            except sqlite3.OperationalError as exc:
                raise ValueError("Invalid search query") from exc
        return int(row["count"]) if row else 0

    def _search_results_sync(
        self,
        query: str,
        limit: int,
        offset: int = 0,
        classification_code: str | None = None,
        document_status: DocumentStatus | None = None,
        filename_contains: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        scope: SearchScope = "cleaned",
        phrase: bool = False,
    ) -> list[SearchResult]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if offset < 0:
            raise ValueError("offset must be greater than or equal to 0")
        if scope == "raw":
            return self._raw_search_results_sync(
                query,
                limit,
                offset,
                classification_code,
                document_status,
                filename_contains,
                created_after,
                created_before,
                phrase,
            )
        match_query = normalize_search_query(query, phrase=phrase)
        filename_pattern = f"%{_escape_like(filename_contains)}%" if filename_contains else None
        parameters: tuple[object, ...] = (
            match_query,
            RunStatus.SUCCEEDED.value,
            classification_code,
            classification_code,
            document_status.value if document_status else None,
            document_status.value if document_status else None,
            filename_pattern,
            filename_pattern,
            created_after.isoformat() if created_after else None,
            created_after.isoformat() if created_after else None,
            created_before.isoformat() if created_before else None,
            created_before.isoformat() if created_before else None,
            limit,
            offset,
        )
        with self.database.connect() as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT
                      cleaned_outputs_fts.document_id,
                      cleaned_outputs_fts.run_id,
                      documents.filename,
                      documents.status AS document_status,
                      classifications.code AS classification_code,
                      classifications.label AS classification_label,
                      snippet(cleaned_outputs_fts, 2, '<mark>', '</mark>', '...', 16) AS snippet,
                      bm25(cleaned_outputs_fts) AS score
                    FROM cleaned_outputs_fts
                    JOIN runs ON runs.id = cleaned_outputs_fts.run_id
                    JOIN documents ON documents.id = cleaned_outputs_fts.document_id
                    JOIN cleaned_outputs
                      ON cleaned_outputs.document_id = cleaned_outputs_fts.document_id
                     AND cleaned_outputs.run_id = cleaned_outputs_fts.run_id
                    LEFT JOIN classifications
                      ON classifications.document_id = cleaned_outputs_fts.document_id
                    WHERE cleaned_outputs_fts MATCH ?
                      AND runs.status = ?
                      AND cleaned_outputs.created_at = (
                        SELECT MAX(latest.created_at)
                        FROM cleaned_outputs AS latest
                        WHERE latest.document_id = cleaned_outputs.document_id
                      )
                      AND (? IS NULL OR classifications.code = ?)
                      AND (? IS NULL OR documents.status = ?)
                      AND (? IS NULL OR documents.filename LIKE ? ESCAPE '~')
                      AND (? IS NULL OR documents.created_at >= ?)
                      AND (? IS NULL OR documents.created_at <= ?)
                    ORDER BY score ASC
                    LIMIT ? OFFSET ?
                    """,
                    parameters,
                ).fetchall()
            except sqlite3.OperationalError as exc:
                raise ValueError("Invalid search query") from exc
        return [
            SearchResult(
                document_id=DocumentId(str(row["document_id"])),
                run_id=RunId(str(row["run_id"])),
                source="cleaned",
                filename=str(row["filename"]),
                document_status=DocumentStatus(str(row["document_status"])),
                snippet=str(row["snippet"]),
                score=float(row["score"]),
                classification_code=(
                    str(row["classification_code"])
                    if row["classification_code"] is not None
                    else None
                ),
                classification_label=(
                    str(row["classification_label"])
                    if row["classification_label"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def _raw_search_count_sync(
        self,
        query: str,
        classification_code: str | None,
        document_status: DocumentStatus | None,
        filename_contains: str | None,
        created_after: datetime | None,
        created_before: datetime | None,
        phrase: bool = False,
    ) -> int:
        match_query = normalize_search_query(query, phrase=phrase)
        filename_pattern = f"%{_escape_like(filename_contains)}%" if filename_contains else None
        parameters: tuple[object, ...] = (
            match_query,
            classification_code,
            classification_code,
            document_status.value if document_status else None,
            document_status.value if document_status else None,
            filename_pattern,
            filename_pattern,
            created_after.isoformat() if created_after else None,
            created_after.isoformat() if created_after else None,
            created_before.isoformat() if created_before else None,
            created_before.isoformat() if created_before else None,
        )
        with self.database.connect() as connection:
            try:
                row = connection.execute(
                    """
                    SELECT COUNT(DISTINCT raw_content_fts.document_id) AS count
                    FROM raw_content_fts
                    JOIN documents ON documents.id = raw_content_fts.document_id
                    LEFT JOIN classifications
                      ON classifications.document_id = raw_content_fts.document_id
                    WHERE raw_content_fts MATCH ?
                      AND (? IS NULL OR classifications.code = ?)
                      AND (? IS NULL OR documents.status = ?)
                      AND (? IS NULL OR documents.filename LIKE ? ESCAPE '~')
                      AND (? IS NULL OR documents.created_at >= ?)
                      AND (? IS NULL OR documents.created_at <= ?)
                    """,
                    parameters,
                ).fetchone()
            except sqlite3.OperationalError as exc:
                raise ValueError("Invalid search query") from exc
        return int(row["count"]) if row else 0

    def _raw_search_results_sync(
        self,
        query: str,
        limit: int,
        offset: int,
        classification_code: str | None,
        document_status: DocumentStatus | None,
        filename_contains: str | None,
        created_after: datetime | None,
        created_before: datetime | None,
        phrase: bool = False,
    ) -> list[SearchResult]:
        match_query = normalize_search_query(query, phrase=phrase)
        filename_pattern = f"%{_escape_like(filename_contains)}%" if filename_contains else None
        parameters: tuple[object, ...] = (
            match_query,
            classification_code,
            classification_code,
            document_status.value if document_status else None,
            document_status.value if document_status else None,
            filename_pattern,
            filename_pattern,
            created_after.isoformat() if created_after else None,
            created_after.isoformat() if created_after else None,
            created_before.isoformat() if created_before else None,
            created_before.isoformat() if created_before else None,
            limit,
            offset,
        )
        with self.database.connect() as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT
                      raw_content_fts.document_id,
                      documents.filename,
                      documents.status AS document_status,
                      classifications.code AS classification_code,
                      classifications.label AS classification_label,
                      snippet(raw_content_fts, 1, '<mark>', '</mark>', '...', 16) AS snippet,
                      bm25(raw_content_fts) AS score
                    FROM raw_content_fts
                    JOIN documents ON documents.id = raw_content_fts.document_id
                    LEFT JOIN classifications
                      ON classifications.document_id = raw_content_fts.document_id
                    WHERE raw_content_fts MATCH ?
                      AND (? IS NULL OR classifications.code = ?)
                      AND (? IS NULL OR documents.status = ?)
                      AND (? IS NULL OR documents.filename LIKE ? ESCAPE '~')
                      AND (? IS NULL OR documents.created_at >= ?)
                      AND (? IS NULL OR documents.created_at <= ?)
                    ORDER BY score ASC
                    LIMIT ? OFFSET ?
                    """,
                    parameters,
                ).fetchall()
            except sqlite3.OperationalError as exc:
                raise ValueError("Invalid search query") from exc
        return [
            SearchResult(
                document_id=DocumentId(str(row["document_id"])),
                run_id=None,
                source="raw",
                filename=str(row["filename"]),
                document_status=DocumentStatus(str(row["document_status"])),
                snippet=str(row["snippet"]),
                score=float(row["score"]),
                classification_code=(
                    str(row["classification_code"])
                    if row["classification_code"] is not None
                    else None
                ),
                classification_label=(
                    str(row["classification_label"])
                    if row["classification_label"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def _search_facets_sync(
        self,
        query: str,
        classification_code: str | None,
        document_status: DocumentStatus | None,
        filename_contains: str | None,
        created_after: datetime | None,
        created_before: datetime | None,
        scope: SearchScope,
        phrase: bool = False,
    ) -> SearchFacets:
        if scope == "raw":
            return self._raw_search_facets_sync(
                query,
                classification_code,
                document_status,
                filename_contains,
                created_after,
                created_before,
                phrase,
            )
        match_query = normalize_search_query(query, phrase=phrase)
        filename_pattern = f"%{_escape_like(filename_contains)}%" if filename_contains else None
        filter_parameters: tuple[object, ...] = (
            match_query,
            RunStatus.SUCCEEDED.value,
            classification_code,
            classification_code,
            document_status.value if document_status else None,
            document_status.value if document_status else None,
            filename_pattern,
            filename_pattern,
            created_after.isoformat() if created_after else None,
            created_after.isoformat() if created_after else None,
            created_before.isoformat() if created_before else None,
            created_before.isoformat() if created_before else None,
        )
        with self.database.connect() as connection:
            try:
                classification_rows = connection.execute(
                    """
                    SELECT classifications.code, classifications.label,
                           COUNT(DISTINCT cleaned_outputs_fts.document_id) AS count
                    FROM cleaned_outputs_fts
                    JOIN runs ON runs.id = cleaned_outputs_fts.run_id
                    JOIN documents ON documents.id = cleaned_outputs_fts.document_id
                    JOIN cleaned_outputs
                      ON cleaned_outputs.document_id = cleaned_outputs_fts.document_id
                     AND cleaned_outputs.run_id = cleaned_outputs_fts.run_id
                    LEFT JOIN classifications
                      ON classifications.document_id = cleaned_outputs_fts.document_id
                    WHERE cleaned_outputs_fts MATCH ?
                      AND runs.status = ?
                      AND cleaned_outputs.created_at = (
                        SELECT MAX(latest.created_at)
                        FROM cleaned_outputs AS latest
                        WHERE latest.document_id = cleaned_outputs.document_id
                      )
                      AND (? IS NULL OR classifications.code = ?)
                      AND (? IS NULL OR documents.status = ?)
                      AND (? IS NULL OR documents.filename LIKE ? ESCAPE '~')
                      AND (? IS NULL OR documents.created_at >= ?)
                      AND (? IS NULL OR documents.created_at <= ?)
                    GROUP BY classifications.code, classifications.label
                    ORDER BY count DESC, classifications.code ASC
                    """,
                    filter_parameters,
                ).fetchall()
                status_rows = connection.execute(
                    """
                    SELECT documents.status,
                           COUNT(DISTINCT cleaned_outputs_fts.document_id) AS count
                    FROM cleaned_outputs_fts
                    JOIN runs ON runs.id = cleaned_outputs_fts.run_id
                    JOIN documents ON documents.id = cleaned_outputs_fts.document_id
                    JOIN cleaned_outputs
                      ON cleaned_outputs.document_id = cleaned_outputs_fts.document_id
                     AND cleaned_outputs.run_id = cleaned_outputs_fts.run_id
                    LEFT JOIN classifications
                      ON classifications.document_id = cleaned_outputs_fts.document_id
                    WHERE cleaned_outputs_fts MATCH ?
                      AND runs.status = ?
                      AND cleaned_outputs.created_at = (
                        SELECT MAX(latest.created_at)
                        FROM cleaned_outputs AS latest
                        WHERE latest.document_id = cleaned_outputs.document_id
                      )
                      AND (? IS NULL OR classifications.code = ?)
                      AND (? IS NULL OR documents.status = ?)
                      AND (? IS NULL OR documents.filename LIKE ? ESCAPE '~')
                      AND (? IS NULL OR documents.created_at >= ?)
                      AND (? IS NULL OR documents.created_at <= ?)
                    GROUP BY documents.status
                    ORDER BY count DESC, documents.status ASC
                    """,
                    filter_parameters,
                ).fetchall()
                filename_rows = connection.execute(
                    """
                    SELECT documents.filename,
                           COUNT(DISTINCT cleaned_outputs_fts.document_id) AS count
                    FROM cleaned_outputs_fts
                    JOIN runs ON runs.id = cleaned_outputs_fts.run_id
                    JOIN documents ON documents.id = cleaned_outputs_fts.document_id
                    JOIN cleaned_outputs
                      ON cleaned_outputs.document_id = cleaned_outputs_fts.document_id
                     AND cleaned_outputs.run_id = cleaned_outputs_fts.run_id
                    LEFT JOIN classifications
                      ON classifications.document_id = cleaned_outputs_fts.document_id
                    WHERE cleaned_outputs_fts MATCH ?
                      AND runs.status = ?
                      AND cleaned_outputs.created_at = (
                        SELECT MAX(latest.created_at)
                        FROM cleaned_outputs AS latest
                        WHERE latest.document_id = cleaned_outputs.document_id
                      )
                      AND (? IS NULL OR classifications.code = ?)
                      AND (? IS NULL OR documents.status = ?)
                      AND (? IS NULL OR documents.filename LIKE ? ESCAPE '~')
                      AND (? IS NULL OR documents.created_at >= ?)
                      AND (? IS NULL OR documents.created_at <= ?)
                    GROUP BY documents.filename
                    ORDER BY count DESC, documents.filename ASC
                    """,
                    filter_parameters,
                ).fetchall()
            except sqlite3.OperationalError as exc:
                raise ValueError("Invalid search query") from exc
        total = sum(int(row["count"]) for row in filename_rows)
        return SearchFacets(
            classifications=_classification_facets(classification_rows),
            statuses=tuple(
                SearchFacetValue(value=str(row["status"]), count=int(row["count"]))
                for row in status_rows
            ),
            sources=(SearchFacetValue(value="cleaned", count=total),),
            filenames=tuple(
                SearchFacetValue(value=str(row["filename"]), count=int(row["count"]))
                for row in filename_rows
            ),
        )

    def _raw_search_facets_sync(
        self,
        query: str,
        classification_code: str | None,
        document_status: DocumentStatus | None,
        filename_contains: str | None,
        created_after: datetime | None,
        created_before: datetime | None,
        phrase: bool = False,
    ) -> SearchFacets:
        match_query = normalize_search_query(query, phrase=phrase)
        filename_pattern = f"%{_escape_like(filename_contains)}%" if filename_contains else None
        filter_parameters: tuple[object, ...] = (
            match_query,
            classification_code,
            classification_code,
            document_status.value if document_status else None,
            document_status.value if document_status else None,
            filename_pattern,
            filename_pattern,
            created_after.isoformat() if created_after else None,
            created_after.isoformat() if created_after else None,
            created_before.isoformat() if created_before else None,
            created_before.isoformat() if created_before else None,
        )
        with self.database.connect() as connection:
            try:
                classification_rows = connection.execute(
                    """
                    SELECT classifications.code, classifications.label,
                           COUNT(DISTINCT raw_content_fts.document_id) AS count
                    FROM raw_content_fts
                    JOIN documents ON documents.id = raw_content_fts.document_id
                    LEFT JOIN classifications
                      ON classifications.document_id = raw_content_fts.document_id
                    WHERE raw_content_fts MATCH ?
                      AND (? IS NULL OR classifications.code = ?)
                      AND (? IS NULL OR documents.status = ?)
                      AND (? IS NULL OR documents.filename LIKE ? ESCAPE '~')
                      AND (? IS NULL OR documents.created_at >= ?)
                      AND (? IS NULL OR documents.created_at <= ?)
                    GROUP BY classifications.code, classifications.label
                    ORDER BY count DESC, classifications.code ASC
                    """,
                    filter_parameters,
                ).fetchall()
                status_rows = connection.execute(
                    """
                    SELECT documents.status, COUNT(DISTINCT raw_content_fts.document_id) AS count
                    FROM raw_content_fts
                    JOIN documents ON documents.id = raw_content_fts.document_id
                    LEFT JOIN classifications
                      ON classifications.document_id = raw_content_fts.document_id
                    WHERE raw_content_fts MATCH ?
                      AND (? IS NULL OR classifications.code = ?)
                      AND (? IS NULL OR documents.status = ?)
                      AND (? IS NULL OR documents.filename LIKE ? ESCAPE '~')
                      AND (? IS NULL OR documents.created_at >= ?)
                      AND (? IS NULL OR documents.created_at <= ?)
                    GROUP BY documents.status
                    ORDER BY count DESC, documents.status ASC
                    """,
                    filter_parameters,
                ).fetchall()
                filename_rows = connection.execute(
                    """
                    SELECT documents.filename, COUNT(DISTINCT raw_content_fts.document_id) AS count
                    FROM raw_content_fts
                    JOIN documents ON documents.id = raw_content_fts.document_id
                    LEFT JOIN classifications
                      ON classifications.document_id = raw_content_fts.document_id
                    WHERE raw_content_fts MATCH ?
                      AND (? IS NULL OR classifications.code = ?)
                      AND (? IS NULL OR documents.status = ?)
                      AND (? IS NULL OR documents.filename LIKE ? ESCAPE '~')
                      AND (? IS NULL OR documents.created_at >= ?)
                      AND (? IS NULL OR documents.created_at <= ?)
                    GROUP BY documents.filename
                    ORDER BY count DESC, documents.filename ASC
                    """,
                    filter_parameters,
                ).fetchall()
            except sqlite3.OperationalError as exc:
                raise ValueError("Invalid search query") from exc
        total = sum(int(row["count"]) for row in filename_rows)
        return SearchFacets(
            classifications=_classification_facets(classification_rows),
            statuses=tuple(
                SearchFacetValue(value=str(row["status"]), count=int(row["count"]))
                for row in status_rows
            ),
            sources=(SearchFacetValue(value="raw", count=total),),
            filenames=tuple(
                SearchFacetValue(value=str(row["filename"]), count=int(row["count"]))
                for row in filename_rows
            ),
        )

    def _emit_sync(self, run_id: RunId, stage: RunStage, message: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO run_events (run_id, stage, message, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (str(run_id), stage.value, message, utc_now().isoformat()),
            )

    def _list_events_sync(self, run_id: RunId, limit: int, offset: int) -> list[str]:
        _validate_event_page(limit=limit, offset=offset)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT stage, message
                FROM run_events
                WHERE run_id = ?
                ORDER BY id ASC
                LIMIT ? OFFSET ?
                """,
                (str(run_id), limit, offset),
            ).fetchall()
        return [f"{row['stage']}: {row['message']}" for row in rows]

    def _list_event_records_sync(self, run_id: RunId, limit: int, offset: int) -> list[RunEvent]:
        _validate_event_page(limit=limit, offset=offset)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, run_id, stage, message, created_at
                FROM run_events
                WHERE run_id = ?
                ORDER BY id ASC
                LIMIT ? OFFSET ?
                """,
                (str(run_id), limit, offset),
            ).fetchall()
        return [
            RunEvent(
                run_id=RunId(str(row["run_id"])),
                stage=RunStage(str(row["stage"])),
                message=str(row["message"]),
                created_at=datetime.fromisoformat(str(row["created_at"])),
                sequence=int(row["id"]),
            )
            for row in rows
        ]


def _validate_event_page(*, limit: int, offset: int) -> None:
    if limit < 1 or limit > _MAX_EVENT_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {_MAX_EVENT_PAGE_SIZE}")
    if offset < 0:
        raise ValueError("offset must be greater than or equal to 0")


def normalize_search_query(query: str, *, phrase: bool = False) -> str:
    """Return a safe FTS query for punctuation-heavy user input."""
    if len(query) > _MAX_SEARCH_QUERY_CHARS:
        raise ValueError(
            f"Search query exceeds configured limit of {_MAX_SEARCH_QUERY_CHARS} characters"
        )
    if phrase:
        normalized_phrase = _normalize_search_phrase(query)
        if not normalized_phrase:
            raise ValueError("Invalid search query")
        return f'"{normalized_phrase}"'
    parts: list[str] = []
    unquoted: list[str] = []
    in_quote = False
    quoted: list[str] = []
    for char in query:
        if char == '"':
            if in_quote:
                quoted_phrase = _normalize_search_phrase("".join(quoted))
                if quoted_phrase:
                    parts.append(f'"{quoted_phrase}"')
                quoted = []
                in_quote = False
            else:
                in_quote = True
            continue
        if in_quote:
            quoted.append(char)
        else:
            unquoted.append(char)
    if in_quote and not unquoted:
        raise ValueError("Invalid search query")
    if in_quote:
        unquoted.extend(quoted)
    parts.extend(_SEARCH_TOKEN_RE.findall("".join(unquoted).casefold()))
    if not parts:
        raise ValueError("Invalid search query")
    return " ".join(parts)


def _normalize_search_phrase(value: str) -> str:
    return " ".join(_SEARCH_TOKEN_RE.findall(value.casefold()))


def _escape_like(value: str) -> str:
    return value.replace("~", "~~").replace("%", "~%").replace("_", "~_")


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

    async def heartbeat(self, run_id: RunId, *, worker_id: str, lease_seconds: int) -> bool:
        """Renew a worker lease for a running queue row."""
        return await asyncio.to_thread(self._heartbeat_sync, run_id, worker_id, lease_seconds)

    async def complete(self, run_id: RunId, *, worker_id: str | None = None) -> None:
        """Mark a queued run complete."""
        await asyncio.to_thread(self._complete_sync, run_id, worker_id)

    async def fail(
        self,
        run_id: RunId,
        *,
        error: str,
        max_attempts: int,
        worker_id: str | None = None,
    ) -> None:
        """Mark a queued run failed or schedule a retry."""
        await asyncio.to_thread(self._fail_sync, run_id, error, max_attempts, worker_id)

    async def cancel(self, run_id: RunId, *, error: str | None = None) -> None:
        """Mark a queued run canceled."""
        await asyncio.to_thread(self._cancel_sync, run_id, error)

    async def list(self, *, limit: int = 100, offset: int = 0) -> tuple[QueuedRun, ...]:
        """List queued run state."""
        return await asyncio.to_thread(self._list_sync, limit, offset)

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
                SELECT run_queue.*
                FROM run_queue
                JOIN runs ON runs.id = run_queue.run_id
                WHERE run_queue.status IN (?, ?, ?)
                  AND run_queue.available_at <= ?
                  AND (run_queue.locked_at IS NULL OR run_queue.locked_at <= ?)
                  AND runs.status NOT IN (?, ?)
                ORDER BY run_queue.updated_at ASC
                LIMIT 1
                """,
                (
                    QueueStatus.QUEUED.value,
                    QueueStatus.RETRY.value,
                    QueueStatus.RUNNING.value,
                    now.isoformat(),
                    lease_cutoff.isoformat(),
                    RunStatus.SUCCEEDED.value,
                    RunStatus.CANCELED.value,
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

    def _heartbeat_sync(self, run_id: RunId, worker_id: str, lease_seconds: int) -> bool:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be at least 1")
        now = utc_now().isoformat()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE run_queue
                SET locked_at = ?, updated_at = ?
                WHERE run_id = ?
                  AND status = ?
                  AND locked_by = ?
                """,
                (
                    now,
                    now,
                    str(run_id),
                    QueueStatus.RUNNING.value,
                    worker_id,
                ),
            )
        return cursor.rowcount > 0

    def _complete_sync(self, run_id: RunId, worker_id: str | None) -> None:
        now = utc_now().isoformat()
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE run_queue
                SET status = ?, locked_at = NULL, locked_by = NULL, updated_at = ?
                WHERE run_id = ?
                  AND status = ?
                  AND (? IS NULL OR locked_by = ?)
                """,
                (
                    QueueStatus.SUCCEEDED.value,
                    now,
                    str(run_id),
                    QueueStatus.RUNNING.value,
                    worker_id,
                    worker_id,
                ),
            )

    def _fail_sync(
        self,
        run_id: RunId,
        error: str,
        max_attempts: int,
        worker_id: str | None,
    ) -> None:
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
                  AND (? IS NULL OR locked_by = ?)
                """,
                (
                    status.value,
                    available_at.isoformat(),
                    now.isoformat(),
                    error,
                    str(run_id),
                    QueueStatus.RUNNING.value,
                    worker_id,
                    worker_id,
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

    def _list_sync(self, limit: int, offset: int) -> tuple[QueuedRun, ...]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if offset < 0:
            raise ValueError("offset must be greater than or equal to 0")
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM run_queue
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
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


def _classification_facets(rows: Sequence[sqlite3.Row]) -> tuple[SearchFacetValue, ...]:
    return tuple(
        SearchFacetValue(
            value=str(row["code"]) if row["code"] is not None else "",
            label=str(row["label"]) if row["label"] is not None else None,
            count=int(row["count"]),
        )
        for row in rows
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
