import json

import pytest

from librarian.pipeline.prose_patches import apply_prose_patches


def test_patches_use_original_offsets_and_preserve_unrelated_text() -> None:
    candidate = "A.\n\nB.\n\nThe remaining paragraph stays intact."
    patches = {
        "patches": [
            {"id": "p0", "find": "A.", "replace": "B."},
            {"id": "p1", "find": "B.", "replace": "C."},
        ]
    }
    edited, warnings = apply_prose_patches(candidate, json.dumps(patches))
    assert edited == "B.\n\nC.\n\nThe remaining paragraph stays intact."
    assert not warnings


@pytest.mark.parametrize(
    "patches",
    [
        [{"id": "missing", "find": "A", "replace": "correct"}],
        [{"id": "p0", "find": "A", "replace": "correct"}],
        [{"id": "p0", "find": "B C", "replace": "B C"}],
        [
            {"id": "p0", "find": "A B", "replace": "correct"},
            {"id": "p0", "find": "B C", "replace": "other"},
        ],
        [{"id": "p0", "text": "correct", "extra": "wrong schema"}],
        [],
    ],
)
def test_invalid_or_ambiguous_patches_are_atomic(patches: list[dict[str, str]]) -> None:
    candidate = "A B C. Another A."
    edited, warnings = apply_prose_patches(candidate, json.dumps({"patches": patches}))
    assert edited == candidate
    assert warnings == ("invalid-prose-patch",)


def test_invalid_json_preserves_candidate() -> None:
    assert apply_prose_patches("Original.", "not json") == ("Original.", ("invalid-prose-patch",))


def test_span_local_anchor_can_be_unique_when_phrase_repeats_elsewhere() -> None:
    candidate = "I think it works.\n\nI think it fails."
    raw = json.dumps({"patches": [{"id": "p1", "find": "I think", "replace": "I doubt"}]})
    assert apply_prose_patches(candidate, raw) == ("I think it works.\n\nI doubt it fails.", ())
