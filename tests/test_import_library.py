from pathlib import Path

import pytest

from librarian.application.convert_document import (
    ConversionFormat,
    DirectoryOutputMode,
    DocumentConverter,
)
from librarian.application.factory import build_container
from librarian.application.import_library import ImportLibrary, ImportProcessingMode
from librarian.config import Settings
from librarian.domain.models import RunStatus
from librarian.ingest.extractors import CompositeExtractor
from librarian.storage.sqlite import SQLiteRunQueue


@pytest.mark.asyncio
async def test_import_directory_converts_and_ingests(tmp_path: Path) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("Alpha horse notes", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    importer = ImportLibrary(
        converter=DocumentConverter(CompositeExtractor()),
        ingest=container.ingest_document,
        process=container.process_document,
    )

    result = await importer.import_directory(
        source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        processing_mode=ImportProcessingMode.NONE,
    )

    assert result.converted == 1
    assert result.ingested == 1
    assert result.failed == 0
    assert result.items[0].document_id is not None


@pytest.mark.asyncio
async def test_import_directory_processes_documents(tmp_path: Path) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("Horse training transcript", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    container = await build_container(settings)
    importer = ImportLibrary(
        converter=DocumentConverter(CompositeExtractor()),
        ingest=container.ingest_document,
        process=container.process_document,
    )

    result = await importer.import_directory(
        source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        processing_mode=ImportProcessingMode.PROCESS,
    )

    assert result.processed == 1
    assert result.items[0].run_id is not None
    run = await container.repository.get_run(result.items[0].run_id)
    assert run is not None
    assert run.status == RunStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_import_directory_queues_documents(tmp_path: Path) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("Horse training transcript", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    importer = ImportLibrary(
        converter=DocumentConverter(CompositeExtractor()),
        ingest=container.ingest_document,
        process=container.process_document,
        queue_factory=lambda: SQLiteRunQueue(container.database),
    )

    result = await importer.import_directory(
        source_dir,
        format=ConversionFormat.TEXT,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        processing_mode=ImportProcessingMode.QUEUE,
    )

    assert result.queued == 1
    assert result.items[0].run_id is not None
    claimed = await SQLiteRunQueue(container.database).claim(worker_id="test", lease_seconds=60)
    assert claimed is not None
    assert claimed.run_id == result.items[0].run_id
