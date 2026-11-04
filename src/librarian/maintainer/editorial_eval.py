"""Independent editorial assessment with evidence checks and explicit abstentions."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from librarian.application.ports import LLMProvider
from librarian.pipeline.validation import (
    FIDELITY_WARNINGS,
    UNCLEAR_TRANSCRIPTION_REGEX,
    validate_cleaned_text,
)
from librarian.prompts import PromptCatalog

Criterion = Literal["fidelity", "completeness", "fluency", "voice", "style", "structure"]
Status = Literal["pass", "fail", "uncertain"]
CRITERIA: frozenset[Criterion] = frozenset(
    {"fidelity", "completeness", "fluency", "voice", "style", "structure"}
)


class EditorialCase(BaseModel):
    """An oracle kept separate from everything given to the editor."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    reference: str | None = Field(default=None, min_length=1)
    requirements: list[str] = Field(default_factory=list)
    exact_fragments: list[str] = Field(default_factory=list)


class RequirementVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    status: Status


class EditorialIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion: str
    source_quote: str | None
    candidate_quote: str | None
    explanation: str = Field(min_length=1)


class EditorialJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criteria: dict[Criterion, Status]
    requirements: list[RequirementVerdict]
    issues: list[EditorialIssue]

    @model_validator(mode="after")
    def complete_criteria(self) -> Self:
        if frozenset(self.criteria) != CRITERIA:
            raise ValueError("Every editorial criterion must be assessed")
        return self


class EditorialAssessment(BaseModel):
    """A passing judgment also needs valid evidence and deterministic data checks."""

    case_id: str
    source_sha256: str
    candidate_sha256: str
    judge_model: str
    judge_prompt_sha256: str
    judgment: EditorialJudgment
    assessment_errors: list[str]
    protected_failures: list[str]
    unresolved_readings: int

    @property
    def passed(self) -> bool:
        return (
            not self.assessment_errors
            and not self.protected_failures
            and not self.unresolved_readings
            and all(value == "pass" for value in self.judgment.criteria.values())
            and all(item.status == "pass" for item in self.judgment.requirements)
        )


async def assess_editorial_output(
    provider: LLMProvider,
    catalog: PromptCatalog,
    model: str,
    case: EditorialCase,
    candidate: str,
    *,
    max_tokens: int = 8192,
    prompt_version: str = "editorial_v2",
) -> EditorialAssessment:
    """The judge sees no editor identity, prompt, scores, or prior approvals."""
    prompt = catalog.get("evaluation", prompt_version)
    raw = await provider.complete(
        system_prompt=prompt,
        user_prompt=json.dumps(
            {
                "source": case.source,
                "reference": case.reference,
                "candidate": candidate,
                "requirements": [
                    {"id": f"r{index}", "text": text}
                    for index, text in enumerate(case.requirements)
                ],
            },
            ensure_ascii=False,
        ),
        model=model,
        max_tokens=max_tokens,
        temperature=0.0,
    )
    return validate_assessment(case, candidate, raw, model=model, prompt=prompt)


def validate_assessment(
    case: EditorialCase, candidate: str, raw: str, *, model: str, prompt: str
) -> EditorialAssessment:
    if len(raw) > 128_000:
        raise ValueError("Editorial judgment exceeded its response limit")
    judgment = EditorialJudgment.model_validate_json(raw)
    errors: list[str] = []
    expected_ids = [f"r{index}" for index in range(len(case.requirements))]
    if [item.id for item in judgment.requirements] != expected_ids:
        errors.append("requirements-missing-duplicated-or-reordered")
    allowed = CRITERIA | set(expected_ids)
    reported: set[str] = set()
    for issue in judgment.issues:
        if issue.criterion not in allowed:
            errors.append("unknown-criterion")
        if not issue.source_quote and not issue.candidate_quote:
            errors.append("issue-without-evidence")
        if issue.source_quote and not (
            issue.source_quote in case.source
            or (case.reference is not None and issue.source_quote in case.reference)
        ):
            errors.append("invented-source-evidence")
        if issue.candidate_quote and issue.candidate_quote not in candidate:
            errors.append("invented-candidate-evidence")
        reported.add(issue.criterion)
        # Contradictory "all pass" JSON must never silently hide a reported discrepancy.
        if any(
            name == issue.criterion and status == "pass"
            for name, status in judgment.criteria.items()
        ):
            errors.append("issue-contradicts-passing-criterion")
        if any(
            item.id == issue.criterion and item.status == "pass" for item in judgment.requirements
        ):
            errors.append("issue-contradicts-passing-requirement")
    required_issues = {name for name, status in judgment.criteria.items() if status != "pass"} | {
        item.id for item in judgment.requirements if item.status != "pass"
    }
    if required_issues - reported:
        errors.append("failed-check-without-evidence")
    validated = validate_cleaned_text(
        candidate, input_size=len(case.source), source_text=case.reference or case.source
    )
    advisory = {
        "substantial-content-loss",
        "suspiciously-short-output",
        "collapsed-paragraphs",
        "repeated-tail",
    }
    protected = [w for w in validated.warnings if w in FIDELITY_WARNINGS - advisory]
    if any(fragment not in candidate for fragment in case.exact_fragments):
        protected.append("missing-exact-fragment")
    return EditorialAssessment(
        case_id=case.id,
        source_sha256=hashlib.sha256(case.source.encode()).hexdigest(),
        candidate_sha256=hashlib.sha256(candidate.encode()).hexdigest(),
        judge_model=model,
        judge_prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
        judgment=judgment,
        assessment_errors=sorted(set(errors)),
        protected_failures=sorted(set(protected)),
        unresolved_readings=len(UNCLEAR_TRANSCRIPTION_REGEX.findall(candidate)),
    )
