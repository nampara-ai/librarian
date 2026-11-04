import json

import pytest

from librarian.application.editorial_context import parse_editorial_context


def test_spelling_sheet_rejects_invented_names_and_numeric_changes() -> None:
    raw = json.dumps(
        {
            "corrections": [
                {"observed": "Thomas Soul", "preferred": "Thomas Sowell"},
                {"observed": "Absent Person", "preferred": "Absent Persons"},
                {"observed": "Chapter 12", "preferred": "Chapter 13"},
                {"observed": "Thomas Soul", "preferred": "Karl Marx"},
            ]
        }
    )
    result = json.loads(parse_editorial_context(raw, "Thomas Soul discusses Chapter 12."))
    assert result == {
        "approved_spelling_corrections": [{"observed": "Thomas Soul", "preferred": "Thomas Sowell"}]
    }


@pytest.mark.parametrize("raw", ["not json", "[]", "{}", '{"corrections": "wrong"}'])
def test_invalid_spelling_sheet_is_explicit_failure(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_editorial_context(raw, "A source.")


def test_advisory_spelling_sheet_is_not_an_authority_for_changing_identifiers() -> None:
    raw = json.dumps(
        {
            "corrections": [
                {"observed": "Albrt", "preferred": "Albert"},
                {"observed": "author-example.com", "preferred": "author-examples.com"},
            ]
        }
    )
    result = json.loads(
        parse_editorial_context(raw, "Albrt writes at author-example.com.", advisory=True)
    )
    assert result == {
        "proposed_spelling_corrections": [{"observed": "Albrt", "preferred": "Albert"}]
    }
