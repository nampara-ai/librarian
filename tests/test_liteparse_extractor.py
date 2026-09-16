"""Tests for the optional liteparse-backed extraction engine."""

import asyncio
import io
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from librarian.ingest.extractors import (
    IMAGE_EXTENSIONS,
    CompositeExtractor,
    FallbackExtractor,
    FigureImage,
    LiteParseExtractor,
    _attach_unreferenced_figures,  # pyright: ignore[reportPrivateUsage]
    _chapter_openers,  # pyright: ignore[reportPrivateUsage]
    _parse_liteparse_pages_incrementally,  # pyright: ignore[reportPrivateUsage]
    _repair_merged_table_heading,  # pyright: ignore[reportPrivateUsage]
    _restore_missing_native_body_lines,  # pyright: ignore[reportPrivateUsage]
    _restore_native_figure_captions,  # pyright: ignore[reportPrivateUsage]
    _strip_recurring_side_furniture,  # pyright: ignore[reportPrivateUsage]
    _suppress_figure_ocr_zones,  # pyright: ignore[reportPrivateUsage]
    liteparse_available,
    normalize_spatial_markdown,
    reflow_multicolumn_markdown,
)

requires_liteparse = pytest.mark.skipif(
    not liteparse_available(), reason="liteparse extra not installed"
)


def test_merged_table_heading_recovers_caption_columns_and_first_row() -> None:
    def item(text: str, x: int, y: int) -> SimpleNamespace:
        return SimpleNamespace(text=text, x=x, y=y, width=len(text) * 5, height=10)

    page = SimpleNamespace(
        text_items=[
            item("Table 3-2", 210, 390),
            item("Preferred names", 270, 390),
            item("Old name", 90, 415),
            item("New name", 215, 415),
            item("Example", 340, 415),
            item("foo", 90, 432),
            item("bar", 215, 432),
            item("bar item", 340, 432),
        ]
    )
    malformed = (
        "Intro\n\n| Old name foo | Table 3-2 New name bar | Preferred names Example bar item | |\n"
        "|---|---|---|---|\n| later | newer | next | |"
    )

    repaired, changed = _repair_merged_table_heading(page, malformed)

    assert changed
    assert "Table 3-2 Preferred names\n| Old name | New name | Example |  |" in repaired
    assert "| foo | bar | bar item |  |" in repaired
    assert "| later | newer | next | |" in repaired
    assert _repair_merged_table_heading(page, "| Table 3-2 | Preferred names |\n|---|---|") == (
        "| Table 3-2 | Preferred names |\n|---|---|",
        False,
    )


def test_missing_body_line_is_restored_between_spatial_neighbors() -> None:
    def item(text: str, x: int, y: int, width: int = 320) -> SimpleNamespace:
        return SimpleNamespace(text=text, x=x, y=y, width=width, height=10)

    before = "Readers can see the available options in the menu at any time"
    missing = "They can remember the names because the choices are always visible"
    after = "The menu remains in the same place while the application is open"
    page = SimpleNamespace(
        width=600,
        height=800,
        text_items=[
            item(before, 170, 100),
            item("Margin label", 60, 112, 50),
            item(missing, 170, 112),
            item(after, 170, 124),
        ],
    )

    output, count = _restore_missing_native_body_lines(
        page, f"{before}\n\n{after}", ()
    )

    assert count == 1
    assert output.index(before) < output.index(missing) < output.index(after)
    assert _restore_missing_native_body_lines(page, output, ()) == (output, 0)
    assert _restore_missing_native_body_lines(
        page, f"{before}\n\n{after}", [(150, 105, 500, 125)]
    )[1] == 0


def test_chapter_opener_uses_layout_title_and_ignores_later_running_header() -> None:
    def item(text: str, x: int, y: int) -> SimpleNamespace:
        return SimpleNamespace(text=text, x=x, y=y, width=len(text) * 6, height=10)

    pages = [
        SimpleNamespace(
            page_num=1,
            width=600,
            height=800,
            text_items=[
                item("C H A P T E R", 200, 28),
                item("3", 310, 28),
                item("Figure 3-0", 35, 92),
                item("Listing 3-0", 35, 103),
                item("Table 3-0", 35, 112),
                item("Human", 200, 92),
                item("Interface", 300, 92),
                item("Design", 200, 123),
            ],
        ),
        SimpleNamespace(
            page_num=2,
            width=600,
            height=800,
            text_items=[
                item("C H A P T E R", 174, 29),
                item("3", 235, 29),
                item("Human Interface Design", 174, 56),
                item("The first body paragraph is not a chapter title.", 174, 93),
            ],
        ),
    ]

    assert _chapter_openers(pages) == {1: "Chapter 3: Human Interface Design"}


def test_spatial_normalization_keeps_blank_pdf_page_without_empty_code_fence() -> None:
    page = SimpleNamespace(page_num=1, width=600, height=800, text="", text_items=[])
    reports: list[dict[str, object]] = []

    output, *_counts = normalize_spatial_markdown(
        SimpleNamespace(text="```text\n\n```", pages=[page]), page_reports=reports
    )

    assert output == ""
    assert reports[0]["action"] == "blank-page"


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


