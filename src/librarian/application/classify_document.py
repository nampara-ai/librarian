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
    description: str | None = None
    issuer: str | None = None
    series: str | None = None
    period: str | None = None


_MAX_TAGS = 8
_MAX_TAG_CHARS = 60
_MAX_TITLE_CHARS = 120
_MAX_DESCRIPTION_CHARS = 280
_MAX_ISSUER_CHARS = 80
_MAX_SERIES_TITLE_CHARS = 120
_MAX_PERIOD_CHARS = 24
_MAX_SERIES_KEY_CHARS = 80

# ISO-style date stamps (``2026``, ``2026-06``, ``2026_06_15``, ``202606``,
# ``2026-Q2``) stripped from a series identity first, so that filename-derived
# keys like ``cbre-..._2026-06`` and ``cbre-..._2026-07`` collapse together. Digit
# lookarounds are used instead of ``\b`` because ``_`` is a word character, so a
# ``\b`` would not fire at the ``_2026`` boundary common in download filenames.
_DATE_STAMP = re.compile(
    r"(?<![0-9])(?:19|20)\d{2}"
    r"(?:[-_./]?(?:q[1-4]|h[12]|0[1-9]|1[0-2]))?"
    r"(?:[-_./]?(?:0[1-9]|[12]\d|3[01]))?"
    r"(?![0-9])",
    re.IGNORECASE,
)

# Named periods stripped from a series identity so that editions of one recurring
# publication ("CBRE MarketView May 2026" vs "... June 2026") collapse to the same
# ``series_key``. Month names (full or abbreviated), bare quarters, halves, and
# fiscal years are removed. Month names are listed explicitly and word-bounded so
# they match only whole tokens — "March" is stripped, "MarketView" is not.
_PERIOD_TOKENS = re.compile(
    r"\b(?:"
    r"q[1-4]|h[12]|fy\s*\d{2,4}"
    r"|jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
    r")\b",
    re.IGNORECASE,
)

# Month abbreviation -> two-digit number, used to canonicalize a reporting period
# into an orderable ``YYYY-MM`` token when the model returns a month name.
_MONTH_NUMBERS = {
    "jan": "01",
    "feb": "02",
    "mar": "03",
    "apr": "04",
    "may": "05",
    "jun": "06",
    "jul": "07",
    "aug": "08",
    "sep": "09",
    "sept": "09",
    "oct": "10",
    "nov": "11",
    "dec": "12",
}

# Generic filename stems that must never seed a series identity on their own;
# repeatedly downloading "report.pdf" should not merge unrelated documents.
_GENERIC_SERIES_STEMS = frozenset(
    {
        "document",
        "documents",
        "report",
        "reports",
        "scan",
        "scanned",
        "file",
        "files",
        "download",
        "downloads",
        "untitled",
        "copy",
        "final",
        "draft",
        "new",
    }
)


