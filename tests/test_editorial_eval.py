import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from librarian.maintainer.editorial_eval import CRITERIA, EditorialCase, validate_assessment


def judgment() -> dict[str, object]:
    return {"criteria": dict.fromkeys(CRITERIA, "pass"), "requirements": [], "issues": []}


def test_editorial_assessment_accepts_faithful_prose() -> None:
    case = EditorialCase(id="faithful", source="The result remained uncertain.")
    result = validate_assessment(
        case, "The result was still uncertain.", json.dumps(judgment()), model="judge", prompt="p"
    )
    assert result.passed
    assert result.candidate_sha256 == hashlib.sha256(b"The result was still uncertain.").hexdigest()


def test_protected_data_overrides_a_passing_model_judgment() -> None:
    case = EditorialCase(id="numbers", source="Nora recorded 15 failures.")
    result = validate_assessment(
        case, "Nora recorded 20 failures.", json.dumps(judgment()), model="judge", prompt="p"
    )
    assert not result.passed
    assert "missing-verbatim-number" in result.protected_failures


def test_missing_or_duplicated_requirements_cannot_pass() -> None:
    case = EditorialCase(id="oracle", source="A source.", requirements=["Meaning survives."])
    result = validate_assessment(case, "A source.", json.dumps(judgment()), model="j", prompt="p")
    assert not result.passed
    assert "requirements-missing-duplicated-or-reordered" in result.assessment_errors


def test_model_evidence_must_exist_and_match_its_verdict() -> None:
    case = EditorialCase(id="evidence", source="It might work.")
    payload = judgment()
    payload["issues"] = [
        {
            "criterion": "fidelity",
            "source_quote": "Fabricated source words",
            "candidate_quote": "Fabricated candidate words",
            "explanation": "An invented issue.",
        }
    ]
    result = validate_assessment(case, "It might work.", json.dumps(payload), model="j", prompt="p")
    assert not result.passed
    assert "invented-source-evidence" in result.assessment_errors
    assert "invented-candidate-evidence" in result.assessment_errors
    assert "issue-contradicts-passing-criterion" in result.assessment_errors


def test_uncertainty_and_review_annotations_do_not_inflate_cleanup_success() -> None:
    case = EditorialCase(id="unclear", source="Uncertain text.")
    result = validate_assessment(
        case,
        "[transcription unclear: Uncertain text]",
        json.dumps(judgment()),
        model="j",
        prompt="p",
    )
    assert not result.passed
    assert result.unresolved_readings == 1


def test_failed_criterion_requires_evidence() -> None:
    payload = judgment()
    payload["criteria"] = {**dict.fromkeys(CRITERIA, "pass"), "fidelity": "fail"}
    case = EditorialCase(id="failure", source="Original text.")
    result = validate_assessment(case, "Edited text.", json.dumps(payload), model="j", prompt="p")
    assert not result.passed
    assert "failed-check-without-evidence" in result.assessment_errors


def test_incomplete_judgment_is_invalid() -> None:
    case = EditorialCase(id="invalid", source="Original text.")
    with pytest.raises(ValidationError):
        validate_assessment(case, "Edited text.", "{}", model="j", prompt="p")


def test_frozen_holdout_hashes_and_families_are_intact() -> None:
    path = Path(__file__).resolve().parents[1] / "examples/editorial_holdout_v1.json"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "2caa22bd339e1314cebd7c07aa62e87b0d0d0058fe589368c54b4913fd2d7b41"
    )
    suite = json.loads(raw)
    assert len(suite["cases"]) == 10
    assert len({case["category"] for case in suite["cases"]}) == 5
    assert len({case["corruption_method"] for case in suite["cases"]}) == 2
    for case in suite["cases"]:
        for field in ("input", "reference"):
            text = case["input_text"] if field == "input" else case["reference"]
            assert hashlib.sha256(text.encode()).hexdigest() == case[field + "_sha256"]