def test_spatial_normalization_separates_screenshot_ocr_from_prose() -> None:
    native = (
        "Figure 4-42 shows a menu in a dialog box. The caption explains the picture.\n"
        "Figure 4-42 Opening a pop-up menu\n"
        "Users should choose a setting and then continue their work."
    )
    markdown = (
        "Figure 4-42 shows a menu in a dialog box. The caption explains the picture.\n\n"
        "**Figure 4-42** Opening a pop-up menu\n\n"
        "![](image_p1_0.png)\n\n"
        "Calendar\n\n2056\n\nveor:\n\n"
        "Users should choose a setting and then continue their work."
    )
    page = SimpleNamespace(
        page_num=1,
        width=600,
        height=500,
        text=native,
        text_items=[
            SimpleNamespace(text=line, x=50, y=100 + index * 35, width=400, height=10)
            for index, line in enumerate(native.splitlines())
        ]
        + [
            SimpleNamespace(text=line, x=60, y=260 + index * 18, width=100, height=10)
            for index, line in enumerate(("Calendar", "2056", "veor:"))
        ],
    )
    reports: list[dict[str, object]] = []

    output, _columns, _outlines, _tables = normalize_spatial_markdown(
        SimpleNamespace(text=markdown, pages=[page]),
        page_reports=reports,
        figure_regions={1: [(50, 245, 180, 330)]},
    )

    assert "Calendar" not in output
    assert "2056" not in output
    assert "veor:" not in output
    assert "**Figure 4-42** Opening a pop-up menu" in output
    assert "Users should choose a setting" in output
    assert output.count("![](image_p1_0.png)") == 1
    assert reports[0]["figure_ocr_lines_removed"] == 3
    assert reports[0]["warnings"] == []


def test_spatial_normalization_removes_screenshot_ocr_before_image_token() -> None:
    body = (
        "The guide explains how to save a document and where the resulting file appears. "
        "The user can choose a destination, enter a name, and confirm the dialog."
    )
    markdown = (
        f"{body}\n\n**Figure 3-1** Save dialog\n\n"
        "Desktop items\n\ni Trash\n\nType a name for your document here\n\n"
        "![](image_p1_0.png)"
    )
    page = SimpleNamespace(
        page_num=1,
        width=600,
        height=500,
        text=body,
        text_items=[
            SimpleNamespace(text=body, x=50, y=100, width=450, height=10),
            SimpleNamespace(text="Desktop items", x=80, y=270, width=90, height=10),
            SimpleNamespace(text="i", x=80, y=280, width=8, height=8),
            SimpleNamespace(text="Trash", x=90, y=284, width=40, height=8),
            SimpleNamespace(
                text="Type a name for your document here", x=80, y=290, width=220, height=10
            ),
        ],
    )
    reports: list[dict[str, object]] = []

    output, *_counts = normalize_spatial_markdown(
        SimpleNamespace(text=markdown, pages=[page]),
        page_reports=reports,
        figure_regions={1: [(60, 250, 320, 330)]},
    )

    assert "Desktop items" not in output
    assert "i Trash" not in output
    assert "Type a name for your document here" not in output
    assert "**Figure 3-1** Save dialog" in output
    assert body in output
    assert output.count("![](image_p1_0.png)") == 1
    assert reports[0]["figure_ocr_lines_removed"] == 3


def test_recurring_vertical_margin_label_is_removed_without_losing_body() -> None:
    pages = [
        SimpleNamespace(
            width=600,
            height=800,
            text_items=[
                SimpleNamespace(text="Body copy", x=100, y=445, width=140, height=10),
                SimpleNamespace(text="More body", x=100, y=520, width=130, height=10),
                *[
                    SimpleNamespace(text=label, x=570, y=y, width=10, height=8)
                    for label, y in zip(
                        ("|", "1", "M", "ar", "gin"), (450, 465, 480, 495, 510), strict=True
                    )
                ],
            ],
        )
        for _ in range(3)
    ]
    blocks = ["Body copy |\n1\nM\nar\ngin\nMore body" for _ in pages]

    removed = _strip_recurring_side_furniture(pages, blocks)

    assert all(count >= 4 for count in removed)
    assert blocks == ["Body copy\nMore body"] * 3

    table_blocks = ["| Label | 1 M ar gin |\n| Value | More body |" for _ in pages]
    inline_removed = _strip_recurring_side_furniture(pages, table_blocks)
    assert inline_removed == [1, 1, 1]
    assert all("M ar gin" not in block for block in table_blocks)


