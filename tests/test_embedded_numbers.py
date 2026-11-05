import pytest

from librarian.pipeline.protected_text import protect_prose_data
from librarian.pipeline.validation import validate_cleaned_text


@pytest.mark.parametrize(
    "source,candidate",
    [
        ("The sample weighs 12.5mg.", "The sample weighs 12.6mg."),
        ("Use build v1.2.", "Use build v2.2."),
        ("The reading is 25ppm.", "The reading is 35ppm."),
    ],
)
def test_embedded_numeric_changes_cannot_escape_data_validation(
    source: str, candidate: str
) -> None:
    result = validate_cleaned_text(candidate, input_size=len(source), source_text=source)
    assert "missing-verbatim-number" in result.warnings


def test_valid_unit_spacing_preserves_the_same_numeric_value() -> None:
    source = "The sample weighs 12.5mg."
    result = validate_cleaned_text(
        "The sample weighs 12.5 mg.", input_size=len(source), source_text=source
    )
    assert "missing-verbatim-number" not in result.warnings
    assert "added-verbatim-number" not in result.warnings


def test_protection_covers_complete_decimal_and_version_components() -> None:
    source = "Use v1.2 to measure 12.5mg and 25ppm."
    protected = protect_prose_data(source)
    assert [value for _, value in protected.values] == ["1.2", "12.5", "25"]
    assert protected.restore(protected.text) == (source, ())


def test_numeric_guard_allows_letter_digit_ocr_confusions_inside_words() -> None:
    source = "Sadd1e fitting and canter transit10ns."
    result = validate_cleaned_text(
        "Saddle fitting and canter transitions.", input_size=len(source), source_text=source
    )
    assert "missing-verbatim-number" not in result.warnings
    assert "added-verbatim-number" not in result.warnings


def test_numeric_source_marker_literals_remain_exact() -> None:
    source = "A literal [[LBR_01234567_A]] precedes 15mg."
    protected = protect_prose_data(source)
    assert protected.restore(protected.text) == (source, ())
