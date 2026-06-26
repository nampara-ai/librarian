import asyncio
import logging
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

from librarian.application.factory import build_container
from librarian.application.jobs import InProcessJobRunner, QueueStatus, QueueWorker, RunQueue
from librarian.config import Settings
from librarian.domain.ids import DocumentId, RunId
from librarian.domain.models import (
    Classification,
    Document,
    DocumentStatus,
    RunStage,
    RunStatus,
    SourceFile,
)
from librarian.observability import MetricsRecorder
from librarian.storage.sqlite import (
    SQLiteDatabase,
    SQLiteRepository,
    SQLiteRunQueue,
    normalize_search_query,
)


class FakeSpan:
    def __init__(self, name: str, attributes: dict[str, object]) -> None:
        self.name = name
        self.attributes = dict(attributes)

    def __enter__(self) -> "FakeSpan":
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


class FakeTracer:
    def __init__(self) -> None:
        self.spans: list[FakeSpan] = []

    def start_as_current_span(
        self,
        name: str,
        *,
        attributes: dict[str, object],
    ) -> FakeSpan:
        span = FakeSpan(name, attributes)
        self.spans.append(span)
        return span


@pytest.mark.asyncio
async def test_sqlite_initializes_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "librarian.sqlite"
    database = SQLiteDatabase(database_path)

    await database.initialize()

    assert database_path.exists()
    with database.connect() as connection:
        rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = connection.execute("PRAGMA synchronous").fetchone()[0]

    assert [row["version"] for row in rows] == [
        "0001_initial.sql",
        "0002_run_queue.sql",
        "0003_document_scoped_chunks.sql",
        "0004_raw_content_fts.sql",
        "0005_api_audit_events.sql",
        "0006_classification_title_tags.sql",
        "0007_classification_description.sql",
        "0008_classification_series.sql",
        "0009_extraction_cache.sql",
    ]
    assert busy_timeout == 5000
    assert str(journal_mode).lower() == "wal"
    assert synchronous == 1


@pytest.mark.asyncio
async def test_sqlite_classification_round_trips_series_fields(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "librarian.sqlite")
    await database.initialize()
    repository = SQLiteRepository(database)
    document = Document(
        id=DocumentId("doc_series"),
        source=SourceFile(
            path=tmp_path / "cbre.pdf",
            filename="cbre.pdf",
            media_type="application/pdf",
            byte_size=10,
            sha256="series-sha",
        ),
    )
    await repository.save_document(document)
    classification = Classification(
        document_id=document.id,
        code="330",
        label="Economics",
        summary="Quarterly office market report.",
        confidence=0.8,
        title="CBRE Dallas Office MarketView",
        tags=("office", "dallas"),
        description="A Dallas office market update.",
        issuer="CBRE",
        series_key="cbre-marketview-dallas-office",
        series_title="CBRE MarketView — Dallas Office",
        period="2026-06",
    )

    await repository.save_classification(classification)
    loaded = await repository.get_classification(document.id)

    assert loaded == classification


@pytest.mark.asyncio
async def test_sqlite_write_waits_for_busy_writer_and_succeeds(tmp_path: Path) -> None:
    database_path = tmp_path / "librarian.sqlite"
    database = SQLiteDatabase(database_path)
    await database.initialize()
    repository = SQLiteRepository(database)
    document = Document(
        id=DocumentId("doc_busy"),
        source=SourceFile(
            path=tmp_path / "busy.txt",
            filename="busy.txt",
            media_type="text/plain",
            byte_size=4,
            sha256="busy-sha",
        ),
        status=DocumentStatus.INGESTED,
    )
    writer_has_lock = Event()
    release_writer = Event()

    def hold_writer_lock() -> None:
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO content_blobs (key, text, created_at) VALUES (?, ?, ?)",
                ("busy-lock", "held", "2026-05-12T00:00:00+00:00"),
            )
            writer_has_lock.set()
            while not release_writer.is_set():
                time.sleep(0.01)

    lock_task = asyncio.create_task(asyncio.to_thread(hold_writer_lock))
    await asyncio.wait_for(asyncio.to_thread(writer_has_lock.wait), timeout=1)
    save_task = asyncio.create_task(repository.save_document(document))
    await asyncio.sleep(0.1)

    assert not save_task.done()

    release_writer.set()
    await asyncio.wait_for(lock_task, timeout=1)
    await asyncio.wait_for(save_task, timeout=2)

    saved = await repository.get_document(document.id)
    assert saved is not None
    assert saved.id == document.id