def test_unreferenced_raster_figure_is_restored_at_its_caption(tmp_path: Path) -> None:
    image_module = pytest.importorskip("PIL.Image")
    image = image_module.new("RGB", (120, 90), "white")
    source = tmp_path / "figure.pdf"
    image.save(source)
    bitmap = io.BytesIO()
    image.save(bitmap, format="PNG")
    figure = FigureImage(id="p1_0", page=1, media_type="image/png", data=bitmap.getvalue())
    page = SimpleNamespace(
        page_num=1,
        height=500,
        text_items=[
            SimpleNamespace(text="Figure 1-1 Sample chart", x=50, y=25, width=180, height=12)
        ],
    )
    reports: list[dict[str, object]] = [
        {"action": "accepted", "image_references": 0, "figure_references_restored": 0}
    ]

    output, count = _attach_unreferenced_figures(
        source,
        "Figure 1-1 shows the result.\n\n**Figure 1-1** Sample chart\n\n"
        "The next paragraph is body text.",
        [figure],
        [page],
        reports,
    )

    assert count == 1
    assert output.index("**Figure 1-1** Sample chart") < output.index("![](image_p1_0.png)")
    assert output.index("![](image_p1_0.png)") < output.index("The next paragraph")
    assert reports[0]["figure_references_restored"] == 1
    assert reports[0]["image_references"] == 1


def test_figure_ocr_zone_keeps_caption_image_and_following_prose() -> None:
    body = "The menu labels remain familiar and the controls behave consistently."
    page = SimpleNamespace(
        page_num=1,
        height=500,
        text_items=[
            SimpleNamespace(text="Figure 2-1 Menu example", x=50, y=100, width=180, height=12),
            SimpleNamespace(
                text="Font Size Style New Open Close", x=200, y=160, width=180, height=12
            ),
            SimpleNamespace(text=body, x=50, y=300, width=410, height=12),
        ],
    )
    source = (
        "**Figure 2-1** Menu example\n\nFont Size Style New Open Close\n\n"
        "|---|---|---|\n\n![](image_p1_0.png)\n\n" + body
    )
    reports: list[dict[str, object]] = [
        {"figure_ocr_lines_removed": 0, "native_text_coverage": 1.0, "warnings": []}
    ]

    output = _suppress_figure_ocr_zones(source, [page], {1: [(180, 120, 420, 220)]}, reports)

    assert "Font Size Style New Open Close" not in output
    assert "|---|" not in output
    assert "**Figure 2-1** Menu example" in output
    assert "![](image_p1_0.png)" in output
    assert body in output


def test_native_caption_title_is_reattached_to_its_figure_number() -> None:
    page = SimpleNamespace(
        page_num=1,
        text_items=[
            SimpleNamespace(text="Figure 2-1 Menu example", x=50, y=100, width=180, height=12)
        ],
    )
    source = "**Figure 2-1**\n\n![](image_p1_0.png)\n\nMenu example\n\nFollowing text."
    reports: list[dict[str, object]] = [{"figure_caption_titles_restored": 0}]

    output = _restore_native_figure_captions(source, [page], {1: [(60, 120, 300, 220)]}, reports)

    assert "**Figure 2-1** Menu example" in output
    assert output.count("Menu example") == 1
    assert reports[0]["figure_caption_titles_restored"] == 1


def test_spatial_normalization_falls_back_when_renderer_loses_native_text() -> None:
    native = " ".join(
        (
            "The quarterly revenue increased by 12 percent across three regions.",
            "The northern branch added five new accounts and lowered costs.",
            "The western branch reported stable demand and improved margins.",
        )
    )
    page = SimpleNamespace(
        page_num=1,
        width=600,
        height=500,
        text=native,
        text_items=[SimpleNamespace(text=native, x=50, y=150, width=450, height=10)],
    )
    reports: list[dict[str, object]] = []

    output, _columns, _outlines, _tables = normalize_spatial_markdown(
        SimpleNamespace(text="Broken renderer output.", pages=[page]), page_reports=reports
    )

    assert output == native
    assert reports[0]["action"] == "native-text-fallback"
    assert reports[0]["native_text_coverage"] == 1.0


def test_liteparse_page_cache_resumes_after_interrupted_batch(tmp_path: Path) -> None:
    image_module = pytest.importorskip("PIL.Image")
    image = image_module.new("RGB", (20, 20), "white")
    source = tmp_path / "two-pages.pdf"
    image.save(source, save_all=True, append_images=[image.copy()])
    cache = tmp_path / "page-cache"
    requested: list[str] = []

    def parse_pages(selected: str) -> SimpleNamespace:
        requested.append(selected)
        if selected == "2" and requested.count("2") == 1:
            raise RuntimeError("interrupted")
        number = int(selected)
        page = SimpleNamespace(
            page_num=number,
            width=600,
            height=500,
            text=f"Page {number}",
            text_items=[SimpleNamespace(text=f"Page {number}", x=50, y=100, width=70, height=10)],
        )
        return SimpleNamespace(pages=[page], text=f"Page {number}")

    with pytest.raises(RuntimeError, match="interrupted"):
        _parse_liteparse_pages_incrementally(
            source, cache_directory=cache, parse_pages=parse_pages, max_pages=2, batch_size=1
        )

    result, hits = _parse_liteparse_pages_incrementally(
        source, cache_directory=cache, parse_pages=parse_pages, max_pages=2, batch_size=1
    )

    assert requested == ["1", "2", "2"]
    assert hits == 1
    assert [page.page_num for page in result.pages] == [1, 2]
    assert "Page 1" in result.text and "Page 2" in result.text


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
