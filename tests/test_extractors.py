import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from librarian.ingest import extractors
from librarian.ingest.extractors import (
    CompositeExtractor,
    ImageOcrExtractor,
    MarkItDownExtractor,
    PdfExtractor,
    TextFamilyExtractor,
)


@pytest.mark.asyncio
async def test_text_family_extractor_reads_markdown(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_text("# Notes\n\nTranscript text", encoding="utf-8")

    text = await TextFamilyExtractor().extract(path)

    assert "Transcript text" in text


@pytest.mark.asyncio
async def test_text_family_extractor_rejects_large_inputs(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("too large", encoding="utf-8")

    with pytest.raises(ValueError, match="Text extraction input exceeds"):
        await TextFamilyExtractor(max_input_bytes=4).extract(path)


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
async def test_text_family_extractor_rejects_binary_content(tmp_path: Path) -> None:
    path = tmp_path / "binary.txt"
    path.write_bytes(b"text\x00binary")

    with pytest.raises(ValueError, match="appears to be binary"):
        await TextFamilyExtractor().extract(path)


@pytest.mark.asyncio
async def test_composite_extractor_rejects_unknown_extension(tmp_path: Path) -> None:
    path = tmp_path / "notes.bin"
    path.write_bytes(b"binary")

    with pytest.raises(ValueError, match="Unsupported file extension"):
        await CompositeExtractor().extract(path)


@pytest.mark.asyncio
async def test_composite_extractor_rejects_zip_archives(tmp_path: Path) -> None:
    path = tmp_path / "archive.zip"
    path.write_bytes(b"PK")

    with pytest.raises(ValueError, match="Unsupported file extension"):
        await CompositeExtractor().extract(path)


@pytest.mark.asyncio
async def test_markitdown_extractor_rejects_large_inputs_before_import(tmp_path: Path) -> None:
    path = tmp_path / "fixture.html"
    path.write_text("<p>too large</p>", encoding="utf-8")

    with pytest.raises(ValueError, match="exceeds"):
        await MarkItDownExtractor(max_input_bytes=4).extract(path)


@pytest.mark.asyncio
async def test_markitdown_extractor_times_out(tmp_path: Path) -> None:
    path = tmp_path / "fixture.html"
    path.write_text("<p>small but bounded</p>", encoding="utf-8")

    with pytest.raises(TimeoutError, match="timed out"):
        await MarkItDownExtractor(timeout_seconds=0).extract(path)


@pytest.mark.asyncio
async def test_docx_extractor_reads_fixture(tmp_path: Path) -> None:
    from docx import Document

    path = tmp_path / "fixture.docx"
    document = Document()
    document.add_paragraph("DOCX fixture text")
    document.save(str(path))

    text = await CompositeExtractor().extract(path)

    assert "DOCX fixture text" in text


@pytest.mark.asyncio
async def test_docx_extractor_reads_tables_headers_and_footers(tmp_path: Path) -> None:
    from docx import Document

    path = tmp_path / "fixture.docx"
    document = Document()
    document.sections[0].header.paragraphs[0].text = "Header text"
    document.add_paragraph("Body paragraph")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Table left"
    table.cell(0, 1).text = "Table right"
    document.sections[0].footer.paragraphs[0].text = "Footer text"
    document.save(str(path))

    text = await CompositeExtractor().extract(path)

    assert "Header text" in text
    assert "Body paragraph" in text
    assert "Table left | Table right" in text
    assert "Footer text" in text


@pytest.mark.asyncio
async def test_docx_extractor_preserves_list_markers(tmp_path: Path) -> None:
    from docx import Document

    path = tmp_path / "fixture.docx"
    document = Document()
    document.add_paragraph("Bullet item", style="List Bullet")
    document.add_paragraph("Numbered item", style="List Number")
    document.save(str(path))

    text = await CompositeExtractor().extract(path)

    assert "- Bullet item" in text
    assert "1. Numbered item" in text


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


@pytest.mark.asyncio
async def test_image_ocr_extractor_passes_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fixture.png"
    path.write_bytes(b"not real image")
    captured_kwargs: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="OCR text\n")

    def fake_which(name: str) -> str:
        return "/usr/bin/tesseract"

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(extractors.subprocess, "run", fake_run)

    text = await ImageOcrExtractor(timeout_seconds=7).extract(path)

    assert text == "OCR text"
    assert captured_kwargs["timeout"] == 7


@pytest.mark.asyncio
async def test_pdf_ocr_passes_timeout_to_rasterizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fixture.pdf"
    path.write_bytes(b"%PDF")
    captured_kwargs: dict[str, Any] = {}

    class FakePdf2Image:
        @staticmethod
        def convert_from_path(*args: Any, **kwargs: Any) -> list[Path]:
            del args
            captured_kwargs.update(kwargs)
            return [tmp_path / "page.png"]

    class FakePage:
        @staticmethod
        def extract_text() -> None:
            return None

    class FakePdf:
        pages = [FakePage()]

        def __enter__(self) -> "FakePdf":
            return self

        def __exit__(self, *args: object) -> None:
            del args

    class FakePdfPlumber:
        @staticmethod
        def open(path: Path) -> FakePdf:
            del path
            return FakePdf()

    def fake_import_module(name: str) -> object:
        if name == "pdfplumber":
            return FakePdfPlumber
        if name == "pdf2image":
            return FakePdf2Image
        return __import__(name)

    def fake_which(name: str) -> str:
        return f"/usr/bin/{name}"

    def fake_ocr_image(*args: object, **kwargs: object) -> str:
        del args, kwargs
        return "OCR text"

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(extractors.importlib, "import_module", fake_import_module)
    monkeypatch.setattr("librarian.ingest.extractors._ocr_image", fake_ocr_image)

    text = await PdfExtractor(ocr_timeout_seconds=9, ocr_correction_mode="never").extract(path)

    assert "## Page 1" in text
    assert "OCR text" in text
    assert "source: ocr" in text
    assert captured_kwargs["timeout"] == 9


@pytest.mark.asyncio
async def test_pdf_extractor_ocr_handles_mixed_text_and_scanned_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fixture.pdf"
    path.write_bytes(b"%PDF")

    class FakePage:
        def __init__(self, text: str | None) -> None:
            self._text = text

        def extract_text(self) -> str | None:
            return self._text

    class FakePdf:
        pages = [FakePage("Text page 1"), FakePage(None), FakePage("Text page 3")]

        def __enter__(self) -> "FakePdf":
            return self

        def __exit__(self, *args: object) -> None:
            del args

    class FakePdfPlumber:
        @staticmethod
        def open(path: Path) -> FakePdf:
            del path
            return FakePdf()

    def fake_import_module(name: str) -> object:
        if name == "pdfplumber":
            return FakePdfPlumber
        return __import__(name)

    def fake_ocr_pdf_page(*args: object, **kwargs: object) -> str:
        del args, kwargs
        return "OCR page 2"

    monkeypatch.setattr(extractors.importlib, "import_module", fake_import_module)
    monkeypatch.setattr("librarian.ingest.extractors._ocr_pdf_page", fake_ocr_pdf_page)

    text = await PdfExtractor(ocr_correction_mode="never").extract(path)

    assert "## Page 1" in text
    assert "Text page 1" in text
    assert "## Page 2" in text
    assert "OCR page 2" in text
    assert "## Page 3" in text
    assert "Text page 3" in text


@pytest.mark.asyncio
async def test_pdf_extractor_fails_when_mixed_page_ocr_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fixture.pdf"
    path.write_bytes(b"%PDF")

    class FakePage:
        def __init__(self, text: str | None) -> None:
            self._text = text

        def extract_text(self) -> str | None:
            return self._text

    class FakePdf:
        pages = [FakePage("Text page"), FakePage(None)]

        def __enter__(self) -> "FakePdf":
            return self

        def __exit__(self, *args: object) -> None:
            del args

    class FakePdfPlumber:
        @staticmethod
        def open(path: Path) -> FakePdf:
            del path
            return FakePdf()

    def fake_import_module(name: str) -> object:
        if name == "pdfplumber":
            return FakePdfPlumber
        return __import__(name)

    def fake_ocr_pdf_page(*args: object, **kwargs: object) -> str:
        del args, kwargs
        raise RuntimeError("missing tesseract")

    monkeypatch.setattr(extractors.importlib, "import_module", fake_import_module)
    monkeypatch.setattr("librarian.ingest.extractors._ocr_pdf_page", fake_ocr_pdf_page)

    with pytest.raises(RuntimeError, match="Unable to OCR scanned PDF page 2"):
        await PdfExtractor(ocr_correction_mode="never").extract(path)


@pytest.mark.asyncio
async def test_pdf_extractor_fails_when_scanned_pages_exceed_ocr_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fixture.pdf"
    path.write_bytes(b"%PDF")

    class FakePage:
        def __init__(self, text: str | None) -> None:
            self._text = text

        def extract_text(self) -> str | None:
            return self._text

    class FakePdf:
        pages = [FakePage("Text page"), FakePage(None), FakePage(None)]

        def __enter__(self) -> "FakePdf":
            return self

        def __exit__(self, *args: object) -> None:
            del args

    class FakePdfPlumber:
        @staticmethod
        def open(path: Path) -> FakePdf:
            del path
            return FakePdf()

    def fake_import_module(name: str) -> object:
        if name == "pdfplumber":
            return FakePdfPlumber
        return __import__(name)

    def fake_ocr_pdf_page(*args: object, **kwargs: object) -> str:
        del args, kwargs
        return "OCR page 2"

    monkeypatch.setattr(extractors.importlib, "import_module", fake_import_module)
    monkeypatch.setattr("librarian.ingest.extractors._ocr_pdf_page", fake_ocr_pdf_page)

    with pytest.raises(ValueError, match="exceeding OCR page limit 1"):
        await PdfExtractor(ocr_pdf_max_pages=1).extract(path)


@pytest.mark.asyncio
async def test_pdf_extractor_fails_when_page_count_exceeds_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fixture.pdf"
    path.write_bytes(b"%PDF")

    class FakePage:
        @staticmethod
        def extract_text() -> str:
            return "page text"

    class FakePdf:
        pages = [FakePage(), FakePage()]

        def __enter__(self) -> "FakePdf":
            return self

        def __exit__(self, *args: object) -> None:
            del args

    class FakePdfPlumber:
        @staticmethod
        def open(path: Path) -> FakePdf:
            del path
            return FakePdf()

    def fake_import_module(name: str) -> object:
        if name == "pdfplumber":
            return FakePdfPlumber
        return __import__(name)

    monkeypatch.setattr(extractors.importlib, "import_module", fake_import_module)

    with pytest.raises(ValueError, match="2 pages, exceeding configured limit 1"):
        await PdfExtractor(max_pages=1).extract(path)


@pytest.mark.asyncio
async def test_pdf_extractor_llm_corrects_ocr_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fixture.pdf"
    path.write_bytes(b"%PDF")

    class FakeProvider:
        name = "fake"

        async def complete(self, **kwargs: object) -> str:
            assert "OCR raw" in str(kwargs["user_prompt"])
            return "OCR corrected"

    class FakePage:
        @staticmethod
        def extract_text() -> None:
            return None

    class FakePdf:
        pages = [FakePage()]

        def __enter__(self) -> "FakePdf":
            return self

        def __exit__(self, *args: object) -> None:
            del args

    class FakePdfPlumber:
        @staticmethod
        def open(path: Path) -> FakePdf:
            del path
            return FakePdf()

    def fake_import_module(name: str) -> object:
        if name == "pdfplumber":
            return FakePdfPlumber
        return __import__(name)

    def fake_ocr_pdf_page(*args: object, **kwargs: object) -> str:
        del args, kwargs
        return "OCR raw"

    monkeypatch.setattr(extractors.importlib, "import_module", fake_import_module)
    monkeypatch.setattr("librarian.ingest.extractors._ocr_pdf_page", fake_ocr_pdf_page)

    extractor = PdfExtractor(ocr_correction_provider=FakeProvider())
    text = await extractor.extract(path)

    assert "OCR corrected" in text
    assert "corrected: true" in text
    assert extractor.last_metadata is not None
    pages = extractor.last_metadata["pages"]
    assert isinstance(pages, list)
    assert pages[0]["corrected"] is True
