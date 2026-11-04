import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from librarian.config import Settings
from librarian.maintainer.editorial_benchmark import (
    EditorialBenchmarkCase,
    EditorialBenchmarkSuite,
    run_editorial_benchmark,
)
from librarian.maintainer.editorial_eval import CRITERIA


@pytest.mark.asyncio
async def test_benchmark_trials_are_fresh_and_reference_is_not_in_generated_source(
    tmp_path: Path,
) -> None:
    class Judge:
        name = "test-judge"

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            data = json.loads(user_prompt)
            assert data["reference"] == "Independent oracle withheld from generation."
            assert "Independent oracle" not in data["candidate"]
            return json.dumps(
                {
                    "criteria": dict.fromkeys(CRITERIA, "pass"),
                    "requirements": [],
                    "issues": [],
                }
            )

    suite = EditorialBenchmarkSuite(
        cases=[
            EditorialBenchmarkCase(
                id="../../untrusted-id",
                input_text="A complete and intact source sentence.",
                reference="Independent oracle withheld from generation.",
            )
        ]
    )
    result = await run_editorial_benchmark(
        Settings(),
        suite,
        Judge(),
        "test",
        tmp_path / "bench",
        repeats=2,
    )
    assert result.passed
    assert result.evidence_tier == "mock-smoke"
    assert len(result.trials) == 2
    assert len({row.artifact_dir for row in result.trials}) == 2
    assert all(row.cached_cleaned_chunks == 0 for row in result.trials)
    assert all(Path(row.artifact_dir).is_relative_to(tmp_path / "bench") for row in result.trials)
    report = json.loads((Path(result.artifact_dir) / "result.json").read_text())
    assert report["summary"][0]["total_cases"] == 1
    assert report["summary"][1]["total_cases"] == 1


def test_benchmark_rejects_duplicate_case_ids() -> None:
    case = EditorialBenchmarkCase(id="same", input_text="A source.")
    with pytest.raises(ValidationError, match="unique"):
        EditorialBenchmarkSuite(cases=[case, case])
