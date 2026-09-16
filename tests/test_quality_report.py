"""Document-wide quality checks complement per-chunk validation."""

from librarian.application.quality_report import audit_final_document


def test_final_audit_detects_missing_pages_and_images() -> None:
    source = "# Sample Guide\n\n![](image_p1_0.png)\n\n<!-- page-break -->\n\nSecond page."

    report = audit_final_document(source, "# Sample Guide\n\nSecond page.", title="Sample Guide")

    assert report["fatal"] == ["image-references-changed", "page-boundaries-changed"]


def test_final_audit_accepts_intact_pages_and_title() -> None:
    source = "# Sample Guide\n\nFirst.\n\n<!-- page-break -->\n\nSecond."

    report = audit_final_document(source, source, title="Sample Guide")

    assert report["fatal"] == []
    assert report["warnings"] == []
    assert report["pages"] == 2


def test_index_order_ignores_single_letters_before_index_heading() -> None:
    source = (
        "# Guide\n\nL\n\nPreface\n\nIndex\n\nA\n\nApple 1\n\nB\n\nBanana 2"
        "\n\nC\n\nCherry 3\n\nD\n\nDate 4"
    )

    report = audit_final_document(source, source, title="Guide")

    assert report["index_letter_order_ok"] is True
    assert report["warnings"] == []


def test_final_audit_flags_unmatched_contents_entry_without_self_matching() -> None:
    source = "## Contents\n- Missing chapter heading 3\n\n## Present chapter\nBody text."

    report = audit_final_document(source, source, title=None)

    assert report["toc_entries_without_matching_heading_or_body"] == 1
    assert report["warnings"] == ["toc-entries-unmatched"]