def _classification_sample(text: str, *, budget: int) -> str:
    """Return a representative excerpt of the document for classification.

    Short documents are used whole. For longer ones, sampling only the head
    means a title page or table of contents decides the classification; taking
    head + middle + tail gives the model a view of the actual body too, within
    the same character budget.
    """
    if budget <= 0 or len(text) <= budget:
        return text
    segment = max(1, budget // 3)
    head = text[:segment]
    middle_start = max(segment, (len(text) - segment) // 2)
    middle = text[middle_start : middle_start + segment]
    tail = text[len(text) - segment :]
    return "\n[...]\n".join((head, middle, tail))


def _clean_one_line(value: str | None, *, max_chars: int) -> str | None:
    if value is None:
        return None
    collapsed = " ".join(value.split())
    return collapsed[:max_chars].strip() or None


def _normalize_period(value: str | None) -> str | None:
    """Canonicalize a reporting period into an orderable token.

    Returns ``YYYY-MM`` / ``YYYY-Qn`` / ``YYYY-Hn`` / ``YYYY`` when the input is
    recognizable, otherwise the cleaned free-text value. Lexicographic ordering
    of these tokens matches chronological order within a series.
    """
    collapsed = _clean_one_line(value, max_chars=_MAX_PERIOD_CHARS)
    if collapsed is None:
        return None
    iso = re.fullmatch(r"((?:19|20)\d{2})-(0[1-9]|1[0-2])", collapsed)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}"
    lowered = collapsed.lower()
    year_match = re.search(r"(?:19|20)\d{2}", lowered)
    year = year_match.group(0) if year_match else None
    if year is None:
        return collapsed
    quarter = re.search(r"q([1-4])", lowered)
    if quarter:
        return f"{year}-Q{quarter.group(1)}"
    half = re.search(r"h([12])", lowered)
    if half:
        return f"{year}-H{half.group(1)}"
    for name, number in _MONTH_NUMBERS.items():
        if re.search(rf"\b{name}", lowered):
            return f"{year}-{number}"
    return year


def _series_slug(raw: str) -> str:
    without_dates = _DATE_STAMP.sub(" ", raw)
    without_period = _PERIOD_TOKENS.sub(" ", without_dates)
    slug = re.sub(r"[^a-z0-9]+", "-", without_period.lower()).strip("-")
    return slug[:_MAX_SERIES_KEY_CHARS].strip("-")


def _is_meaningful_series_slug(slug: str) -> bool:
    tokens = [token for token in slug.split("-") if token]
    if len(tokens) < 2 or len(slug) < 6:
        return False
    return not all(token in _GENERIC_SERIES_STEMS for token in tokens)


def _series_key(
    issuer: str | None,
    series_title: str | None,
    *,
    fallback_filename: str | None,
) -> str | None:
    """Derive a stable identity for a recurring publication.

    Built from issuer + series name when the model supplies them; otherwise
    falls back to a period-stripped filename stem, but only when that stem is
    distinctive enough to be a real series rather than a generic name.
    """
    # A series identity requires an actual recurring-publication *title*. An
    # issuer alone is not a series (otherwise every one-off document from the
    # same publisher would be falsely linked as editions of one series).
    if series_title and series_title.strip():
        primary = " ".join(part for part in (issuer, series_title) if part and part.strip())
        return _series_slug(primary) or None
    if fallback_filename:
        stem = (
            fallback_filename.rsplit(".", 1)[0]
            if "." in fallback_filename
            else fallback_filename
        )
        slug = _series_slug(stem)
        if _is_meaningful_series_slug(slug):
            return slug
    return None


def _clean_title(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = " ".join(value.split())
    return collapsed[:_MAX_TITLE_CHARS].strip() or None


def _clean_description(value: str | None) -> str | None:
    if value is None:
        return None
    collapsed = " ".join(value.split())
    return collapsed[:_MAX_DESCRIPTION_CHARS].strip() or None


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
    max_tokens: int = 2_048
    temperature: float = 0.0
    max_response_chars: int = 2 * 1024 * 1024
    sample_chars: int = 8_000

    async def execute(
        self,
        document_id: DocumentId,
        text: str,
        *,
        source_filename: str | None = None,
    ) -> Classification:
        prompt = self.prompt_catalog.get("classification", self.prompt_version)
        sample = _classification_sample(text, budget=self.sample_chars)
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
        issuer = _clean_one_line(payload.issuer, max_chars=_MAX_ISSUER_CHARS)
        series_title = _clean_one_line(payload.series, max_chars=_MAX_SERIES_TITLE_CHARS)
        period = _normalize_period(payload.period)
        series_key = _series_key(issuer, series_title, fallback_filename=source_filename)
        return Classification(
            document_id=document_id,
            code=code,
            label=label,
            summary=payload.summary.strip() or "No summary available.",
            taxonomy=self.taxonomy.name,
            confidence=payload.confidence,
            title=_clean_title(payload.title),
            tags=_clean_tags(payload.tags),
            description=_clean_description(payload.description),
            issuer=issuer,
            series_key=series_key,
            series_title=series_title,
            period=period,
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
