import json
from pathlib import Path
from typing import cast

import pytest

from librarian.application.convert_document import (
    ConversionFormat,
    DirectoryOutputMode,
    DocumentConverter,
)
from librarian.application.factory import build_container
from librarian.application.import_library import (
    ImportLibrary,
    ImportProcessingMode,
    ImportResult,
    write_import_report,
)
from librarian.application.jobs import RunQueue
from librarian.config import Settings
from librarian.domain.models import RunStatus
from librarian.ingest.extractors import CompositeExtractor
from librarian.storage.sqlite import SQLiteRunQueue


@pytest.mark.asyncio
async def test_import_file_converts_ingests_and_processes(tmp_path: Path) -> None:
    source = tmp_path / "large.md"
    source.write_text("# Transcript\n\nHorse training transcript", encoding="utf-8")
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

    result = await importer.import_path(
        source,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        processing_mode=ImportProcessingMode.PROCESS,
    )

    assert result.converted == 1
    assert result.ingested == 1
    assert result.processed == 1
    assert result.items[0].source_path == source
    assert result.items[0].converted_path == tmp_path / "librarian-converted" / "large.md"
    assert result.items[0].run_id is not None


@pytest.mark.asyncio
async def test_import_file_writes_manifest_and_resumes(tmp_path: Path) -> None:
    source = tmp_path / "large.md"
    source.write_text("# Transcript\n\nHorse training transcript", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
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

    first = await importer.import_file(
        source,
        format=ConversionFormat.TEXT,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        processing_mode=ImportProcessingMode.NONE,
        manifest_path=manifest,
    )
    second = await importer.import_file(
        source,
        format=ConversionFormat.TEXT,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        processing_mode=ImportProcessingMode.NONE,
        manifest_path=manifest,
        resume=True,
    )

    assert first.ingested == 1
    assert second.skipped == 1
    assert second.items[0].converted_path == tmp_path / "librarian-converted" / "large.txt"
    converted_files = sorted(
        path.name for path in (tmp_path / "librarian-converted").glob("*.txt")
    )
    assert converted_files == ["large.txt"]


@pytest.mark.asyncio
async def test_import_manifest_rejects_non_librarian_json(tmp_path: Path) -> None:
    source = tmp_path / "large.md"
    source.write_text("# Transcript\n\nHorse training transcript", encoding="utf-8")
    manifest = tmp_path / "notes.json"
    manifest.write_text('{"unrelated": true}', encoding="utf-8")
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

    with pytest.raises(ValueError, match="non-Librarian JSON"):
        await importer.import_file(
            source,
            format=ConversionFormat.TEXT,
            output_mode=DirectoryOutputMode.SUBDIRECTORY,
            processing_mode=ImportProcessingMode.NONE,
            manifest_path=manifest,
        )


@pytest.mark.asyncio
async def test_import_manifest_rejects_oversized_existing_manifest(tmp_path: Path) -> None:
    source = tmp_path / "large.md"
    source.write_text("# Transcript\n\nHorse training transcript", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "generated_by": "librarian",
                "artifact_type": "import-report",
                "summary": {},
                "items": [],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    importer = ImportLibrary(
        converter=DocumentConverter(CompositeExtractor()),
        ingest=container.ingest_document,
        process=container.process_document,
        manifest_max_bytes=4,
    )

    with pytest.raises(ValueError, match="manifest_path contains more than 4 bytes"):
        await importer.import_file(
            source,
            format=ConversionFormat.TEXT,
            output_mode=DirectoryOutputMode.SUBDIRECTORY,
            processing_mode=ImportProcessingMode.NONE,
            manifest_path=manifest,
        )


@pytest.mark.asyncio
async def test_import_manifest_rejects_symlink_path(tmp_path: Path) -> None:
    source = tmp_path / "large.md"
    source.write_text("# Transcript\n\nHorse training transcript", encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.symlink_to(outside)
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

    with pytest.raises(ValueError, match="manifest_path must not be a symlink"):
        await importer.import_file(
            source,
            format=ConversionFormat.TEXT,
            output_mode=DirectoryOutputMode.SUBDIRECTORY,
            processing_mode=ImportProcessingMode.NONE,
            manifest_path=manifest,
        )

    assert outside.read_text(encoding="utf-8") == "{}"


@pytest.mark.asyncio
async def test_import_manifest_rejects_symlink_parent(tmp_path: Path) -> None:
    source = tmp_path / "large.md"
    source.write_text("# Transcript\n\nHorse training transcript", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
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

    with pytest.raises(ValueError, match="manifest_path must not cross a symlinked parent"):
        await importer.import_file(
            source,
            format=ConversionFormat.TEXT,
            output_mode=DirectoryOutputMode.SUBDIRECTORY,
            processing_mode=ImportProcessingMode.NONE,
            manifest_path=linked_parent / "manifest.json",
        )

    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_import_manifest_requires_json_path(tmp_path: Path) -> None:
    source = tmp_path / "large.md"
    source.write_text("# Transcript\n\nHorse training transcript", encoding="utf-8")
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

    with pytest.raises(ValueError, match=".json"):
        await importer.import_file(
            source,
            format=ConversionFormat.TEXT,
            output_mode=DirectoryOutputMode.SUBDIRECTORY,
            processing_mode=ImportProcessingMode.NONE,
            manifest_path=tmp_path / "manifest.txt",
        )


@pytest.mark.asyncio
async def test_import_file_rejects_archives_with_explicit_policy(tmp_path: Path) -> None:
    source = tmp_path / "archive.zip"
    source.write_bytes(b"PK")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    importer = ImportLibrary(
        converter=DocumentConverter(CompositeExtractor()),
        ingest=container.ingest_document,
        process=None,
    )

    with pytest.raises(ValueError, match="Archive inputs are not supported"):
        await importer.import_file(
            source,
            format=ConversionFormat.MARKDOWN,
            output_mode=DirectoryOutputMode.SUBDIRECTORY,
            processing_mode=ImportProcessingMode.NONE,
        )


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


@pytest.mark.asyncio
async def test_import_directory_marks_run_failed_when_enqueue_fails(tmp_path: Path) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("Horse training transcript", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)

    class FailingQueue:
        async def enqueue(self, run_id: object) -> None:
            del run_id
            raise RuntimeError("queue down")

    importer = ImportLibrary(
        converter=DocumentConverter(CompositeExtractor()),
        ingest=container.ingest_document,
        process=container.process_document,
        queue_factory=lambda: cast(RunQueue, FailingQueue()),
    )

    result = await importer.import_directory(
        source_dir,
        format=ConversionFormat.TEXT,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        processing_mode=ImportProcessingMode.QUEUE,
    )

    assert result.failed == 1
    assert result.items[0].document_id is not None
    assert result.items[0].run_id is not None
    assert "queue enqueue failed" in (result.items[0].error or "")
    run = await container.repository.get_run(result.items[0].run_id)
    assert run is not None
    assert run.status == RunStatus.FAILED


@pytest.mark.asyncio
async def test_import_directory_writes_manifest_and_resumes(tmp_path: Path) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("Horse training transcript", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
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

    first = await importer.import_directory(
        source_dir,
        format=ConversionFormat.TEXT,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        processing_mode=ImportProcessingMode.NONE,
        manifest_path=manifest,
    )
    second = await importer.import_directory(
        source_dir,
        format=ConversionFormat.TEXT,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        processing_mode=ImportProcessingMode.NONE,
        manifest_path=manifest,
        resume=True,
    )
    report = tmp_path / "report.json"
    await write_import_report(report, second)

    assert first.ingested == 1
    assert second.skipped == 1
    assert '"skipped": 1' in report.read_text(encoding="utf-8")
    assert '"generated_by": "librarian"' in report.read_text(encoding="utf-8")
    converted_files = sorted(
        path.name for path in (source_dir / "librarian-converted").glob("*.txt")
    )
    assert converted_files == ["a.txt"]


@pytest.mark.asyncio
async def test_write_import_report_rejects_symlink_output(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    report = tmp_path / "report.json"
    report.symlink_to(outside)
    result = ImportResult(items=())

    with pytest.raises(ValueError, match="must not be a symlink"):
        await write_import_report(report, result)

    assert outside.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_write_import_report_rejects_symlink_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
    result = ImportResult(items=())

    with pytest.raises(ValueError, match="Output path crosses symlinked parent"):
        await write_import_report(linked_parent / "report.json", result)

    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_import_directory_does_not_rediscover_manifest_in_source_tree(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("Alpha horse notes", encoding="utf-8")
    manifest = source_dir / "manifest.json"
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

    await importer.import_directory(
        source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        processing_mode=ImportProcessingMode.NONE,
        recursive=True,
        manifest_path=manifest,
    )
    result = await importer.import_directory(
        source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        processing_mode=ImportProcessingMode.NONE,
        recursive=True,
        manifest_path=manifest,
        resume=True,
    )

    assert result.skipped == 1
    assert [item.source_path.name for item in result.items] == ["a.txt"]
    assert '"generated_by": "librarian"' in manifest.read_text(encoding="utf-8")
