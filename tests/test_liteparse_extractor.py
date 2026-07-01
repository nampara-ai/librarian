"""Tests for the optional liteparse-backed extraction engine."""

import asyncio
import shutil
from pathlib import Path

import pytest

from librarian.ingest.extractors import (
    IMAGE_EXTENSIONS,
    CompositeExtractor,
    FallbackExtractor,
    LiteParseExtractor,
    liteparse_available,
)

requires_liteparse = pytest.mark.skipif(
    not liteparse_available(), reason="liteparse extra not installed"
)


def _table_pdf_bytes() -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 400] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    rows = [
        (b"Region", b"Q1", b"Q2"),
        (b"Dallas", b"448", b"427"),
        (b"Austin", b"310", b"295"),
        (b"Houston", b"512", b"533"),
        (b"Denver", b"201", b"245"),
    ]
    y = 300
    lines: list[bytes] = []
    for region, q1, q2 in rows:
        lines.append(
            b"BT /F1 12 Tf 50 %d Td (%s) Tj 220 0 Td (%s) Tj 120 0 Td (%s) Tj ET"
            % (y, region, q1, q2)
        )
        y -= 20
    stream = b"BT /F1 20 Tf 50 350 Td (Quarterly Report) Tj ET\n" + b"\n".join(lines) + b"\n"
    objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))

    pdf = b"%PDF-1.4\n"
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (index, obj)
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        pdf += b"%010d 00000 n \n" % off
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return pdf


@requires_liteparse
def test_liteparse_extractor_reconstructs_markdown_table(tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(_table_pdf_bytes())

    markdown = asyncio.run(LiteParseExtractor().extract(source))

    assert "# Quarterly Report" in markdown
    assert "| Region | Q1 | Q2 |" in markdown
    assert "| Dallas | 448 | 427 |" in markdown


@requires_liteparse
def test_liteparse_extractor_forwards_tessdata_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import liteparse

    captured: dict[str, object] = {}

    class _RecordingParser:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def parse(self, _path: str) -> object:
            class _Result:
                text = "extracted"
                images: list[object] = []

            return _Result()

    monkeypatch.setattr(liteparse, "LiteParse", _RecordingParser)
    source = tmp_path / "doc.pdf"
    source.write_bytes(_table_pdf_bytes())

    asyncio.run(LiteParseExtractor(tessdata_path="/bundle/tessdata").extract(source))

    assert captured["tessdata_path"] == "/bundle/tessdata"


@requires_liteparse
def test_composite_forwards_liteparse_tessdata_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import liteparse

    captured: dict[str, object] = {}

    class _RecordingParser:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def parse(self, _path: str) -> object:
            class _Result:
                text = "extracted"
                images: list[object] = []

            return _Result()

    monkeypatch.setattr(liteparse, "LiteParse", _RecordingParser)
    source = tmp_path / "doc.pdf"
    source.write_bytes(_table_pdf_bytes())
    composite = CompositeExtractor(liteparse_tessdata_path="/bundle/tessdata")

    asyncio.run(composite.extract(source))

    assert captured["tessdata_path"] == "/bundle/tessdata"


@requires_liteparse
def test_composite_routes_pdf_through_liteparse_by_default(tmp_path: Path) -> None:
    source = tmp_path / "report.pdf"
    source.write_bytes(_table_pdf_bytes())

    composite = CompositeExtractor()  # pdf_engine="auto"
    assert composite.liteparse_active is True
    assert ".pdf" in composite.supported_extensions
    assert IMAGE_EXTENSIONS <= composite.supported_extensions

    markdown = asyncio.run(composite.extract(source))

    assert "| Region | Q1 | Q2 |" in markdown
    assert composite.last_metadata is not None
    assert composite.last_metadata["engine"] == "liteparse"


def test_legacy_engine_does_not_activate_liteparse() -> None:
    composite = CompositeExtractor(pdf_engine="legacy")
    assert composite.liteparse_active is False


def test_auto_engine_falls_back_to_legacy_when_liteparse_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("librarian.ingest.extractors.liteparse_available", lambda: False)
    composite = CompositeExtractor(pdf_engine="auto")
    assert composite.liteparse_active is False


def test_fallback_extractor_uses_legacy_on_primary_failure(tmp_path: Path) -> None:
    class FailingPrimary:
        supported_extensions = frozenset({".pdf"})

        async def extract(self, path: Path) -> str:
            del path
            raise RuntimeError("engine unavailable")

    class LegacyFallback:
        supported_extensions = frozenset({".pdf"})
        last_metadata = {"engine": "legacy"}

        async def extract(self, path: Path) -> str:
            del path
            return "legacy extracted text"

    fallback = FallbackExtractor(
        FailingPrimary(), LegacyFallback(), supported_extensions=frozenset({".pdf"})
    )

    result = asyncio.run(fallback.extract(tmp_path / "x.pdf"))

    assert result == "legacy extracted text"
    # The downgrade is recorded so a permanently broken primary is diagnosable.
    assert fallback.last_metadata is not None
    assert fallback.last_metadata["engine"] == "legacy"
    assert fallback.last_metadata["fallback_from_primary"] is True
    assert "engine unavailable" in str(fallback.last_metadata["primary_error"])


def _text_image_png(tmp_path: Path, *, rotate: int = 0) -> Path:
    image_module = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw, ImageFont

    image = image_module.new("RGB", (820, 300), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 30)
    except OSError:
        font = ImageFont.load_default()
    for index, line in enumerate(
        ["The annual report summarizes revenue", "and dividend policy for shareholders."]
    ):
        draw.text((25, 40 + index * 90), line, fill="black", font=font)
    path = tmp_path / f"report_{rotate}.png"
    image.rotate(rotate, expand=True, fillcolor="white").save(path)
    return path


@requires_liteparse
def test_liteparse_extracts_image_without_imagemagick(tmp_path: Path) -> None:
    pytest.importorskip("PIL.Image")
    source = _text_image_png(tmp_path)

    # liteparse cannot ingest a loose image without ImageMagick; the extractor
    # converts it to a one-page PDF with Pillow first, so this must succeed.
    markdown = asyncio.run(LiteParseExtractor().extract(source))

    assert "revenue" in markdown.lower()
    assert "dividend" in markdown.lower()


@requires_liteparse
def test_composite_routes_image_through_liteparse(tmp_path: Path) -> None:
    pytest.importorskip("PIL.Image")
    source = _text_image_png(tmp_path)

    composite = CompositeExtractor()
    markdown = asyncio.run(composite.extract(source))

    assert "revenue" in markdown.lower()
    assert composite.last_metadata is not None
    assert composite.last_metadata["engine"] == "liteparse"


@requires_liteparse
@pytest.mark.skipif(shutil.which("tesseract") is None, reason="no system tesseract")
def test_liteparse_image_shim_orients_rotated_scan(tmp_path: Path) -> None:
    pytest.importorskip("PIL.Image")
    source = _text_image_png(tmp_path, rotate=180)

    markdown = asyncio.run(LiteParseExtractor(auto_orient=True).extract(source))

    # Upright text recovered from an upside-down image (OSD orients before OCR).
    assert "revenue" in markdown.lower()
    assert "dividend" in markdown.lower()
