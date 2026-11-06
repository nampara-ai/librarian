"""Page vision: figures, tables, equations and scans read by a vision model."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest

from librarian.ingest.extractors import (
    CompositeExtractor,
    OcrTextResult,
    PageObjects,
    PageVision,
    PdfExtractor,
    PdfPageExtraction,
    render_pdf_page_png,
    vision_page_candidates,
    visual_blocks,
)

PAGE_BREAK = "\n\n<!-- page-break -->\n\n"
PROSE = "The quick brown fox jumps over the lazy dog near the river bank today."


def _pdf_bytes(pages: list[tuple[list[str], bool]]) -> bytes:
    """Build a PDF with Helvetica text lines and, optionally, a raster image per page."""
    objects: list[bytes | None] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Length 4 >>\nstream\n\x00\xff\xff\x00\nendstream",
    ]
    kids: list[int] = []
    for lines, image in pages:
        content = b""
        y = 350
        for line in lines:
            content += b"BT /F1 12 Tf 50 %d Td (%s) Tj ET\n" % (y, line.encode("latin-1"))
            y -= 16
        if image:
            content += b"q 300 0 0 200 150 100 cm /Im1 Do Q\n"
        objects.append(b"<< /Length %d >>\nstream\n%sendstream" % (len(content), content))
        content_ref = len(objects)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 400] /Resources << /Font "
            b"<< /F1 3 0 R >> /XObject << /Im1 4 0 R >> >> /Contents %d 0 R >>" % content_ref
        )
        kids.append(len(objects))
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % kid for kid in kids),
        len(kids),
    )
    pdf = b"%PDF-1.4\n"
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        assert obj is not None
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (index, obj)
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        pdf += b"%010d 00000 n \n" % offset
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return pdf


class _VisionProvider:
    """Returns scripted Markdown per page; records what it was asked."""

    name = "vision"

    def __init__(self, replies: dict[int, str], *, fail: set[int] | None = None) -> None:
        self.replies = replies
        self.fail = fail or set()
        self.calls: list[int] = []
        self.images: list[int] = []
        self._next = 0

    async def describe_image(
        self,
        *,
        image_base64: str,
        media_type: str,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        del system_prompt, user_prompt, model, max_tokens, temperature
        assert media_type == "image/png"
        self.images.append(len(image_base64))
        # Pages are described concurrently; identify the page by call order.
        number = sorted(self.replies)[self._next]
        self._next += 1
        self.calls.append(number)
        if number in self.fail:
            raise RuntimeError("model does not accept images")
        return self.replies[number]


def _mixed_pdf(tmp_path: Path) -> Path:
    """Page 1 prose, page 2 image only, page 3 image plus a stray '3'."""
    path = tmp_path / "mixed.pdf"
    path.write_bytes(_pdf_bytes([([PROSE] * 4, False), ([], True), (["3"], True)]))
    return path


def test_candidates_prefer_scans_then_images_then_drawings_then_math() -> None:
    pages = [
        PdfPageExtraction(1, PROSE * 3, "embedded"),
        PdfPageExtraction(2, "", "empty"),
        PdfPageExtraction(3, PROSE * 3, "embedded"),
        PdfPageExtraction(4, "Let ∑ x ≤ ∞ and α ≥ β.", "embedded"),
        PdfPageExtraction(5, PROSE * 3, "embedded"),
    ]
    objects = {3: PageObjects(images=1), 5: PageObjects(paths=120)}

    assert vision_page_candidates(pages, objects, max_pages=10) == [2, 3, 5, 4]
    assert vision_page_candidates(pages, objects, max_pages=2) == [2, 3]


def test_visual_blocks_keep_figures_tables_and_equations_only() -> None:
    reply = (
        "Some transcribed prose that duplicates the text layer.\n\n"
        "**Figure:** A bar chart of quarterly revenue.\nQ1 448, Q2 427.\n\n"
        "| Region | Q1 |\n| --- | --- |\n| Dallas | 448 |\n\n"
        "More prose.\n\n$$\nE = mc^2\n$$\n\nTrailing prose."
    )

    blocks = visual_blocks(reply)

    assert blocks.startswith("**Figure:** A bar chart")
    assert "| Dallas | 448 |" in blocks
    assert "$$\nE = mc^2\n$$" in blocks
    assert "prose" not in blocks


def test_render_page_png_produces_an_image(tmp_path: Path) -> None:
    png = render_pdf_page_png(_mixed_pdf(tmp_path), 1, scale=1.0)
    assert png.startswith(b"\x89PNG")


@pytest.mark.asyncio
async def test_scanned_and_image_pages_take_the_vision_reply_instead_of_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _mixed_pdf(tmp_path)
    provider = _VisionProvider(
        {
            2: "# Scanned title page\n\n**Figure:** A photograph of the author.",
            3: "Chapter three opener.\n\n**Figure:** Decorative artwork.",
        }
    )

    def no_ocr(*args: object, **kwargs: object) -> OcrTextResult:
        del args, kwargs
        raise AssertionError("vision handled these pages; tesseract must not run")

    monkeypatch.setattr("librarian.ingest.extractors._ocr_pdf_page", no_ocr)
    extractor = PdfExtractor(
        ocr_correction_mode="never",
        text_layer_engine="pdfium",
        page_markup="breaks",
        # One request at a time so the scripted replies map to pages in order.
        page_vision=PageVision(provider=provider, model="vision-model", max_concurrency=1),
    )

    text = await extractor.extract(path)

    blocks = text.strip().split(PAGE_BREAK)
    assert blocks[0].count(PROSE) == 4  # the prose page was never sent
    assert blocks[1].startswith("# Scanned title page")
    assert blocks[2].startswith("Chapter three opener.")
    assert sorted(provider.calls) == [2, 3]
    assert all(size > 1000 for size in provider.images)
    metadata = extractor.last_metadata
    assert metadata is not None
    pages = cast(list[dict[str, object]], metadata["pages"])
    assert [page["source"] for page in pages] == ["embedded", "vision", "vision"]
    assert [page["action"] for page in pages] == ["accepted", "vision", "vision"]
    report = cast(dict[str, object], metadata["page_vision"])
    assert report["pages_sent"] == 2 and report["pages_replaced"] == 2
    assert cast(dict[str, object], metadata["quality_summary"])["vision_pages"] == 2


@pytest.mark.asyncio
async def test_text_page_with_figure_is_replaced_only_when_words_are_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "figures.pdf"
    path.write_bytes(_pdf_bytes([([PROSE] * 3, True), ([PROSE] * 3, True)]))
    faithful = "\n".join([PROSE] * 3) + "\n\n**Figure:** A diagram of the river bank."
    lossy = "Lazy summary of the page.\n\n**Figure:** A diagram of the river bank."
    provider = _VisionProvider({1: faithful, 2: lossy})

    def no_ocr(*args: object, **kwargs: object) -> OcrTextResult:
        del args, kwargs
        raise AssertionError("text pages must not be sent to OCR")

    monkeypatch.setattr("librarian.ingest.extractors._ocr_pdf_page", no_ocr)
    extractor = PdfExtractor(
        ocr_correction_mode="never",
        text_layer_engine="pdfium",
        page_markup="breaks",
        # One request at a time so the scripted replies map to pages in order.
        page_vision=PageVision(provider=provider, model="vision-model", max_concurrency=1),
    )

    text = await extractor.extract(path)

    blocks = text.strip().split(PAGE_BREAK)
    assert blocks[0] == faithful  # retained the words: the reply is the page
    assert blocks[1].count(PROSE) == 3  # lossy reply: text layer kept
    assert blocks[1].endswith("**Figure:** A diagram of the river bank.")
    assert "Lazy summary" not in text
    metadata = extractor.last_metadata
    assert metadata is not None
    pages = cast(list[dict[str, object]], metadata["pages"])
    assert [page["source"] for page in pages] == ["vision", "embedded+vision"]
    report = cast(dict[str, object], metadata["page_vision"])
    assert report["pages_replaced"] == 1 and report["pages_augmented"] == 1


@pytest.mark.asyncio
async def test_vision_failure_falls_back_to_ocr_and_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _mixed_pdf(tmp_path)
    provider = _VisionProvider({2: "unused", 3: "unused"}, fail={2, 3})
    ocr_pages: list[int] = []

    def fake_ocr(*args: object, **kwargs: object) -> OcrTextResult:
        del args
        number = cast(int, kwargs["page_number"])
        ocr_pages.append(number)
        return OcrTextResult(text=f"Tesseract text for page {number}.", confidence=80.0)

    monkeypatch.setattr("librarian.ingest.extractors._ocr_pdf_page", fake_ocr)
    extractor = PdfExtractor(
        ocr_correction_mode="never",
        text_layer_engine="pdfium",
        page_markup="breaks",
        page_vision=PageVision(provider=provider, model="text-only-model"),
    )

    text = await extractor.extract(path)

    assert sorted(ocr_pages) == [2, 3]
    assert "Tesseract text for page 2." in text
    metadata = extractor.last_metadata
    assert metadata is not None
    report = cast(dict[str, object], metadata["page_vision"])
    assert report["pages_failed"] == 2
    assert "does not accept images" in str(report["error"])


@pytest.mark.asyncio
async def test_vision_page_cap_limits_requests(tmp_path: Path) -> None:
    path = tmp_path / "many.pdf"
    path.write_bytes(_pdf_bytes([([PROSE], True) for _ in range(4)]))
    provider = _VisionProvider({n: PROSE for n in range(1, 5)})
    extractor = PdfExtractor(
        ocr_correction_mode="never",
        text_layer_engine="pdfium",
        page_markup="breaks",
        page_vision=PageVision(provider=provider, model="vision-model", max_pages=2),
    )

    await extractor.extract(path)

    assert len(provider.calls) == 2


def test_composite_runs_page_vision_without_liteparse(tmp_path: Path) -> None:
    provider = _VisionProvider({2: "# Scanned title page", 3: "Opener."})
    composite = CompositeExtractor(
        figure_vision_provider=provider, figure_vision_model="vision-model"
    )
    plain = CompositeExtractor()

    assert composite.liteparse_active is False
    assert composite.page_vision_active is True
    assert composite.figure_vision_active is True
    assert composite.config_signature != plain.config_signature

    text = asyncio.run(composite.extract(_mixed_pdf(tmp_path)))

    assert "# Scanned title page" in text
    assert composite.last_metadata is not None
    assert cast(dict[str, object], composite.last_metadata["page_vision"])["pages_sent"] == 2
