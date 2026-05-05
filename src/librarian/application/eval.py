"""Lightweight model/provider evaluation harness."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from librarian.application.assemble_document import assemble_cleaned_document
from librarian.application.factory import ApplicationContainer
from librarian.domain.ids import DocumentId, digest_text
from librarian.pipeline.chunking import chunk_text


class EvalCase(BaseModel):
    """One deterministic evaluation case."""

    name: str
    input_text: str
    expected_contains: list[str] = Field(default_factory=list)
    expected_classification_prefix: str | None = None


class EvalSuite(BaseModel):
    """Serializable evaluation suite."""

    cases: list[EvalCase]


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    """Result for one evaluation case."""

    name: str
    passed: bool
    output_chars: int
    classification_code: str
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvalRunResult:
    """Aggregate evaluation result."""

    cases: tuple[EvalCaseResult, ...]

    @property
    def passed(self) -> bool:
        """Return true when every case passed."""
        return all(item.passed for item in self.cases)


def load_eval_suite(path: Path) -> EvalSuite:
    """Load an evaluation suite from JSON."""
    return EvalSuite.model_validate_json(path.read_text(encoding="utf-8"))


async def run_eval_suite(container: ApplicationContainer, suite: EvalSuite) -> EvalRunResult:
    """Run a suite against the configured chunking, prompt, and provider stack."""
    results: list[EvalCaseResult] = []
    for case in suite.cases:
        document_id = DocumentId(digest_text("eval", case.name))
        chunks = chunk_text(
            document_id,
            case.input_text,
            container.process_document.chunking_policy,
        )
        cleaned_chunks = await container.process_document.cleaner.execute(chunks)
        assembled = assemble_cleaned_document(cleaned_chunks)
        classification = await container.process_document.classifier.execute(document_id, assembled)

        failures: list[str] = []
        lower_output = assembled.lower()
        for expected in case.expected_contains:
            if expected.lower() not in lower_output:
                failures.append(f"missing expected text: {expected}")
        if (
            case.expected_classification_prefix
            and not classification.code.startswith(case.expected_classification_prefix)
        ):
            failures.append(
                "classification "
                f"{classification.code} does not match {case.expected_classification_prefix}"
            )

        results.append(
            EvalCaseResult(
                name=case.name,
                passed=not failures,
                output_chars=len(assembled),
                classification_code=classification.code,
                failures=tuple(failures),
            )
        )
    return EvalRunResult(cases=tuple(results))


def eval_result_json(result: EvalRunResult) -> str:
    """Render an evaluation result as JSON."""
    return json.dumps(
        {
            "passed": result.passed,
            "cases": [
                {
                    "name": item.name,
                    "passed": item.passed,
                    "output_chars": item.output_chars,
                    "classification_code": item.classification_code,
                    "failures": list(item.failures),
                }
                for item in result.cases
            ],
        },
        indent=2,
    )
