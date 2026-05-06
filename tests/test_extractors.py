import json
import shutil
from pathlib import Path

import pytest

from librarian.ingest.extractors import CompositeExtractor, TextFamilyExtractor


@pytest.mark.asyncio
async def test_text_family_extractor_reads_markdown(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_text("# Notes\n\nTranscript text", encoding="utf-8")

    text = await TextFamilyExtractor().extract(path)

    assert "Transcript text" in text


@pytest.mark.asyncio
async def test_text_family_extractor_pretty_prints_json(tmp_path: Path) -> None:
    path = tmp_path / "notes.json"
    path.write_text(json.dumps({"speaker": "A", "text": "hello"}), encoding="utf-8")

    text = await TextFamilyExtractor().extract(path)

    assert '"speaker": "A"' in text
    assert "\n" in text


@pytest.mark.asyncio
async def test_text_family_extractor_returns_invalid_json_as_text(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    text = await TextFamilyExtractor().extract(path)

    assert text == "{not json"


@pytest.mark.asyncio
async def test_composite_extractor_rejects_unknown_extension(tmp_path: Path) -> None:
    path = tmp_path / "notes.bin"
    path.write_bytes(b"binary")

    with pytest.raises(ValueError, match="Unsupported file extension"):
        await CompositeExtractor().extract(path)


@pytest.mark.asyncio
async def test_docx_extractor_reads_fixture(tmp_path: Path) -> None:
    from docx import Document

    path = tmp_path / "fixture.docx"
    document = Document()
    document.add_paragraph("DOCX fixture text")
    document.save(str(path))

    text = await CompositeExtractor().extract(path)

    assert "DOCX fixture text" in text


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
@pytest.mark.asyncio
async def test_image_ocr_extractor_reads_fixture(tmp_path: Path) -> None:
    from PIL import Image, ImageDraw

    path = tmp_path / "fixture.png"
    image = Image.new("RGB", (400, 120), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 40), "OCR fixture text", fill="black")
    image.save(path)

    text = await CompositeExtractor().extract(path)

    assert "OCR" in text
