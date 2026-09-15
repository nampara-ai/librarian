"""Tests for the optional liteparse-backed extraction engine."""

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from librarian.ingest.extractors import (
    IMAGE_EXTENSIONS,
    CompositeExtractor,
    FallbackExtractor,
    LiteParseExtractor,
    liteparse_available,
    normalize_spatial_markdown,
    reflow_multicolumn_markdown,
)

requires_liteparse = pytest.mark.skipif(
    not liteparse_available(), reason="liteparse extra not installed"
)


def test_multicolumn_reflow_uses_spatial_column_order() -> None:
    items = [SimpleNamespace(text="Index", x=50, y=20, width=40, height=10)]
    for row in range(12):
        y = 100 + row * 20
        items.extend(
            [
                SimpleNamespace(text=f"left {row}", x=50, y=y, width=70, height=10),
                SimpleNamespace(text=f"right {row}", x=350, y=y, width=75, height=10),
            ]
        )
    page = SimpleNamespace(width=600, height=500, text_items=items)
    result = SimpleNamespace(
        text="\n".join(f"left {row} right {row}" for row in range(12)), pages=[page]
    )

    output, count = reflow_multicolumn_markdown(result)

    assert count == 1
    assert output.index("left 11") < output.index("right 0")


def test_multicolumn_reflow_leaves_ordinary_prose_untouched() -> None:
    items = [
        SimpleNamespace(
            text=f"A full width prose line {row}", x=50, y=80 + row * 15, width=450, height=10
        )
        for row in range(14)
    ]
    source = "\n".join(item.text for item in items)
    result = SimpleNamespace(
        text=source,
        pages=[SimpleNamespace(width=600, height=500, text_items=items)],
    )

    assert reflow_multicolumn_markdown(result) == (source, 0)


def test_spatial_normalization_repairs_collapsed_outline() -> None:
    items = [SimpleNamespace(text="Contents", x=50, y=25, width=60, height=10)]
    labels: list[str] = []
    locators: list[str] = []
    for row in range(12):
        y = 70 + row * 18
        label = f"Topic {row}"
        locator = str(row + 3)
        labels.append(label)
        locators.append(locator)
        items.extend(
            [
                SimpleNamespace(text=label, x=50, y=y, width=120, height=10),
                *(
                    [SimpleNamespace(text="~~", x=260, y=y, width=12, height=10)]
                    if row == 0
                    else []
                ),
                SimpleNamespace(text=locator, x=310, y=y, width=18, height=10),
            ]
        )
    items.append(SimpleNamespace(text="iv", x=520, y=475, width=12, height=10))
    page = SimpleNamespace(width=600, height=500, text_items=items)
    collapsed = "\n\n".join([" ".join(labels), " ".join(locators)])
    result = SimpleNamespace(text=collapsed, pages=[page])

    output, multicolumn_count, outline_count, table_count = normalize_spatial_markdown(result)

    assert multicolumn_count == 0
    assert outline_count == 1
    assert table_count == 0
    assert output.startswith("## Contents")
    assert "- Topic 0 3" in output
    assert "- Topic 11 14" in output
    assert "~~" not in output
    assert "\niv\n" not in f"\n{output}\n"


def test_spatial_normalization_keeps_reference_rows_together() -> None:
    items = []
    table_rows: list[str] = []
    for row in range(12):
        y = 70 + row * 18
        identifier = f"Figure 2-{row + 1}"
        caption = f"Example interface {row + 1}"
        locator = str(row + 40)
        items.extend(
            [
                SimpleNamespace(text=identifier, x=50, y=y, width=75, height=10),
                SimpleNamespace(text=caption, x=350, y=y, width=110, height=10),
                SimpleNamespace(text=locator, x=520, y=y, width=18, height=10),
            ]
        )
        table_rows.append(f"| {identifier} | {caption} | {locator} |")
    page = SimpleNamespace(width=600, height=500, text_items=items)
    result = SimpleNamespace(
        text="\n".join([table_rows[0], "|---|---|---|", *table_rows[1:]]),
        pages=[page],
    )

    output, multicolumn_count, outline_count, table_count = normalize_spatial_markdown(result)

    assert multicolumn_count == 0
    assert outline_count == 1
    assert table_count == 0
    assert "- Figure 2-1 Example interface 1 40" in output
    assert "- Figure 2-12 Example interface 12 51" in output
    assert "|---|" not in output


def test_spatial_normalization_joins_wrapped_outline_entry() -> None:
    items = []
    for row in range(10):
        y = 70 + row * 25
        if row == 2:
            items.extend(
                [
                    SimpleNamespace(text="A long topic that", x=50, y=y, width=130, height=10),
                    SimpleNamespace(text="wraps cleanly", x=90, y=y + 10, width=90, height=10),
                    SimpleNamespace(text="12", x=310, y=y + 10, width=18, height=10),
                ]
            )
        else:
            items.extend(
                [
                    SimpleNamespace(text=f"Topic {row}", x=50, y=y, width=100, height=10),
                    SimpleNamespace(text=str(row + 10), x=310, y=y, width=18, height=10),
                ]
            )
    page = SimpleNamespace(width=600, height=500, text_items=items)
    result = SimpleNamespace(
        text="Topics\n\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19",
        pages=[page],
    )

    output, _multicolumn_count, outline_count, _table_count = normalize_spatial_markdown(result)

    assert outline_count == 1
    assert "- A long topic that wraps cleanly 12" in output


