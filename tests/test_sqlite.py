from pathlib import Path

import pytest

from librarian.application.factory import build_container
from librarian.application.jobs import QueueStatus, QueueWorker
from librarian.config import Settings
from librarian.domain.ids import RunId
from librarian.domain.models import RunStatus
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
