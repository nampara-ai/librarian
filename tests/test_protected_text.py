import pytest

from librarian.pipeline.protected_text import protect_numerals


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