def test_spatial_normalization_reads_lopsided_final_index_column_last() -> None:
    items = [SimpleNamespace(text="I N D E X", x=50, y=20, width=60, height=10)]
    for row in range(24):
        items.append(
            SimpleNamespace(
                text=f"word {row} {row + 10}",
                x=50,
                y=70 + row * 15,
                width=120,
                height=10,
            )
        )
    items.extend(
        [
            SimpleNamespace(text="Z", x=350, y=85, width=10, height=10),
            SimpleNamespace(text="zoom boxes 99", x=350, y=100, width=100, height=10),
        ]
    )
    page = SimpleNamespace(width=600, height=500, text_items=items)
    collapsed_items = " ".join(str(item.text) for item in items)
    result = SimpleNamespace(
        text=f"| I N D E X | |\n|---|---|\n| {collapsed_items} | |",
        pages=[page],
    )

    output, multicolumn_count, outline_count, table_count = normalize_spatial_markdown(result)

    assert multicolumn_count == 1
    assert outline_count == 0
    assert table_count == 0
    assert output.index("word 23 33") < output.index("zoom boxes 99")


def test_spatial_normalization_repairs_collapsed_prose_and_preserves_image() -> None:
    clean_prose = (
        "A readable paragraph stays in its original word order.\n"
        "The following sentence remains attached to it."
    )
    image = "![](image_p7_0.png)"
    scrambled = " ".join(reversed(clean_prose.split()))
    result = SimpleNamespace(
        text=f"| {scrambled * 4} | {clean_prose} {image} |\n|---|---|",
        pages=[SimpleNamespace(width=600, height=500, text_items=[], text=clean_prose)],
    )

    output, multicolumn_count, outline_count, table_count = normalize_spatial_markdown(result)

    assert multicolumn_count == 0
    assert outline_count == 0
    assert table_count == 1
    assert output.startswith(clean_prose)
    assert output.count(image) == 1
    assert "|---|" not in output


def test_spatial_normalization_removes_page_headers_and_footers_by_geometry() -> None:
    pages: list[SimpleNamespace] = []
    blocks: list[str] = []
    for page_number in range(1, 4):
        items = [
            SimpleNamespace(text="Quarterly Field Report", x=50, y=25, width=140, height=10),
            SimpleNamespace(text=f"Body finding {page_number}", x=50, y=120, width=150, height=10),
            SimpleNamespace(text="2056", x=50, y=250, width=30, height=10),
            SimpleNamespace(text=f"Results {page_number}", x=50, y=470, width=80, height=10),
        ]
        pages.append(SimpleNamespace(width=600, height=500, text_items=items))
        blocks.append(
            "\n\n".join(
                (
                    "###### Quarterly Field Report",
                    f"Body finding {page_number}",
                    "2056",
                    f"Results {page_number}",
                    "6 Dialog Boxes",
                )
            )
        )
    result = SimpleNamespace(text="\n\n-----\n\n".join(blocks), pages=pages)

    output, multicolumn_count, outline_count, table_count = normalize_spatial_markdown(result)

    # Keep page one's possible document title; discard later copies and every
    # footer. A number in the page body must remain untouched.
    assert output.count("Quarterly Field Report") == 1
    assert "Results 1" not in output
    assert "Results 2" not in output
    assert "Results 3" not in output
    assert "6 Dialog Boxes" not in output
    assert output.count("2056") == 3
    assert multicolumn_count == outline_count == table_count == 0


def test_spatial_normalization_preserves_unique_bottom_edge_content() -> None:
    result = SimpleNamespace(
        text="Body finding\n\nRevenue 2024",
        pages=[
            SimpleNamespace(
                width=600,
                height=500,
                text_items=[
                    SimpleNamespace(text="Body finding", x=50, y=120, width=100, height=10),
                    SimpleNamespace(text="Revenue 2024", x=50, y=470, width=100, height=10),
                ],
            )
        ],
    )

    output, _columns, _outlines, _tables = normalize_spatial_markdown(result)

    assert "Revenue 2024" in output


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


def _fixture_font(size: int):
    """A real scalable font on any OS.

    Pillow's tiny bitmap fallback renders text too small for Tesseract's
    orientation detection (macOS runners have no DejaVu at the Linux path).
    """
    from PIL import ImageFont

    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Linux
        "/System/Library/Fonts/Supplemental/Arial.ttf",  # macOS
        "/System/Library/Fonts/Helvetica.ttc",  # macOS
        "C:/Windows/Fonts/arial.ttf",  # Windows
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    try:
        # Pillow >= 10.1 renders its default font at any size.
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - ancient Pillow only
        return ImageFont.load_default()


def _text_image_png(tmp_path: Path, *, rotate: int = 0) -> Path:
    image_module = pytest.importorskip("PIL.Image")
    from PIL import ImageDraw

    image = image_module.new("RGB", (820, 300), "white")
    draw = ImageDraw.Draw(image)
    font = _fixture_font(30)
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
