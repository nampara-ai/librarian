import json

import pytest

from librarian.application import clean_chunks


@pytest.mark.parametrize(
    "checks",
    [
        [{"id": "c0", "confirmed": True, "reason": "Fix the join."}],
        [{"id": "c0", "confirmed": True, "reason": "Fix the join."}] * 2,
        [
            {"id": "c1", "confirmed": True, "reason": "Fix it."},
            {"id": "c0", "confirmed": False, "reason": "Not a defect."},
        ],
        [
            {"id": "c0", "confirmed": "true", "reason": "Fix the join."},
            {"id": "c1", "confirmed": False, "reason": "Not a defect."},
        ],
        [
            {"id": "c0", "confirmed": True, "reason": ""},
            {"id": "c1", "confirmed": False, "reason": "Not a defect."},
        ],
    ],
)
def test_adjudication_cannot_silently_omit_or_reassign_a_check(checks: object) -> None:
    warning, issues = clean_chunks._confirmation_issues(  # pyright: ignore[reportPrivateUsage]
        json.dumps({"checks": checks}), "u0", ["First proposal.", "Second proposal."]
    )
    assert warning == "invalid-fidelity-verification"
    assert issues == {}


def test_adjudication_keeps_explicit_rejections_separate_from_confirmed_repairs() -> None:
    raw = json.dumps(
        {
            "checks": [
                {"id": "c0", "confirmed": True, "reason": "Remove the duplicated conjunction."},
                {
                    "id": "c1",
                    "confirmed": False,
                    "reason": "The local query preserves unresolved data.",
                },
            ]
        }
    )
    warning, issues = clean_chunks._confirmation_issues(  # pyright: ignore[reportPrivateUsage]
        raw, "u0", ["Fix the conjunction.", "Delete the query."]
    )
    assert warning == "semantic-fidelity-failed"
    assert issues == {"u0": ["Fix the conjunction."]}


def test_confirmation_explanation_cannot_replace_the_proposal_it_approved() -> None:
    raw = json.dumps(
        {"checks": [{"id": "c0", "confirmed": True, "reason": "Do the opposite action instead."}]}
    )
    warning, issues = clean_chunks._confirmation_issues(  # pyright: ignore[reportPrivateUsage]
        raw, "u0", ["Retain this exact proposal."]
    )
    assert warning == "semantic-fidelity-failed"
    assert issues == {"u0": ["Retain this exact proposal."]}
