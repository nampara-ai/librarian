import json
from dataclasses import replace
from pathlib import Path

import pytest

from librarian.application import eval as eval_module
from librarian.application.eval import (
    EvalCase,
    EvalSuite,
    eval_result_json,
    load_eval_suite,
    run_eval_suite,
)
from librarian.application.factory import build_container
from librarian.config import Settings

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"


@pytest.mark.asyncio
async def test_eval_suite_runs_against_configured_stack(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    suite = EvalSuite(
        cases=[
            EvalCase(
                name="horse notes",
                input_text="A horse training transcript about groundwork.",
                tags=["classification"],
                expected_contains=["groundwork"],
                expected_classification_prefix="636",
                min_output_chars=10,
            )
        ]
    )

    result = await run_eval_suite(container, suite)

    assert result.passed
    assert result.provider == "mock"
    assert result.cases[0].classification_code == "636.1"
    assert result.cases[0].chars_per_second > 0
    rendered = json.loads(eval_result_json(result))
    assert rendered["artifact_type"] == "librarian-eval-result"
    assert rendered["evidence_tier"] == "mock-smoke"
    assert rendered["librarian_version"]
    assert rendered["generated_at"].endswith("+00:00")
    assert rendered["cleaning_prompt_version"] == "cmos_v2"
    assert rendered["classification_prompt_version"] == "dewey_v2"
    assert rendered["summary"]["case_count"] == 1
    assert rendered["summary"]["passed_count"] == 1
    assert rendered["summary"]["failed_count"] == 0
    assert rendered["summary"]["pass_rate"] == 1
    assert rendered["summary"]["warning_count"] == 0
    assert rendered["summary"]["failure_count"] == 0
    assert rendered["summary"]["warning_case_count"] == 0
    assert rendered["summary"]["failure_case_count"] == 0
    assert rendered["cases"][0]["tags"] == ["classification"]
    assert rendered["cases"][0]["warnings"] == []
    assert rendered["cases"][0]["output_char_ratio"] > 0


@pytest.mark.asyncio
async def test_eval_suite_fails_on_unexpected_quality_warnings(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)

    class CollapsingProvider:
        name = "collapsing"

        async def complete(
            self,
            *,
            system_prompt: str,
            user_prompt: str,
            model: str,
            max_tokens: int,
            temperature: float,
        ) -> str:
            del system_prompt, user_prompt, model, max_tokens, temperature
            return "Horse Notes Saddle fit Groundwork"

    cleaner = replace(container.process_document.cleaner, provider=CollapsingProvider())
    process_document = replace(container.process_document, cleaner=cleaner)
    container = replace(container, process_document=process_document)
    suite = EvalSuite(
        cases=[
            EvalCase(
                name="markdown quality",
                input_text="# Horse Notes\n\n- Saddle fit\n- Groundwork",
                expected_contains=["Saddle fit"],
            )
        ]
    )

    result = await run_eval_suite(container, suite)

    assert not result.passed
    assert "missing-markdown-list" in result.cases[0].warnings
    assert any("unexpected output warnings" in failure for failure in result.cases[0].failures)
    rendered = json.loads(eval_result_json(result))
    assert rendered["summary"]["warning_count"] == len(result.cases[0].warnings)
    assert rendered["summary"]["failure_count"] == len(result.cases[0].failures)
    assert rendered["summary"]["warning_case_count"] == 1
    assert rendered["summary"]["failure_case_count"] == 1


@pytest.mark.asyncio
async def test_eval_suite_fails_on_forbidden_text(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    suite = EvalSuite(
        cases=[
            EvalCase(
                name="forbidden phrase",
                input_text="Horse transcript with assistant artifact.",
                forbidden_contains=["assistant artifact"],
            )
        ]
    )

    result = await run_eval_suite(container, suite)

    assert not result.passed
    assert any(
        "found forbidden text: assistant artifact" in item
        for item in result.cases[0].failures
    )


@pytest.mark.asyncio
async def test_eval_suite_fails_on_output_char_ratio_bounds(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)
    suite = EvalSuite(
        cases=[
            EvalCase(
                name="ratio too narrow",
                input_text="Horse transcript with detailed saddle fit notes.",
                min_output_char_ratio=1.5,
                max_output_char_ratio=1.6,
            )
        ]
    )

    result = await run_eval_suite(container, suite)

    assert not result.passed
    assert result.cases[0].output_char_ratio > 0
    assert any("output character ratio" in failure for failure in result.cases[0].failures)


def test_load_eval_suite(tmp_path: Path) -> None:
    path = tmp_path / "eval.json"
    path.write_text(
        """
        {
          "cases": [
            {
              "name": "sample",
              "input_text": "Library science notes",
              "expected_contains": ["science"]
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    suite = load_eval_suite(path)

    assert suite.cases[0].name == "sample"


def test_load_eval_suite_rejects_empty_cases(tmp_path: Path) -> None:
    path = tmp_path / "eval.json"
    path.write_text('{"cases": []}', encoding="utf-8")

    with pytest.raises(ValueError):
        load_eval_suite(path)


def test_shipped_eval_suite_covers_v2_prompt_risks() -> None:
    suite = load_eval_suite(EXAMPLES_DIR / "eval_cases.json")
    tags = {tag for case in suite.cases for tag in case.tags}

    assert len(suite.cases) >= 6
    assert {
        "classification",
        "transcript",
        "legal",
        "technical",
        "no-summarization",
        "markdown",
        "structure",
        "ocr-correction",
    } <= tags
    assert any(
        case.forbidden_contains
        and {"Sadd1e", "transit10ns", "cust0dy"} <= set(case.forbidden_contains)
        for case in suite.cases
        if "ocr-correction" in case.tags
    )
    assert any(case.expected_classification_prefix == "636" for case in suite.cases)
    assert any(case.expected_classification_prefix == "610" for case in suite.cases)
    assert any(case.expected_classification_prefix == "800" for case in suite.cases)
    assert all(case.min_output_char_ratio is not None for case in suite.cases)
    assert all(case.max_output_char_ratio is not None for case in suite.cases)
    assert all(case.forbidden_contains for case in suite.cases)


def test_eval_case_requires_real_input() -> None:
    with pytest.raises(ValueError):
        EvalCase(name="", input_text="Horse notes")
    with pytest.raises(ValueError):
        EvalCase(name="horse notes", input_text="")
    with pytest.raises(ValueError):
        EvalCase(name="horse notes", input_text="Horse notes", min_output_chars=0)
    with pytest.raises(ValueError):
        EvalCase(name="horse notes", input_text="Horse notes", min_output_char_ratio=-1)
    with pytest.raises(ValueError):
        EvalCase(name="horse notes", input_text="Horse notes", max_output_char_ratio=-1)


def test_load_eval_suite_rejects_oversized_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "eval.json"
    path.write_text(" " * 8, encoding="utf-8")
    monkeypatch.setattr(eval_module, "_MAX_EVAL_SUITE_BYTES", 4)

    with pytest.raises(ValueError, match="Eval suite exceeds configured limit"):
        load_eval_suite(path)