@pytest.mark.asyncio
async def test_sqlite_surfaces_busy_timeout_after_configured_wait(tmp_path: Path) -> None:
    database_path = tmp_path / "librarian.sqlite"
    database = SQLiteDatabase(database_path)
    await database.initialize()
    repository = SQLiteRepository(database)
    document = Document(
        id=DocumentId("doc_busy_timeout"),
        source=SourceFile(
            path=tmp_path / "busy-timeout.txt",
            filename="busy-timeout.txt",
            media_type="text/plain",
            byte_size=4,
            sha256="busy-timeout-sha",
        ),
        status=DocumentStatus.INGESTED,
    )
    with database.connect() as locker:
        locker.execute("BEGIN IMMEDIATE")
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            await repository.save_document(document)

    assert time.monotonic() - start >= 4.5


@pytest.mark.asyncio
async def test_sqlite_maintenance_runs_checkpoint_and_optional_vacuum(tmp_path: Path) -> None:
    database_path = tmp_path / "librarian.sqlite"
    database = SQLiteDatabase(database_path)
    await database.initialize()

    result = await database.maintain(vacuum=True)

    assert result.checkpoint_busy == 0
    assert result.checkpoint_log_frames >= 0
    assert result.checkpoint_checkpointed_frames >= 0
    assert result.vacuumed is True


@pytest.mark.asyncio
async def test_sqlite_storage_stats_report_growth_inputs(tmp_path: Path) -> None:
    database_path = tmp_path / "librarian.sqlite"
    database = SQLiteDatabase(database_path)
    await database.initialize()
    repository = SQLiteRepository(database)
    document = Document(
        id=DocumentId("doc_stats"),
        source=SourceFile(
            path=tmp_path / "stats.txt",
            filename="stats.txt",
            media_type="text/plain",
            byte_size=19,
            sha256="stats-sha",
        ),
        status=DocumentStatus.INGESTED,
    )
    await repository.save_document(document)
    await repository.put_text("raw:doc_stats", "stable raw storage text")

    stats = await database.stats()

    assert stats.database_path == database_path
    assert stats.database_file_bytes > 0
    assert stats.total_sqlite_bytes >= stats.database_file_bytes
    assert stats.page_size_bytes > 0
    assert stats.page_count > 0
    assert stats.used_page_bytes > 0
    assert stats.table_counts["documents"] == 1
    assert stats.table_counts["content_blobs"] == 1
    assert stats.table_counts["api_audit_events"] == 0
    assert stats.source_file_bytes == 19
    assert stats.content_blob_text_bytes == len("stable raw storage text")


@pytest.mark.asyncio
async def test_sqlite_backup_creates_consistent_copy(tmp_path: Path) -> None:
    database_path = tmp_path / "librarian.sqlite"
    backup_path = tmp_path / "backups" / "librarian-backup.sqlite"
    database = SQLiteDatabase(database_path)
    await database.initialize()
    repository = SQLiteRepository(database)
    document = Document(
        id=DocumentId("doc_backup"),
        source=SourceFile(
            path=tmp_path / "backup.txt",
            filename="backup.txt",
            media_type="text/plain",
            byte_size=6,
            sha256="backup-sha",
        ),
        status=DocumentStatus.INGESTED,
    )
    await repository.save_document(document)

    result = await database.backup(backup_path)

    assert result.destination_path == backup_path.resolve()
    assert result.byte_size > 0
    copied = SQLiteRepository(SQLiteDatabase(backup_path))
    restored = await copied.get_document(document.id)
    assert restored is not None
    assert restored.id == document.id
    with pytest.raises(FileExistsError):
        await database.backup(backup_path)
    overwritten = await database.backup(backup_path, overwrite=True)
    assert overwritten.byte_size > 0


@pytest.mark.asyncio
async def test_sqlite_backup_rejects_symlink_destination(tmp_path: Path) -> None:
    database_path = tmp_path / "librarian.sqlite"
    database = SQLiteDatabase(database_path)
    await database.initialize()
    outside_target = tmp_path / "outside.sqlite"
    outside_target.write_text("do not overwrite", encoding="utf-8")
    backup_link = tmp_path / "backup-link.sqlite"
    backup_link.symlink_to(outside_target)

    with pytest.raises(ValueError, match="must not be a symlink"):
        await database.backup(backup_link, overwrite=True)

    assert outside_target.read_text(encoding="utf-8") == "do not overwrite"


