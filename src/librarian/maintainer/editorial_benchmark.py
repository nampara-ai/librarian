"""Fresh, end-to-end editorial trials; reference material is withheld from generation."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, model_validator

from librarian.application.errors import LLMAccessError
from librarian.application.factory import build_container
from librarian.application.ports import LLMProvider
from librarian.application.quality_report import run_quality_key
from librarian.config import Settings
from librarian.maintainer.editorial_eval import (
    EditorialAssessment,
    EditorialCase,
    assess_editorial_output,
)
from librarian.observability import MetricsRecorder, sanitize_error_message
from librarian.prompts import PromptCatalog


class EditorialBenchmarkCase(BaseModel):
    id: str = Field(min_length=1)
    input_text: str = Field(min_length=1)
    reference: str | None = Field(default=None, min_length=1)
    requirements: list[str] = Field(default_factory=list)
    exact_fragments: list[str] = Field(default_factory=list)


class EditorialBenchmarkSuite(BaseModel):
    cases: list[EditorialBenchmarkCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_case_ids(self) -> Self:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("Editorial benchmark case IDs must be unique")
        return self


class EditorialTrial(BaseModel):
    case_id: str
    trial: int
    passed: bool = False
    processing_succeeded: bool = False
    cached_cleaned_chunks: int = 0
    elapsed_seconds: float = 0.0
    error: str | None = None
    error_kind: str | None = None
    assessment: EditorialAssessment | None = None
    artifact_dir: str


class EditorialBenchmarkResult(BaseModel):
    artifact_type: str = "librarian-editorial-benchmark"
    evidence_tier: str
    suite_sha256: str
    provenance: dict[str, str] = Field(default_factory=dict)
    settings: dict[str, object]
    judge_model: str
    repeats: int
    planned_trials: int
    stopped_reason: str | None = None
    trials: list[EditorialTrial]
    artifact_dir: str

    @property
    def passed(self) -> bool:
        return (
            len(self.trials) == self.planned_trials
            and not self.stopped_reason
            and all(trial.passed for trial in self.trials)
        )

    def report(self) -> dict[str, object]:
        rows = self.model_dump(mode="json")
        rows["passed"] = self.passed
        rows["complete"] = len(self.trials) == self.planned_trials and not self.stopped_reason
        rows["summary"] = [
            {
                "trial": index,
                "passed_cases": sum(row.passed for row in self.trials if row.trial == index),
                "total_cases": sum(row.trial == index for row in self.trials),
                "planned_cases": self.planned_trials // self.repeats,
                "unattempted_cases": self.planned_trials // self.repeats
                - sum(row.trial == index for row in self.trials),
                "processing_failures": sum(
                    not row.processing_succeeded for row in self.trials if row.trial == index
                ),
            }
            for index in range(1, self.repeats + 1)
        ]
        return rows


async def run_editorial_benchmark(
    settings: Settings,
    suite: EditorialBenchmarkSuite,
    judge: LLMProvider,
    judge_model: str,
    output_dir: Path,
    *,
    repeats: int = 3,
    provenance: dict[str, str] | None = None,
) -> EditorialBenchmarkResult:
    if not 1 <= repeats <= 20:
        raise ValueError("Editorial benchmark repeats must be between one and twenty")
    run_name = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    root = output_dir / run_name
    root.mkdir(parents=True, mode=0o700)
    rows: list[EditorialTrial] = []
    stopped_reason: str | None = None
    serialized = suite.model_dump_json()
    for trial in range(1, repeats + 1):
        for case in suite.cases:
            digest = hashlib.sha256(case.id.encode()).hexdigest()[:16]
            folder = root / f"trial-{trial}" / digest
            folder.mkdir(parents=True)
            row = EditorialTrial(case_id=case.id, trial=trial, artifact_dir=str(folder))
            started = time.monotonic()
            metrics = MetricsRecorder()
            try:
                isolated = settings.model_copy(
                    update={
                        "data_dir": folder / "state",
                        "database_path": folder / "state/run.sqlite",
                    }
                )
                container = await build_container(isolated, metrics=metrics)
                source = folder / "source.md"
                source.write_text(case.input_text, encoding="utf-8")
                ingested = await container.ingest_document.execute(source)
                run = await container.process_document.execute(ingested.document.id)
                output = await container.repository.get_cleaned_output(ingested.document.id)
                if output is None or run.status.value != "succeeded":
                    raise RuntimeError("Processing did not produce a successful cleaned output")
                row.processing_succeeded = True
                quality = json.loads(await container.repository.get_text(run_quality_key(run.id)))
                cached: object = quality.get("cached_cleaned_chunks", 0)
                if not isinstance(cached, int) or cached != 0:
                    raise RuntimeError("A fresh editorial trial unexpectedly reused cached output")
                row.cached_cleaned_chunks = cached
                (folder / "candidate.md").write_text(output.text, encoding="utf-8")
                (folder / "quality.json").write_text(
                    json.dumps(quality, indent=2), encoding="utf-8"
                )
                # The independently preserved reference is introduced only after generation.
                oracle = EditorialCase(
                    id=case.id,
                    source=ingested.raw_text,
                    reference=case.reference,
                    requirements=case.requirements,
                    exact_fragments=case.exact_fragments,
                )
                row.assessment = await assess_editorial_output(
                    judge,
                    PromptCatalog(),
                    judge_model,
                    oracle,
                    output.text,
                    max_tokens=16_384,
                )
                row.passed = row.assessment.passed
            except LLMAccessError as exc:
                row.error = sanitize_error_message(exc)
                row.error_kind = "provider-access"
                stopped_reason = exc.reason
            except Exception as exc:  # noqa: BLE001 - every failed trial remains in the denominator
                row.error = sanitize_error_message(exc)
                row.error_kind = "assessment" if row.processing_succeeded else "pipeline"
            row.elapsed_seconds = time.monotonic() - started
            (folder / "trial.json").write_text(row.model_dump_json(indent=2), encoding="utf-8")
            (folder / "metrics.json").write_text(
                json.dumps(metrics.snapshot(), indent=2), encoding="utf-8"
            )
            rows.append(row)
            # Incremental persistence also retains completed work if the benchmark is interrupted.
            (root / "trials.json").write_text(
                json.dumps([item.model_dump(mode="json") for item in rows], indent=2),
                encoding="utf-8",
            )
            if stopped_reason:
                break
        if stopped_reason:
            break
    result = EditorialBenchmarkResult(
        evidence_tier="mock-smoke" if settings.llm_provider == "mock" else "real-provider",
        suite_sha256=hashlib.sha256(serialized.encode()).hexdigest(),
        provenance=provenance or {},
        settings=settings.redacted_config(),
        judge_model=judge_model,
        repeats=repeats,
        planned_trials=len(suite.cases) * repeats,
        stopped_reason=stopped_reason,
        trials=rows,
        artifact_dir=str(root),
    )
    (root / "result.json").write_text(json.dumps(result.report(), indent=2), encoding="utf-8")
    return result
