from pathlib import Path

import pytest

from librarian.application.factory import build_container
from librarian.application.jobs import QueueStatus, QueueWorker
from librarian.config import Settings
from librarian.domain.ids import RunId
from librarian.domain.models import RunStage, RunStatus
from librarian.storage.sqlite import SQLiteDatabase, SQLiteRunQueue


@pytest.mark.asyncio
async def test_sqlite_initializes_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "librarian.sqlite"
    database = SQLiteDatabase(database_path)

    await database.initialize()

    assert database_path.exists()
    with database.connect() as connection:
        rows = connection.execute("SELECT version FROM schema_migrations").fetchall()

    assert [row["version"] for row in rows] == [
        "0001_initial.sql",
        "0002_run_queue.sql",
        "0003_document_scoped_chunks.sql",
    ]


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

    await queue.complete(run.id)
    assert await queue.claim(worker_id="test-worker", lease_seconds=60) is None


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
    with pytest.raises(ValueError, match="limit"):
        await queue.list(limit=10_000)
