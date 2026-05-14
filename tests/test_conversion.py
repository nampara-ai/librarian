import json
from pathlib import Path
from typing import Any

import pytest

from librarian.application.convert_document import (
    ConversionFormat,
    DirectoryOutputMode,
    DocumentConverter,
    classify_conversion_error,
    conversion_output_path,
    discover_supported_files,
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
async def test_convert_srt_file_to_markdown(tmp_path: Path) -> None:
    source = tmp_path / "captions.srt"
    output = tmp_path / "captions.md"
    source.write_text(
        "1\n"
        "00:00:03,000 --> 00:00:04,000\n"
        "Host: Welcome\n\n"
        "2\n"
        "00:00:04,000 --> 00:00:06,000\n"
        "back.\n",
        encoding="utf-8",
    )

    await DocumentConverter(CompositeExtractor()).convert_file(
        source,
        output,
        format=ConversionFormat.MARKDOWN,
    )

    assert "- [00:03] Host: Welcome back." in output.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_convert_file_rejects_symlink_output(tmp_path: Path) -> None:
    source = tmp_path / "meeting.txt"
    source.write_text("Speaker: Hello world.", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("keep", encoding="utf-8")
    output = tmp_path / "meeting.md"
    output.symlink_to(outside)

    with pytest.raises(ValueError, match="Output path must not be a symlink"):
        await DocumentConverter(CompositeExtractor()).convert_file(
            source,
            output,
            format=ConversionFormat.MARKDOWN,
            overwrite=True,
        )

    assert outside.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_convert_file_rejects_symlink_output_parent(tmp_path: Path) -> None:
    source = tmp_path / "meeting.txt"
    source.write_text("Speaker: Hello world.", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="Output path crosses symlinked parent"):
        await DocumentConverter(CompositeExtractor()).convert_file(
            source,
            linked_parent / "meeting.md",
            format=ConversionFormat.MARKDOWN,
            overwrite=True,
        )

    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_convert_file_rejects_symlink_sidecar(tmp_path: Path) -> None:
    source = tmp_path / "meeting.txt"
    source.write_text("Speaker: Hello world.", encoding="utf-8")
    output = tmp_path / "meeting.md"
    outside = tmp_path / "outside.json"
    outside.write_text("keep", encoding="utf-8")
    output.with_suffix(".md.json").symlink_to(outside)

    with pytest.raises(ValueError, match="Output path must not be a symlink"):
        await DocumentConverter(CompositeExtractor()).convert_file(
            source,
            output,
            format=ConversionFormat.MARKDOWN,
            write_sidecar=True,
        )

    assert outside.read_text(encoding="utf-8") == "keep"
    assert not output.exists()


@pytest.mark.asyncio
async def test_convert_file_rejects_symlink_sidecar_parent(tmp_path: Path) -> None:
    source = tmp_path / "meeting.txt"
    source.write_text("Speaker: Hello world.", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="Output path crosses symlinked parent"):
        await DocumentConverter(CompositeExtractor()).convert_file(
            source,
            linked_parent / "meeting.md",
            format=ConversionFormat.MARKDOWN,
            write_sidecar=True,
        )

    assert list(outside.iterdir()) == []


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


@pytest.mark.asyncio
async def test_convert_directory_redacts_item_errors(tmp_path: Path) -> None:
    class FailingExtractor:
        supported_extensions = frozenset({".txt"})

        async def extract(self, path: Path) -> str:
            del path
            raise RuntimeError("extract failed api_key=abc123 sk-testSECRET123")

    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("Alpha", encoding="utf-8")

    result = await DocumentConverter(FailingExtractor()).convert_directory(
        source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
    )

    assert result.failed == 1
    assert result.items[0].error == "extract failed api_key=[REDACTED] [REDACTED]"
    assert "abc123" not in (result.items[0].error or "")
    assert "sk-testSECRET123" not in (result.items[0].error or "")


@pytest.mark.asyncio
async def test_recursive_convert_directory_skips_own_output_subdirectory(tmp_path: Path) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("Alpha", encoding="utf-8")
    converter = DocumentConverter(CompositeExtractor())

    first = await converter.convert_directory(
        source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        subdirectory_name="converted",
        recursive=True,
    )
    second = await converter.convert_directory(
        source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        subdirectory_name="converted",
        recursive=True,
    )

    assert first.converted == 1
    assert second.converted == 1
    assert [item.source_path.relative_to(source_dir) for item in second.items] == [Path("a.txt")]
    assert not (source_dir / "converted" / "converted").exists()


@pytest.mark.asyncio
async def test_recursive_convert_directory_skips_previous_generated_original_outputs(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.md").write_text("Alpha", encoding="utf-8")
    converter = DocumentConverter(CompositeExtractor())

    original = await converter.convert_directory(
        source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.ORIGINAL,
        recursive=True,
    )
    recursive = await converter.convert_directory(
        source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        subdirectory_name="converted",
        recursive=True,
    )

    assert original.converted == 1
    assert (source_dir / "a-2.md").exists()
    assert (source_dir / "a-2.md.json").exists()
    assert [item.source_path.relative_to(source_dir) for item in recursive.items] == [Path("a.md")]


@pytest.mark.asyncio
async def test_new_directory_rejects_source_or_ancestor_output_dir(tmp_path: Path) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "a.txt").write_text("Alpha", encoding="utf-8")
    converter = DocumentConverter(CompositeExtractor())

    with pytest.raises(ValueError, match="ancestor"):
        await converter.convert_directory(
            source_dir,
            format=ConversionFormat.MARKDOWN,
            output_mode=DirectoryOutputMode.NEW_DIRECTORY,
            output_dir=tmp_path,
            recursive=True,
        )
    with pytest.raises(ValueError, match="ancestor"):
        await converter.convert_directory(
            source_dir,
            format=ConversionFormat.MARKDOWN,
            output_mode=DirectoryOutputMode.NEW_DIRECTORY,
            output_dir=source_dir,
            recursive=True,
        )


@pytest.mark.asyncio
async def test_convert_directory_avoids_output_collisions_and_writes_sidecars(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "same.txt").write_text("Alpha", encoding="utf-8")
    (source_dir / "same.md").write_text("Bravo", encoding="utf-8")

    result = await DocumentConverter(CompositeExtractor()).convert_directory(
        source_dir,
        format=ConversionFormat.MARKDOWN,
        output_mode=DirectoryOutputMode.SUBDIRECTORY,
        subdirectory_name="converted",
        write_sidecar=True,
    )

    outputs = sorted(item.output_path for item in result.items if item.output_path is not None)
    assert result.converted == 2
    assert outputs[0] != outputs[1]
    assert outputs[0].with_suffix(".md.json").exists()


@pytest.mark.asyncio
async def test_convert_file_sidecar_includes_extraction_metadata(tmp_path: Path) -> None:
    class MetadataExtractor:
        supported_extensions = frozenset({".txt"})
        last_metadata: dict[str, object] | None = None

        async def extract(self, path: Path) -> str:
            del path
            self.last_metadata = {
                "artifact_type": "pdf-page-extraction",
                "page_count": 2,
                "pages": [{"page_number": 1, "source": "embedded"}],
            }
            return "Alpha"

    source = tmp_path / "source.txt"
    output = tmp_path / "source.md"
    source.write_text("Alpha", encoding="utf-8")

    await DocumentConverter(MetadataExtractor()).convert_file(
        source,
        output,
        format=ConversionFormat.MARKDOWN,
        write_sidecar=True,
    )

    payload = json.loads(output.with_suffix(".md.json").read_text(encoding="utf-8"))
    assert payload["extraction"]["page_count"] == 2


@pytest.mark.asyncio
async def test_convert_file_sets_page_manifest_path_when_writing_sidecar(tmp_path: Path) -> None:
    class ManifestAwareExtractor:
        supported_extensions = frozenset({".pdf"})

        def __init__(self) -> None:
            self.manifest_path: Path | None = None

        def set_page_manifest_path(self, path: Path | None) -> None:
            self.manifest_path = path

        async def extract(self, path: Path) -> str:
            del path
            assert self.manifest_path is not None
            self.manifest_path.write_text(
                json.dumps(
                    {
                        "generated_by": "librarian",
                        "artifact_type": "pdf-page-extraction-manifest",
                    }
                ),
                encoding="utf-8",
            )
            return "PDF text"

    source = tmp_path / "source.pdf"
    output = tmp_path / "source.md"
    source.write_bytes(b"%PDF")
    extractor = ManifestAwareExtractor()

    await DocumentConverter(extractor).convert_file(
        source,
        output,
        format=ConversionFormat.MARKDOWN,
        write_sidecar=True,
    )

    assert extractor.manifest_path == output.with_suffix(".md.pages.json")
    assert output.with_suffix(".md.pages.json").exists()


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


def test_discovery_does_not_parse_large_metadata_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    source = source_dir / "a.txt"
    source.write_text("Alpha", encoding="utf-8")
    sidecar = source.with_suffix(".txt.json")
    sidecar.write_text(
        '{"generated_by": "librarian", "artifact_type": "conversion-sidecar", "padding": "'
        + ("x" * (70 * 1024))
        + '"}',
        encoding="utf-8",
    )
    original_read_text = Path.read_text

    def fail_large_sidecar_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == sidecar:
            raise AssertionError("large metadata sidecar should be prefix-read, not read_text")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_large_sidecar_read_text)

    files = discover_supported_files(
        source_dir,
        supported_extensions=frozenset({".txt"}),
        recursive=True,
    )

    assert files == [source]


def test_discovery_reads_small_metadata_sidecars_with_bounded_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    source = source_dir / "a.txt"
    source.write_text("Alpha", encoding="utf-8")
    sidecar = source.with_suffix(".txt.json")
    sidecar.write_text(
        json.dumps(
            {
                "generated_by": "librarian",
                "artifact_type": "conversion-sidecar",
            }
        ),
        encoding="utf-8",
    )

    original_read_text = Path.read_text

    def fail_sidecar_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == sidecar:
            raise AssertionError("metadata sidecar should be bounded-read, not read_text")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_sidecar_read_text)

    files = discover_supported_files(
        source_dir,
        supported_extensions=frozenset({".txt"}),
        recursive=True,
    )

    assert files == []


def test_discovery_skips_large_pdf_page_manifests(tmp_path: Path) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    source = source_dir / "a.txt"
    source.write_text("Alpha", encoding="utf-8")
    manifest = source_dir / "a.md.pages.json"
    manifest.write_text(
        json.dumps(
            {
                "generated_by": "librarian",
                "artifact_type": "pdf-page-extraction-manifest",
                "padding": "x" * (70 * 1024),
            }
        ),
        encoding="utf-8",
    )

    files = discover_supported_files(
        source_dir,
        supported_extensions=frozenset({".txt", ".json"}),
        recursive=True,
    )

    assert files == [source]


def test_markdown_to_text_removes_common_markup() -> None:
    assert markdown_to_text("# Title\n\n- **Important** [link](https://example.com)") == (
        "Title\nImportant link"
    )


def test_classify_conversion_error() -> None:
    assert classify_conversion_error(FileExistsError("exists")).value == "output_exists"
