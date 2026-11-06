import pytest

from librarian.pipeline.protected_text import protect_numerals, protect_prose_data
from librarian.pipeline.validation import validate_cleaned_text


def test_link_identifiers_are_protected_with_numbers_and_punctuation_round_trips() -> None:
    source = (
        "Visit author-example.com, https://example.org/v2?id=15, or www.example.net. Read 15 pages."
    )
    protected = protect_prose_data(source)
    assert [value for _, value in protected.values] == [
        "author-example.com",
        "https://example.org/v2?id=15",
        "www.example.net",
        "15",
    ]
    assert protected.restore(protected.text) == (source, ())
    assert "example" not in protected.text


def test_readable_but_changed_link_is_a_fidelity_failure() -> None:
    source = "Visit author-example.com for more."
    result = validate_cleaned_text(
        "Visit author-examples.com for more.", input_size=len(source), source_text=source
    )
    assert "changed-literal-link" in result.warnings


@pytest.mark.parametrize(
    "source",
    [
        "No numeric data here.",
        "There were 12 subscribers and 20 people.",
        "The value is 12.50, not 12.05.",
        "The identifiers are 0012 and 1,000.",
        "In 2020 and 2020, the stated value was 0.",
        "A literal [[LBR_deadbeef_A]] precedes 15.",
    ],
)
def test_numeric_protection_is_an_exact_round_trip(source: str) -> None:
    protected = protect_numerals(source)
    restored, warnings = protected.restore(protected.text)
    assert restored == source
    assert not warnings
    assert len({token for token, _ in protected.values}) == len(protected.values)


def test_equal_numeric_occurrences_have_distinct_provenance() -> None:
    protected = protect_numerals("Nora had 15 failures; Luis had 15 successes.")
    assert len(protected.values) == 2
    assert protected.values[0][0] != protected.values[1][0]
    assert protected.values[0][1] == protected.values[1][1] == "15"


def test_repeated_missing_and_invented_placeholders_fail() -> None:
    protected = protect_numerals("Nora had 15 failures.")
    token = protected.values[0][0]
    for candidate in [
        protected.text.replace(token, "fifteen"),
        protected.text + token,
        protected.text + "[[LBR_other_A]]",
    ]:
        _, warnings = protected.restore(candidate)
        assert warnings


def test_unknown_marker_cannot_pass_when_source_has_no_numeric_tokens() -> None:
    _, warnings = protect_numerals("Ordinary prose.").restore("Ordinary [[LBR_other_A]] prose.")
    assert "unknown-protected-placeholder" in warnings


def test_protection_supports_many_distinct_numeric_occurrences() -> None:
    source = " ".join(str(index) for index in range(200))
    protected = protect_numerals(source)
    assert len(protected.values) == 200
    assert protected.restore(protected.text) == (source, ())


def test_prose_mask_allows_contextual_decade_repair_but_freezes_other_numbers() -> None:
    source = (
        "Across decades, styles changed during the '7s; "
        "the interval was 9s and the dose was 12.5mg."
    )
    protected = protect_prose_data(source)
    assert "'7s" in protected.text
    assert [reading.as_dict() for reading in protected.numeric_readings] == [
        {"observed": "7", "reading": "70", "role": "decade-shorthand", "century": "unspecified"}
    ]
    assert {value for _, value in protected.values} == {"9", "12.5"}
    edited, warnings = protected.restore(protected.text.replace("'7s", "’70s"))
    assert (
        edited == "Across decades, styles changed during the ’70s; "
        "the interval was 9s and the dose was 12.5mg."
    )
    assert warnings == ()


def test_patch_can_resolve_a_local_query_using_original_temporal_context() -> None:
    source = "This happened during the '7s, a decade of change."
    candidate = "This happened during the [transcription unclear: '7s], a decade of change."
    protected = protect_prose_data(candidate, source_context=source)
    assert not protected.values
    edited, warnings = protected.restore(
        protected.text.replace("[transcription unclear: '7s]", "’70s")
    )
    assert edited == "This happened during the ’70s, a decade of change."
    assert not warnings


def test_established_decade_role_survives_a_narrative_rewrite() -> None:
    source = "During the '7s, the decade brought changes."
    candidate = "The ’7s brought changes."
    protected = protect_prose_data(candidate, source_context=source)
    assert not protected.values
    edited, warnings = protected.restore(protected.text.replace("’7s", "’70s"))
    result = validate_cleaned_text(edited, input_size=len(source), source_text=source)
    assert not warnings
    assert "missing-verbatim-number" not in result.warnings
    assert "added-verbatim-number" not in result.warnings
