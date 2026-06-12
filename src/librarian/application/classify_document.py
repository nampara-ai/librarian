"""Application service for structured document classification."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from librarian.application.ports import LLMProvider, TaxonomyProvider
from librarian.domain.ids import DocumentId
from librarian.domain.models import Classification
from librarian.prompts import PromptCatalog


class ClassificationPayload(BaseModel):
    """Structured LLM classification payload.

    ``title`` and ``tags`` arrived with the dewey_v3 prompt; they default so
    dewey_v1/v2 responses keep parsing and a provider that omits them never
    fails validation.
    """

    summary: str
    dewey_code: str
    category_name: str
    confidence: float | None = None
    title: str | None = None
    tags: list[str] = []


_MAX_TAGS = 8
_MAX_TAG_CHARS = 60
_MAX_TITLE_CHARS = 120


def _clean_title(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = " ".join(value.split())
    return collapsed[:_MAX_TITLE_CHARS].strip() or None


def _clean_tags(values: list[str]) -> tuple[str, ...]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = " ".join(value.split()).strip().lower()[:_MAX_TAG_CHARS]
        if not tag or tag in seen:
            continue
        seen.add(tag)
        cleaned.append(tag)
        if len(cleaned) == _MAX_TAGS:
            break
    return tuple(cleaned)


@dataclass(frozen=True, slots=True)
class ClassifyDocument:
    """Classify a cleaned document through an LLM with deterministic fallback."""

    provider: LLMProvider
    prompt_catalog: PromptCatalog
    prompt_version: str
    model: str
    taxonomy: TaxonomyProvider
    max_tokens: int = 500
    temperature: float = 0.0
    max_response_chars: int = 2 * 1024 * 1024

    async def execute(self, document_id: DocumentId, text: str) -> Classification:
        prompt = self.prompt_catalog.get("classification", self.prompt_version)
        sample = text[:8_000]
        raw = await self.provider.complete(
            system_prompt=(
                "You are a librarian expert in Dewey Decimal Classification. "
                "Respond only with valid JSON."
            ),
            user_prompt=f"{prompt}\n\nText to analyze:\n{sample}",
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        if len(raw) > self.max_response_chars:
            raise ValueError(
                "classification provider response exceeded configured character limit "
                f"({len(raw)} > {self.max_response_chars})"
            )
        try:
            payload = _parse_payload(raw)
        except ValueError:
            return heuristic_classify(document_id, text, self.taxonomy)

        code = payload.dewey_code.strip() or "000"
        label = payload.category_name.strip() or self.taxonomy.label_for(code) or "General"
        return Classification(
            document_id=document_id,
            code=code,
            label=label,
            summary=payload.summary.strip() or "No summary available.",
            taxonomy=self.taxonomy.name,
            confidence=payload.confidence,
            title=_clean_title(payload.title),
            tags=_clean_tags(payload.tags),
        )


def heuristic_classify(
    document_id: DocumentId,
    text: str,
    taxonomy: TaxonomyProvider,
) -> Classification:
    """Fast deterministic fallback classifier."""
    lowered = text.lower()
    code = "000"
    if any(term in lowered for term in ("horse", "equine", "colt", "mare", "stallion")):
        code = "636.1"
    elif any(term in lowered for term in ("software", "programming", "computer", "algorithm")):
        code = "000"
    elif any(term in lowered for term in ("medicine", "health", "doctor", "disease")):
        code = "610"
    elif any(term in lowered for term in ("writing", "literature", "novel", "poetry")):
        code = "800"

    label = taxonomy.label_for(code) or "General"
    summary = text[:500].strip() or "No summary available."
    return Classification(document_id=document_id, code=code, label=label, summary=summary)


def _parse_payload(raw: str) -> ClassificationPayload:
    candidate = raw.strip()
    try:
        return ClassificationPayload.model_validate_json(candidate)
    except ValidationError:
        pass

    match = re.search(r"\{.*\}", candidate, re.DOTALL)
    if not match:
        raise ValueError("classification response did not contain JSON")

    try:
        decoded = json.loads(match.group(0))
        return ClassificationPayload.model_validate(decoded)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError("classification response was invalid") from exc
