"""Tests for bounded-concurrency directory imports."""

from __future__ import annotations

import asyncio
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


class _SlowCountingExtractor:
    """Fake extractor that records peak concurrency while it runs."""

    supported_extensions = frozenset({".txt"})

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.last_metadata: dict[str, object] | None = None

    async def extract(self, path: Path) -> str:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            return await asyncio.to_thread(path.read_text, encoding="utf-8")
        finally:
            self.active -= 1


async def _make_importer(
    tmp_path: Path, *, extractor: _SlowCountingExtractor, concurrency: int
) -> ImportLibrary:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    return ImportLibrary(
        converter=DocumentConverter(extractor),
        ingest=container.ingest_document,
        process=container.process_document,
        import_concurrency=concurrency,
    )


def _make_corpus(tmp_path: Path, names: list[str]) -> Path:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    for name in names:
        (source_dir / name).write_text(f"contents of {name}", encoding="utf-8")
    return source_dir


@pytest.mark.asyncio
async def test_import_directory_overlaps_extraction_when_concurrent(tmp_path: Path) -> None:
    source_dir = _make_corpus(tmp_path, [f"{letter}.txt" for letter in "abcdef"])
    extractor = _SlowCountingExtractor()
    importer = await _make_importer(tmp_path, extractor=extractor, concurrency=4)

    result = await importer.import_directory(
        source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        processing_mode=ImportProcessingMode.NONE,
    )

    assert result.ingested == 6
    assert result.failed == 0
    # Several files extracted at the same time rather than strictly serially.
    assert extractor.max_active >= 2


@pytest.mark.asyncio
async def test_import_directory_serial_when_concurrency_one(tmp_path: Path) -> None:
    source_dir = _make_corpus(tmp_path, [f"{letter}.txt" for letter in "abcd"])
    extractor = _SlowCountingExtractor()
    importer = await _make_importer(tmp_path, extractor=extractor, concurrency=1)

    result = await importer.import_directory(
        source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        processing_mode=ImportProcessingMode.NONE,
    )

    assert result.ingested == 4
    assert extractor.max_active == 1  # never more than one extraction at a time


@pytest.mark.asyncio
async def test_import_directory_preserves_order_under_concurrency(tmp_path: Path) -> None:
    names = [f"{letter}.txt" for letter in "abcdef"]
    source_dir = _make_corpus(tmp_path, names)
    extractor = _SlowCountingExtractor()
    importer = await _make_importer(tmp_path, extractor=extractor, concurrency=4)

    result = await importer.import_directory(
        source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        processing_mode=ImportProcessingMode.NONE,
    )

    assert [item.source_path.name for item in result.items] == names


@pytest.mark.asyncio
async def test_import_directory_isolates_failures_under_concurrency(tmp_path: Path) -> None:
    source_dir = _make_corpus(tmp_path, [f"{letter}.txt" for letter in "abcd"])

    class _SometimesFailing(_SlowCountingExtractor):
        async def extract(self, path: Path) -> str:
            if path.name == "b.txt":
                raise RuntimeError("bad file")
            return await super().extract(path)

    extractor = _SometimesFailing()
    importer = await _make_importer(tmp_path, extractor=extractor, concurrency=4)

    result = await importer.import_directory(
        source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        processing_mode=ImportProcessingMode.NONE,
    )

    assert result.failed == 1
    assert result.ingested == 3
    failed = [item for item in result.items if item.status == "failed"]
    assert len(failed) == 1
    assert failed[0].source_path.name == "b.txt"


@pytest.mark.asyncio
async def test_import_directory_unique_paths_under_concurrency(tmp_path: Path) -> None:
    # Two source files in different subdirectories collapse to the same output
    # name under NEW_DIRECTORY output, so destinations must stay distinct even
    # when allocated up front before any file is written.
    source_dir = tmp_path / "input"
    (source_dir / "one").mkdir(parents=True)
    (source_dir / "two").mkdir(parents=True)
    (source_dir / "one" / "report.txt").write_text("first", encoding="utf-8")
    (source_dir / "two" / "report.txt").write_text("second", encoding="utf-8")
    output_dir = tmp_path / "out"
    extractor = _SlowCountingExtractor()
    importer = await _make_importer(tmp_path, extractor=extractor, concurrency=4)

    result = await importer.import_directory(
        source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.NEW_DIRECTORY,
        output_dir=output_dir,
        processing_mode=ImportProcessingMode.NONE,
        recursive=True,
    )

    assert result.ingested == 2
    destinations = {item.converted_path for item in result.items}
    assert len(destinations) == 2  # no two files clobbered the same output


@pytest.mark.asyncio
async def test_import_directory_resume_with_concurrency(tmp_path: Path) -> None:
    source_dir = _make_corpus(tmp_path, [f"{letter}.txt" for letter in "abcd"])
    manifest = tmp_path / "manifest.json"
    extractor = _SlowCountingExtractor()
    importer = await _make_importer(tmp_path, extractor=extractor, concurrency=4)

    first = await importer.import_directory(
        source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        processing_mode=ImportProcessingMode.NONE,
        manifest_path=manifest,
    )
    assert first.ingested == 4

    second = await importer.import_directory(
        source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        processing_mode=ImportProcessingMode.NONE,
        manifest_path=manifest,
        resume=True,
    )
    assert second.skipped == 4
    assert [item.source_path.name for item in second.items] == [
        "a.txt",
        "b.txt",
        "c.txt",
        "d.txt",
    ]
