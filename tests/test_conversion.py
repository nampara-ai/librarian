from pathlib import Path

import pytest

from librarian.application.convert_document import (
    ConversionFormat,
    DirectoryOutputMode,
    DocumentConverter,
    conversion_output_path,
    markdown_to_text,
)
from librarian.ingest.extractors import CompositeExtractor


@pytest.mark.asyncio
async def test_convert_file_to_markdown(tmp_path: Path) -> None:
    source = tmp_path / "meeting.txt"
    output = tmp_path / "meeting.md"
    source.write_text("Speaker: Hello world.", encoding="utf-8")

    result = await DocumentConverter(CompositeExtractor()).convert_file(
        source,
        output,
        format=ConversionFormat.MARKDOWN,
    )

    assert result.output_path == output
    assert output.read_text(encoding="utf-8").startswith("# meeting")
    assert "Speaker: Hello world." in output.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_convert_directory_to_subdirectory(tmp_path: Path) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("Alpha", encoding="utf-8")
    (source_dir / "b.md").write_text("# Bravo", encoding="utf-8")
    (source_dir / "ignore.bin").write_bytes(b"nope")

    result = await DocumentConverter(CompositeExtractor()).convert_directory(
        source_dir,
        format=ConversionFormat.TEXT,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        subdirectory_name="converted",
    )

    assert result.converted == 2
    assert result.failed == 0
    assert (source_dir / "converted" / "a.txt").read_text(encoding="utf-8") == "Alpha\n"
    assert (source_dir / "converted" / "b.txt").read_text(encoding="utf-8") == "Bravo\n"


def test_conversion_output_path_new_directory(tmp_path: Path) -> None:
    source_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    source = source_dir / "nested" / "a.docx"

    output = conversion_output_path(
        source,
        source_dir=source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.NEW_DIRECTORY,
        output_dir=output_dir,
        subdirectory_name="converted",
    )

    assert output == output_dir / "nested" / "a.md"


def test_markdown_to_text_removes_common_markup() -> None:
    assert markdown_to_text("# Title\n\n- **Important** [link](https://example.com)") == (
        "Title\nImportant link"
    )