@pytest.mark.asyncio
async def test_sqlite_backup_rejects_symlink_parent(tmp_path: Path) -> None:
    database_path = tmp_path / "librarian.sqlite"
    database = SQLiteDatabase(database_path)
    await database.initialize()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="Backup destination must not cross a symlinked parent"):
        await database.backup(linked_parent / "backup.sqlite", overwrite=True)

    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_sqlite_restore_replaces_database_from_verified_backup(tmp_path: Path) -> None:
    database_path = tmp_path / "librarian.sqlite"
    backup_path = tmp_path / "backup.sqlite"
    database = SQLiteDatabase(database_path)
    await database.initialize()
    repository = SQLiteRepository(database)
    first = Document(
        id=DocumentId("doc_before_restore"),
        source=SourceFile(
            path=tmp_path / "before.txt",
            filename="before.txt",
            media_type="text/plain",
            byte_size=6,
            sha256="before-sha",
        ),
        status=DocumentStatus.INGESTED,
    )
    second = Document(
        id=DocumentId("doc_after_backup"),
        source=SourceFile(
            path=tmp_path / "after.txt",
            filename="after.txt",
            media_type="text/plain",
            byte_size=5,
            sha256="after-sha",
        ),
        status=DocumentStatus.INGESTED,
    )
    await repository.save_document(first)
    await database.backup(backup_path)
    await repository.save_document(second)

    result = await database.restore(backup_path)

    assert result.source_path == backup_path.resolve()
    assert result.byte_size > 0
    restored_repository = SQLiteRepository(SQLiteDatabase(database_path))
    assert await restored_repository.get_document(first.id) is not None
    assert await restored_repository.get_document(second.id) is None


@pytest.mark.asyncio
async def test_sqlite_restore_rejects_symlink_destination(tmp_path: Path) -> None:
    source_database_path = tmp_path / "source.sqlite"
    backup_path = tmp_path / "backup.sqlite"
    source_database = SQLiteDatabase(source_database_path)
    await source_database.initialize()
    await source_database.backup(backup_path)
    outside_target = tmp_path / "outside.sqlite"
    outside_target.write_text("do not overwrite", encoding="utf-8")
    database_link = tmp_path / "librarian.sqlite"
    database_link.symlink_to(outside_target)

    with pytest.raises(ValueError, match="destination must not be a symlink"):
        await SQLiteDatabase(database_link).restore(backup_path)

    assert outside_target.read_text(encoding="utf-8") == "do not overwrite"


@pytest.mark.asyncio
async def test_sqlite_restore_rejects_symlink_parent(tmp_path: Path) -> None:
    source_database_path = tmp_path / "source.sqlite"
    backup_path = tmp_path / "backup.sqlite"
    source_database = SQLiteDatabase(source_database_path)
    await source_database.initialize()
    await source_database.backup(backup_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="Restore destination must not cross a symlinked parent"):
        await SQLiteDatabase(linked_parent / "librarian.sqlite").restore(backup_path)

    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_sqlite_restore_rejects_invalid_backup(tmp_path: Path) -> None:
    database_path = tmp_path / "librarian.sqlite"
    backup_path = tmp_path / "invalid.sqlite"
    backup_path.write_text("not sqlite", encoding="utf-8")
    database = SQLiteDatabase(database_path)

    with pytest.raises(RuntimeError, match="failed integrity check"):
        await database.restore(backup_path)


@pytest.mark.asyncio
async def test_sqlite_verify_reports_integrity_and_migrations(tmp_path: Path) -> None:
    database_path = tmp_path / "librarian.sqlite"
    database = SQLiteDatabase(database_path)
    await database.initialize()

    result = await database.verify()

    assert result.ok is True
    assert result.database_path == database_path.resolve()
    assert result.integrity_ok is True
    assert result.foreign_key_violations == 0
    assert result.applied_migrations >= 1


def test_normalize_search_query_preserves_safe_quoted_phrases() -> None:
    assert normalize_search_query('"follow-up care" horse?!') == '"follow up care" AND horse'
    assert normalize_search_query("follow-up care", phrase=True) == '"follow up care"'
    assert normalize_search_query("follow-up care") == "((follow AND up) OR followup) AND care"
    assert normalize_search_query("co-operate/horse") == (
        "((co AND operate AND horse) OR cooperatehorse)"
    )
    assert normalize_search_query('horse "follow-up') == (
        "horse AND ((follow AND up) OR followup)"
    )
    assert normalize_search_query("children's hospital horse\u2019s gait") == (
        "children AND hospital AND horse AND gait"
    )
    assert normalize_search_query('"children\u2019s hospital"') == '"children s hospital"'
    with pytest.raises(ValueError, match="Invalid search query"):
        normalize_search_query('"')
    with pytest.raises(ValueError, match="Search query exceeds configured limit"):
        normalize_search_query("x" * 4097)


