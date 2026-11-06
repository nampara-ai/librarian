"""An advisory reading of source, kept separate from the subsequent fidelity review."""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from librarian.application.errors import AdvisoryModelOutputError, LLMResponseTruncatedError
from librarian.application.model_output import complete_json
from librarian.application.ports import LLMProvider
from librarian.prompts import PromptCatalog


class SourceReading(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[str] = Field(max_length=100)
    qualifiers: list[str] = Field(max_length=100)
    rhetoric: list[str] = Field(max_length=100)
    voice: list[str] = Field(max_length=100)
    discarded_false_starts: list[str] = Field(max_length=100)


async def read_source(
    provider: LLMProvider,
    catalog: PromptCatalog,
    model: str,
    source: str,
    before: str,
    after: str,
    *,
    max_tokens: int,
    temperature: float,
    prompt_version: str = "source_reading_v1",
) -> SourceReading:
    payload: dict[str, object] = {
        "source": source,
        "context_before": before,
        "context_after": after,
    }
    for attempt in range(2):
        try:
            raw = await complete_json(
                provider,
                system_prompt=catalog.get("repair", prompt_version),
                user_prompt=json.dumps(payload, ensure_ascii=False),
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except LLMResponseTruncatedError:
            raise AdvisoryModelOutputError("Source reading was truncated") from None
        if len(raw) > 128_000:
            raise AdvisoryModelOutputError("Source reading exceeded its response limit")
        try:
            return SourceReading.model_validate_json(raw)
        except ValidationError:
            if attempt:
                raise AdvisoryModelOutputError(
                    "Source reading did not satisfy the required schema"
                ) from None
            payload["validation_error"] = "Return exactly the five required arrays of strings."
            payload["previous_reading"] = raw
    raise AssertionError("Source reading retry budget must terminate")
