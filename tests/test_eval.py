from pathlib import Path

import pytest

from librarian.application.eval import (
    EvalCase,
    EvalSuite,
    eval_result_json,
    load_eval_suite,
    run_eval_suite,
)
from librarian.application.factory import build_container
from librarian.config import Settings


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
    assert '"tags": [' in eval_result_json(result)


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