@pytest.mark.asyncio
async def test_raw_content_fts_migration_backfills_existing_raw_blobs(tmp_path: Path) -> None:
    database_path = tmp_path / "librarian.sqlite"
    database = SQLiteDatabase(database_path)
    await database.initialize()
    with database.connect() as connection:
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            ("0004_raw_content_fts.sql",),
        )
        connection.execute("DROP TABLE raw_content_fts")
        connection.execute(
            """
            INSERT INTO documents (
              id, source_path, filename, media_type, byte_size, sha256,
              status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))
            """,
            (
                "doc_backfill",
                str(tmp_path / "legacy.txt"),
                "legacy.txt",
                "text/plain",
                12,
                "sha",
                "ingested",
            ),
        )
        connection.execute(
            "INSERT INTO content_blobs (key, text, created_at) VALUES (?, ?, datetime('now'))",
            ("raw:doc_backfill", "Legacy raw searchable horse text"),
        )

    await database.initialize()
    repository = SQLiteRepository(database)

    results = await repository.search_results("searchable horse", scope="raw")

    assert str(results[0].document_id) == "doc_backfill"
    assert results[0].source == "raw"


@pytest.mark.asyncio
async def test_sqlite_search_snippets_escape_source_markup(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    source = tmp_path / "unsafe.txt"
    source.write_text(
        "<script>alert(1)</script> horse <mark>not trusted</mark>",
        encoding="utf-8",
    )

    await container.ingest_document.execute(source)
    results = await container.repository.search_results("script horse", scope="raw")

    assert results
    snippet = results[0].snippet
    assert "<mark>script</mark>" in snippet
    assert "<mark>horse</mark>" in snippet
    assert "&lt;" in snippet
    assert "&gt;" in snippet
    assert "&lt;mark&gt;not trusted&lt;/mark&gt;" in snippet
    assert "<script>" not in snippet
    assert "</script>" not in snippet
    assert "<mark>not trusted</mark>" not in snippet


@pytest.mark.asyncio
async def test_sqlite_search_broad_query_ignores_possessive_suffixes(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    container = await build_container(settings)
    plain_source = tmp_path / "plain.txt"
    possessive_source = tmp_path / "possessive.txt"
    plain_source.write_text("Children hospital discharge guidance.", encoding="utf-8")
    possessive_source.write_text("Children's hospital intake guidance.", encoding="utf-8")
    plain_document = await container.ingest_document.execute(plain_source)
    possessive_document = await container.ingest_document.execute(possessive_source)

    results = await container.repository.search_results("children's hospital", scope="raw")
    result_ids = {result.document_id for result in results}

    assert result_ids == {
        plain_document.document.id,
        possessive_document.document.id,
    }


@pytest.mark.asyncio
async def test_sqlite_search_supports_offset_and_created_date_filters(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    container = await build_container(settings)
    old_source = tmp_path / "old.txt"
    new_source = tmp_path / "new.txt"
    old_source.write_text("Horse pagination transcript old fixture.", encoding="utf-8")
    new_source.write_text("Horse pagination transcript new fixture.", encoding="utf-8")

    old_ingested = await container.ingest_document.execute(old_source)
    new_ingested = await container.ingest_document.execute(new_source)

    old_created = datetime(2024, 1, 1, tzinfo=UTC)
    new_created = datetime(2024, 1, 2, tzinfo=UTC)
    with container.database.connect() as connection:
        connection.execute(
            "UPDATE documents SET created_at = ? WHERE id = ?",
            (old_created.isoformat(), str(old_ingested.document.id)),
        )
        connection.execute(
            "UPDATE documents SET created_at = ? WHERE id = ?",
            (new_created.isoformat(), str(new_ingested.document.id)),
        )

    all_results = await container.repository.search_results(
        "Horse pagination",
        limit=1,
        offset=1,
        scope="raw",
    )
    all_count = await container.repository.search_count("Horse pagination", scope="raw")
    recent_results = await container.repository.search_results(
        "Horse pagination",
        created_after=new_created - timedelta(hours=1),
        created_before=new_created + timedelta(hours=1),
        scope="raw",
    )
    recent_count = await container.repository.search_count(
        "Horse pagination",
        created_after=new_created - timedelta(hours=1),
        created_before=new_created + timedelta(hours=1),
        scope="raw",
    )

    assert len(all_results) == 1
    assert all_results[0].document_id == old_ingested.document.id
    assert all_results[0].created_at == old_created
    assert all_count == 2
    assert [item.document_id for item in recent_results] == [new_ingested.document.id]
    assert recent_results[0].created_at == new_created
    assert recent_count == 1


@pytest.mark.asyncio
async def test_sqlite_search_phrase_mode_requires_adjacent_terms(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    container = await build_container(settings)
    phrase_source = tmp_path / "phrase.txt"
    reordered_source = tmp_path / "reordered.txt"
    compact_source = tmp_path / "compact.txt"
    phrase_source.write_text("Follow-up care checklist for discharge.", encoding="utf-8")
    reordered_source.write_text("Care plans should follow up after discharge.", encoding="utf-8")
    compact_source.write_text("Followup care checklist for discharge.", encoding="utf-8")
    phrase_document = await container.ingest_document.execute(phrase_source)
    reordered_document = await container.ingest_document.execute(reordered_source)
    compact_document = await container.ingest_document.execute(compact_source)

    broad_results = await container.repository.search_results("follow-up care", scope="raw")
    phrase_results = await container.repository.search_results(
        "follow-up care",
        scope="raw",
        phrase=True,
    )
    phrase_count = await container.repository.search_count(
        "follow-up care",
        scope="raw",
        phrase=True,
    )
    phrase_facets = await container.repository.search_facets(
        "follow-up care",
        scope="raw",
        phrase=True,
    )

    assert {item.document_id for item in broad_results} == {
        phrase_document.document.id,
        reordered_document.document.id,
        compact_document.document.id,
    }
    assert [item.document_id for item in phrase_results] == [phrase_document.document.id]
    assert phrase_count == 1
    assert phrase_facets.sources[0].count == 1


@pytest.mark.asyncio
async def test_sqlite_search_recovers_terms_after_unclosed_quote(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    container = await build_container(settings)
    source = tmp_path / "notes.txt"
    source.write_text("Horse follow-up care discharge checklist.", encoding="utf-8")
    ingested = await container.ingest_document.execute(source)

    results = await container.repository.search_results('horse "follow-up', scope="raw")
    count = await container.repository.search_count('horse "follow-up', scope="raw")

    assert [item.document_id for item in results] == [ingested.document.id]
    assert count == 1


@pytest.mark.asyncio
async def test_sqlite_search_facets_honor_filters(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    container = await build_container(settings)
    old_source = tmp_path / "old-notes.txt"
    new_source = tmp_path / "new-notes.txt"
    old_source.write_text("Horse facet transcript old fixture.", encoding="utf-8")
    new_source.write_text("Horse facet transcript new fixture.", encoding="utf-8")

    old_ingested = await container.ingest_document.execute(old_source)
    new_ingested = await container.ingest_document.execute(new_source)

    old_created = datetime(2024, 1, 1, tzinfo=UTC)
    new_created = datetime(2024, 1, 2, tzinfo=UTC)
    with container.database.connect() as connection:
        connection.execute(
            "UPDATE documents SET created_at = ? WHERE id = ?",
            (old_created.isoformat(), str(old_ingested.document.id)),
        )
        connection.execute(
            "UPDATE documents SET created_at = ? WHERE id = ?",
            (new_created.isoformat(), str(new_ingested.document.id)),
        )

    facets = await container.repository.search_facets(
        "Horse facet",
        filename_contains="new",
        created_after=new_created - timedelta(hours=1),
        created_before=new_created + timedelta(hours=1),
        scope="raw",
    )

    assert [(item.value, item.count) for item in facets.filenames] == [("new-notes.txt", 1)]
    assert facets.sources[0].count == 1


@pytest.mark.asyncio
async def test_sqlite_search_facets_cap_buckets_without_losing_total(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    container = await build_container(settings)
    for index in range(3):
        source = tmp_path / f"facet-{index}.txt"
        source.write_text(
            f"Horse facet limit transcript fixture {index}.",
            encoding="utf-8",
        )
        await container.ingest_document.execute(source)

    facets = await container.repository.search_facets(
        "Horse facet",
        scope="raw",
        facet_limit=2,
    )

    assert facets.sources[0].count == 3
    assert [(item.value, item.count) for item in facets.filenames] == [
        ("facet-0.txt", 1),
        ("facet-1.txt", 1),
    ]


@pytest.mark.asyncio
async def test_sqlite_lists_structured_run_events(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript with event records.", encoding="utf-8")
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)
    repository = SQLiteRepository(container.database)

    await repository.emit(run.id, RunStage.CLEAN, "cleaned one chunk")
    await repository.emit(run.id, RunStage.COMPLETE, "finished events")

    events = await repository.list_event_records(run.id)
    assert events[-1].run_id == run.id
    assert events[-1].stage == RunStage.COMPLETE
    assert events[-1].message == "finished events"
    assert events[-1].sequence > 0
    page = await repository.list_events(run.id, limit=1, offset=1)
    record_page = await repository.list_event_records(run.id, limit=1, offset=1)
    assert len(page) == 1
    assert len(record_page) == 1


@pytest.mark.asyncio
async def test_sqlite_run_queue_claims_and_completes_run(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript with notes about saddle fit.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)
    queue = SQLiteRunQueue(container.database)

    await queue.enqueue(run.id)
    claimed = await queue.claim(worker_id="test-worker", lease_seconds=60)
    assert claimed is not None
    assert claimed.run_id == run.id
    assert claimed.status == QueueStatus.RUNNING
    assert claimed.attempts == 1
    assert len(await queue.list(limit=1, offset=0)) == 1
    assert await queue.list(limit=1, offset=1) == ()

    await queue.complete(run.id)
    assert await queue.claim(worker_id="test-worker", lease_seconds=60) is None


@pytest.mark.asyncio
async def test_sqlite_run_queue_claims_once_under_api_worker_contention(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    container = await build_container(settings)
    queue = SQLiteRunQueue(container.database)
    run_ids: list[RunId] = []

    for index in range(12):
        source = tmp_path / f"notes-{index}.txt"
        source.write_text(
            f"Horse transcript {index} with contention and queue notes.",
            encoding="utf-8",
        )
        ingested = await container.ingest_document.execute(source)
        run = await container.process_document.start(ingested.document.id)
        await queue.enqueue(run.id)
        run_ids.append(run.id)

    async def worker(worker_index: int) -> list[RunId]:
        worker_id = f"contention-worker-{worker_index}"
        worker_queue = SQLiteRunQueue(container.database)
        claimed: list[RunId] = []
        while True:
            item = await worker_queue.claim(worker_id=worker_id, lease_seconds=60)
            if item is None:
                return claimed
            claimed.append(item.run_id)
            await asyncio.sleep(0)
            await worker_queue.complete(item.run_id, worker_id=worker_id)

    async def api_like_repository_traffic() -> None:
        for index in range(24):
            await container.repository.list(limit=10, offset=0)
            await container.repository.search_count("contention")
            sidecar = Document(
                id=DocumentId(f"doc_api_contention_{index}"),
                source=SourceFile(
                    path=tmp_path / f"api-contention-{index}.txt",
                    filename=f"api-contention-{index}.txt",
                    media_type="text/plain",
                    byte_size=10,
                    sha256=f"api-contention-sha-{index}",
                ),
                status=DocumentStatus.INGESTED,
            )
            await container.repository.save_document(sidecar)
            await asyncio.sleep(0)

    worker_results, _ = await asyncio.gather(
        asyncio.gather(*(worker(index) for index in range(4))),
        api_like_repository_traffic(),
    )
    claimed_ids = [run_id for worker_claims in worker_results for run_id in worker_claims]

    assert sorted(str(run_id) for run_id in claimed_ids) == sorted(
        str(run_id) for run_id in run_ids
    )
    assert len({str(run_id) for run_id in claimed_ids}) == len(run_ids)
    rows = await queue.list(limit=100, offset=0)
    queued_rows = {row.run_id: row for row in rows if row.run_id in set(run_ids)}
    assert set(queued_rows) == set(run_ids)
    assert {row.status for row in queued_rows.values()} == {QueueStatus.SUCCEEDED}
    assert {row.attempts for row in queued_rows.values()} == {1}


@pytest.mark.asyncio
async def test_sqlite_run_queue_reclaims_expired_running_lease(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript with notes about queue leases.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)
    queue = SQLiteRunQueue(container.database)

    await queue.enqueue(run.id)
    first = await queue.claim(worker_id="first-worker", lease_seconds=60)
    assert first is not None
    assert await queue.claim(worker_id="second-worker", lease_seconds=60) is None
    with container.database.connect() as connection:
        connection.execute(
            """
            UPDATE run_queue
            SET locked_at = datetime('now', '-120 seconds')
            WHERE run_id = ?
            """,
            (str(run.id),),
        )

    second = await queue.claim(worker_id="second-worker", lease_seconds=60)

    assert second is not None
    assert second.run_id == run.id
    assert second.status == QueueStatus.RUNNING
    assert second.attempts == 2
    assert second.locked_by == "second-worker"


@pytest.mark.asyncio
async def test_queue_worker_heartbeat_prevents_active_lease_reclaim(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript with notes about active leases.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)
    queue = SQLiteRunQueue(container.database)
    metrics = MetricsRecorder()
    await queue.enqueue(run.id)
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_processor(_: RunId) -> object:
        started.set()
        await release.wait()
        return None

    worker = QueueWorker(
        queue=queue,
        processor=slow_processor,
        worker_id="active-worker",
        lease_seconds=1,
        heartbeat_interval_seconds=0.05,
        metrics=metrics,
    )
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(1.25)

    assert await queue.claim(worker_id="second-worker", lease_seconds=1) is None

    release.set()
    assert await asyncio.wait_for(task, timeout=1)
    rows = await queue.list()
    assert rows[0].status == QueueStatus.SUCCEEDED
    snapshot = metrics.snapshot()
    assert snapshot["queue_claims_total"] == 1
    assert snapshot["runs_completed_total"] == 1
    assert snapshot["run_stage_counts"] == {"queue_process": 1}


@pytest.mark.asyncio
async def test_queue_worker_fails_run_when_heartbeat_adapter_errors(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript with heartbeat failure.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)
    queue = SQLiteRunQueue(container.database)
    await queue.enqueue(run.id)
    started = asyncio.Event()
    release = asyncio.Event()

    class FailingHeartbeatQueue:
        def __init__(self, wrapped: SQLiteRunQueue) -> None:
            self._wrapped = wrapped

        async def enqueue(self, run_id: RunId) -> None:
            await self._wrapped.enqueue(run_id)

        async def claim(self, *, worker_id: str, lease_seconds: int):
            return await self._wrapped.claim(worker_id=worker_id, lease_seconds=lease_seconds)

        async def heartbeat(self, run_id: RunId, *, worker_id: str, lease_seconds: int) -> bool:
            del run_id, worker_id, lease_seconds
            raise RuntimeError("heartbeat down")

        async def complete(self, run_id: RunId, *, worker_id: str | None = None) -> None:
            await self._wrapped.complete(run_id, worker_id=worker_id)

        async def fail(
            self,
            run_id: RunId,
            *,
            error: str,
            max_attempts: int,
            worker_id: str | None = None,
        ) -> None:
            await self._wrapped.fail(
                run_id,
                error=error,
                max_attempts=max_attempts,
                worker_id=worker_id,
            )

        async def cancel(self, run_id: RunId, *, error: str | None = None) -> None:
            await self._wrapped.cancel(run_id, error=error)

        async def list(self, *, limit: int = 100, offset: int = 0):
            return await self._wrapped.list(limit=limit, offset=offset)

    async def slow_processor(_: RunId) -> object:
        started.set()
        await release.wait()
        return None

    queue_adapter: RunQueue = FailingHeartbeatQueue(queue)
    worker = QueueWorker(
        queue=queue_adapter,
        processor=slow_processor,
        worker_id="heartbeat-worker",
        lease_seconds=1,
        heartbeat_interval_seconds=0.01,
    )

    assert await worker.run_once()
    assert started.is_set()
    rows = await queue.list()
    assert rows[0].status == QueueStatus.RETRY
    assert "heartbeat down" in (rows[0].last_error or "")


@pytest.mark.asyncio
async def test_queue_completion_requires_current_worker_owner(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript with stale worker ownership.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)
    queue = SQLiteRunQueue(container.database)
    await queue.enqueue(run.id)
    assert await queue.claim(worker_id="first-worker", lease_seconds=60) is not None
    with container.database.connect() as connection:
        connection.execute(
            """
            UPDATE run_queue
            SET locked_at = datetime('now', '-120 seconds')
            WHERE run_id = ?
            """,
            (str(run.id),),
        )
    assert await queue.claim(worker_id="second-worker", lease_seconds=60) is not None

    await queue.complete(run.id, worker_id="first-worker")
    rows = await queue.list()
    assert rows[0].status == QueueStatus.RUNNING
    assert rows[0].locked_by == "second-worker"

    await queue.complete(run.id, worker_id="second-worker")
    rows = await queue.list()
    assert rows[0].status == QueueStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_in_process_runner_logs_background_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def fail_job() -> object:
        raise RuntimeError("background boom")

    runner = InProcessJobRunner(logger=logging.getLogger("test.librarian.jobs"))
    run_id = RunId("run_background_failure")

    with caplog.at_level(logging.ERROR, logger="test.librarian.jobs"):
        await runner.submit(run_id, fail_job)
        with pytest.raises(RuntimeError, match="background boom"):
            await runner.wait(run_id)

    assert "in_process_job_failed" in caplog.text


@pytest.mark.asyncio
async def test_queue_worker_processes_one_run(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript with notes about groundwork.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)
    queue = SQLiteRunQueue(container.database)
    await queue.enqueue(run.id)
    worker = QueueWorker(
        queue=queue,
        processor=container.process_document.execute_existing,
        worker_id="test-worker",
    )

    assert await worker.run_once()
    finished = await container.repository.get_run(run.id)
    output = await container.repository.get_cleaned_output(ingested.document.id)
    assert finished is not None
    assert finished.status == RunStatus.SUCCEEDED
    assert output is not None


@pytest.mark.asyncio
async def test_queue_worker_emits_processing_trace_span(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript with notes about tracing.", encoding="utf-8")
    tracer = FakeTracer()
    container = await build_container(settings, tracer=tracer)
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)
    queue = SQLiteRunQueue(container.database)
    await queue.enqueue(run.id)
    worker = QueueWorker(
        queue=queue,
        processor=container.process_document.execute_existing,
        worker_id="trace-worker",
        tracer=tracer,
    )

    assert await worker.run_once()

    queue_span = next(span for span in tracer.spans if span.name == "librarian.queue_process")
    assert queue_span.attributes["librarian.run_id"] == str(run.id)
    assert queue_span.attributes["librarian.worker_id"] == "trace-worker"
    assert queue_span.attributes["librarian.queue_attempts"] == 1
    assert queue_span.attributes["librarian.status"] == "succeeded"
    assert any(span.name == "librarian.run_stage" for span in tracer.spans)


@pytest.mark.asyncio
async def test_queue_worker_records_failure_without_stopping(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)
    queue = SQLiteRunQueue(container.database)
    await queue.enqueue(run.id)

    async def fail_run(_: RunId) -> object:
        raise RuntimeError("boom")

    worker = QueueWorker(
        queue=queue,
        processor=fail_run,
        worker_id="test-worker",
    )

    assert await worker.run_once()
    rows = await queue.list()

    assert rows[0].status == QueueStatus.RETRY
    assert rows[0].last_error == "boom"


@pytest.mark.asyncio
async def test_canceled_queued_run_is_not_claimed(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)
    queue = SQLiteRunQueue(container.database)
    await queue.enqueue(run.id)
    await container.repository.update_status(
        run.id,
        status=RunStatus.CANCELED,
        stage=RunStage.COMPLETE,
        error="canceled by user",
    )
    await queue.cancel(run.id, error="canceled by user")

    assert await queue.claim(worker_id="test-worker", lease_seconds=60) is None
    rows = await queue.list()
    assert rows[0].status == QueueStatus.CANCELED


@pytest.mark.asyncio
async def test_canceled_queue_row_is_not_completed_or_retried(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)
    queue = SQLiteRunQueue(container.database)
    await queue.enqueue(run.id)
    assert await queue.claim(worker_id="test-worker", lease_seconds=60) is not None
    await queue.cancel(run.id, error="canceled by user")

    await queue.complete(run.id)
    await queue.fail(run.id, error="late failure", max_attempts=3)

    rows = await queue.list()
    assert rows[0].status == QueueStatus.CANCELED
    assert rows[0].last_error == "canceled by user"


@pytest.mark.asyncio
async def test_sqlite_rejects_unbounded_limits(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    queue = SQLiteRunQueue(container.database)

    with pytest.raises(ValueError, match="limit"):
        await container.repository.search("horse", limit=-1)
    with pytest.raises(ValueError, match="limit"):
        await container.repository.list_runs(limit=0)
    with pytest.raises(ValueError, match="offset"):
        await container.repository.list_runs(offset=-1)
    assert await container.repository.count_runs() == 0
    with pytest.raises(ValueError, match="limit"):
        await container.repository.list(limit=0)
    with pytest.raises(ValueError, match="offset"):
        await container.repository.list(offset=-1)
    with pytest.raises(ValueError, match="limit"):
        await queue.list(limit=10_000)
    with pytest.raises(ValueError, match="offset"):
        await queue.list(offset=-1)
