import json

from librarian.pipeline.editing import decode_edits, editing_units


def test_multiline_prose_remains_bounded_without_losing_lines() -> None:
    source = "\n".join(
        f"This is line {index}, retaining a distinct observation about the experimental record."
        for index in range(200)
    )
    units = editing_units(source, target_chars=2000)
    assert len(units) > 1
    assert max(len(unit.text) for unit in units) <= 2250
    raw = json.dumps({"edits": [{"id": unit.id, "text": unit.text} for unit in units]})
    output, warnings, _ = decode_edits(raw, units)
    assert output == source
    assert not warnings


def test_multiline_lists_keep_markers_with_their_items() -> None:
    lines = [
        f"{index}. A complete item that must keep its original marker." for index in range(1, 101)
    ]
    source = "\n".join(lines)
    units = editing_units(source, target_chars=300)
    assert max(len(unit.text) for unit in units) <= 300
    assert [line for unit in units for line in unit.text.splitlines()] == lines


def test_short_ocr_blocks_share_an_editable_region_without_rewriting_source() -> None:
    source = (
        "The survey covers the\n\ncoastal region. It began last year.\n\nThe results are pending."
    )
    units = editing_units(source, target_chars=2000, pack_prose_blocks=True)
    assert len(units) == 1
    assert units[0].text == source


def test_prose_packing_never_absorbs_atomic_code() -> None:
    source = "First prose.\n\n```python\nrun()\n```\n\nLast prose."
    units = editing_units(source, pack_prose_blocks=True)
    assert [unit.kind for unit in units] == ["prose", "code", "prose"]


def test_nested_lists_and_hard_line_breaks_survive_edit_unit_joins() -> None:
    source = "- Parent item\n" + "\n".join(
        f"    - Child item {index} stays nested.  " for index in range(30)
    )
    source = source.rstrip()
    units = editing_units(source, target_chars=200, pack_prose_blocks=True)
    raw = json.dumps({"edits": [{"id": unit.id, "text": unit.text} for unit in units]})
    output, _, _ = decode_edits(raw, units)
    assert output == source
