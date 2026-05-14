"""Lightweight model/provider evaluation harness."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from librarian.application.assemble_document import assemble_cleaned_document
from librarian.application.factory import ApplicationContainer
from librarian.domain.ids import DocumentId, digest_text
from librarian.pipeline.chunking import chunk_text
from librarian.version import __version__

_MAX_EVAL_SUITE_BYTES = 10 * 1024 * 1024


class EvalCase(BaseModel):
    """One deterministic evaluation case."""

    name: str = Field(min_length=1)
    input_text: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    expected_contains: list[str] = Field(default_factory=list)
    forbidden_contains: list[str] = Field(default_factory=list)
    expected_classification_prefix: str | None = None
    min_output_chars: int = Field(default=1, ge=1)
    min_output_char_ratio: float | None = Field(default=None, ge=0)
    max_output_char_ratio: float | None = Field(default=None, ge=0)
    allowed_warnings: list[str] = Field(default_factory=list)


class EvalSuite(BaseModel):
    """Serializable evaluation suite."""

    cases: list[EvalCase] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    """Result for one evaluation case."""

    name: str
    passed: bool
    tags: tuple[str, ...]
    input_chars: int
    output_chars: int
    output_char_ratio: float
    duration_seconds: float
    chars_per_second: float
    classification_code: str
    classification_label: str
    warnings: tuple[str, ...]
    failures: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvalRunResult:
    """Aggregate evaluation result."""

    cases: tuple[EvalCaseResult, ...]
    provider: str
    model: str
    total_seconds: float
    generated_at: datetime
    librarian_version: str
    cleaning_prompt_version: str
    classification_prompt_version: str

    @property
    def passed(self) -> bool:
        """Return true when every case passed."""
        return all(item.passed for item in self.cases)


def load_eval_suite(path: Path) -> EvalSuite:
    """Load an evaluation suite from JSON."""
    return EvalSuite.model_validate_json(
        _read_limited_text_file(path, max_bytes=_MAX_EVAL_SUITE_BYTES)
    )


def _read_limited_text_file(path: Path, *, max_bytes: int) -> str:
    with path.open("rb") as handle:
        payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"Eval suite exceeds configured limit of {max_bytes} bytes: {path}")
    return payload.decode("utf-8")


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
        warnings = tuple(
            sorted({warning for chunk in cleaned_chunks for warning in chunk.warnings})
        )
        allowed_warnings = set(case.allowed_warnings)
        unexpected_warnings = [warning for warning in warnings if warning not in allowed_warnings]
        if unexpected_warnings:
            failures.append(f"unexpected output warnings: {', '.join(unexpected_warnings)}")
        lower_output = assembled.lower()
        if len(assembled) < case.min_output_chars:
            failures.append(
                f"output has {len(assembled)} chars, expected at least {case.min_output_chars}"
            )
        output_char_ratio = len(assembled) / len(case.input_text)
        if (
            case.min_output_char_ratio is not None
            and output_char_ratio < case.min_output_char_ratio
        ):
            failures.append(
                "output character ratio "
                f"{output_char_ratio:.3f} is below minimum {case.min_output_char_ratio:.3f}"
            )
        if (
            case.max_output_char_ratio is not None
            and output_char_ratio > case.max_output_char_ratio
        ):
            failures.append(
                "output character ratio "
                f"{output_char_ratio:.3f} is above maximum {case.max_output_char_ratio:.3f}"
            )
        for expected in case.expected_contains:
            if expected.lower() not in lower_output:
                failures.append(f"missing expected text: {expected}")
        for forbidden in case.forbidden_contains:
            if forbidden.lower() in lower_output:
                failures.append(f"found forbidden text: {forbidden}")
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
                output_char_ratio=output_char_ratio,
                duration_seconds=duration_seconds,
                chars_per_second=len(case.input_text) / duration_seconds,
                classification_code=classification.code,
                classification_label=classification.label,
                warnings=warnings,
                failures=tuple(failures),
            )
        )
    return EvalRunResult(
        cases=tuple(results),
        provider=container.process_document.cleaner.provider.name,
        model=container.process_document.cleaner.model,
        total_seconds=time.perf_counter() - start,
        generated_at=datetime.now(UTC),
        librarian_version=__version__,
        cleaning_prompt_version=container.settings.cleaning_prompt_version,
        classification_prompt_version=container.settings.classification_prompt_version,
    )


def eval_result_json(result: EvalRunResult) -> str:
    """Render an evaluation result as JSON."""
    return json.dumps(
        {
            "artifact_type": "librarian-eval-result",
            "evidence_tier": _evidence_tier(result.provider),
            "passed": result.passed,
            "provider": result.provider,
            "model": result.model,
            "total_seconds": result.total_seconds,
            "generated_at": result.generated_at.isoformat(),
            "librarian_version": result.librarian_version,
            "cleaning_prompt_version": result.cleaning_prompt_version,
            "classification_prompt_version": result.classification_prompt_version,
            "summary": _eval_summary(result),
            "cases": [
                {
                    "name": item.name,
                    "passed": item.passed,
                    "tags": list(item.tags),
                    "input_chars": item.input_chars,
                    "output_chars": item.output_chars,
                    "output_char_ratio": item.output_char_ratio,
                    "duration_seconds": item.duration_seconds,
                    "chars_per_second": item.chars_per_second,
                    "classification_code": item.classification_code,
                    "classification_label": item.classification_label,
                    "warnings": list(item.warnings),
                    "failures": list(item.failures),
                }
                for item in result.cases
            ],
        },
        indent=2,
    )


def _evidence_tier(provider: str) -> str:
    return "mock-smoke" if provider == "mock" else "real-provider"


def _eval_summary(result: EvalRunResult) -> dict[str, object]:
    cases = result.cases
    passed_count = sum(1 for item in cases if item.passed)
    return {
        "case_count": len(cases),
        "passed_count": passed_count,
        "failed_count": len(cases) - passed_count,
        "pass_rate": passed_count / len(cases) if cases else 0.0,
        "total_input_chars": sum(item.input_chars for item in cases),
        "total_output_chars": sum(item.output_chars for item in cases),
        "average_chars_per_second": (
            sum(item.chars_per_second for item in cases) / len(cases) if cases else 0.0
        ),
        "warning_count": sum(len(item.warnings) for item in cases),
        "failure_count": sum(len(item.failures) for item in cases),
        "warning_case_count": sum(1 for item in cases if item.warnings),
        "failure_case_count": sum(1 for item in cases if item.failures),
    }
