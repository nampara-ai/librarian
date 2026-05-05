"""Lightweight model/provider evaluation harness."""

from __future__ import annotations

import json
import time
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
    tags: list[str] = Field(default_factory=list)
    expected_contains: list[str] = Field(default_factory=list)
    expected_classification_prefix: str | None = None
    min_output_chars: int = 1


class EvalSuite(BaseModel):
    """Serializable evaluation suite."""

    cases: list[EvalCase]


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    """Result for one evaluation case."""

    name: str
    passed: bool
    tags: tuple[str, ...]
    input_chars: int
    output_chars: int
    duration_seconds: float
    chars_per_second: float
    classification_code: str
    classification_label: str
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvalRunResult:
    """Aggregate evaluation result."""

    cases: tuple[EvalCaseResult, ...]
    provider: str
    model: str
    total_seconds: float

    @property
    def passed(self) -> bool:
        """Return true when every case passed."""
        return all(item.passed for item in self.cases)


def load_eval_suite(path: Path) -> EvalSuite:
    """Load an evaluation suite from JSON."""
    return EvalSuite.model_validate_json(path.read_text(encoding="utf-8"))


async def run_eval_suite(container: ApplicationContainer, suite: EvalSuite) -> EvalRunResult:
    """Run a suite against the configured chunking, prompt, and provider stack."""
    start = time.perf_counter()
    results: list[EvalCaseResult] = []
    for case in suite.cases:
        case_start = time.perf_counter()
        document_id = DocumentId(digest_text("eval", case.name))
        chunks = chunk_text(
            document_id,
            case.input_text,
            container.process_document.chunking_policy,
        )
        cleaned_chunks = await container.process_document.cleaner.execute(chunks)
        assembled = assemble_cleaned_document(cleaned_chunks)
        classification = await container.process_document.classifier.execute(document_id, assembled)
        duration_seconds = max(time.perf_counter() - case_start, 1e-9)

        failures: list[str] = []
        lower_output = assembled.lower()
        if len(assembled) < case.min_output_chars:
            failures.append(
                f"output has {len(assembled)} chars, expected at least {case.min_output_chars}"
            )
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
                tags=tuple(case.tags),
                input_chars=len(case.input_text),
                output_chars=len(assembled),
                duration_seconds=duration_seconds,
                chars_per_second=len(case.input_text) / duration_seconds,
                classification_code=classification.code,
                classification_label=classification.label,
                failures=tuple(failures),
            )
        )
    return EvalRunResult(
        cases=tuple(results),
        provider=container.process_document.cleaner.provider.name,
        model=container.process_document.cleaner.model,
        total_seconds=time.perf_counter() - start,
    )


def eval_result_json(result: EvalRunResult) -> str:
    """Render an evaluation result as JSON."""
    return json.dumps(
        {
            "passed": result.passed,
            "provider": result.provider,
            "model": result.model,
            "total_seconds": result.total_seconds,
            "cases": [
                {
                    "name": item.name,
                    "passed": item.passed,
                    "tags": list(item.tags),
                    "input_chars": item.input_chars,
                    "output_chars": item.output_chars,
                    "duration_seconds": item.duration_seconds,
                    "chars_per_second": item.chars_per_second,
                    "classification_code": item.classification_code,
                    "classification_label": item.classification_label,
                    "failures": list(item.failures),
                }
                for item in result.cases
            ],
        },
        indent=2,
    )
