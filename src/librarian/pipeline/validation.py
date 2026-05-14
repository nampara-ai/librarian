"""Output validation and LLM artifact filtering."""

from __future__ import annotations

import re
from dataclasses import dataclass

ARTIFACT_PATTERNS = [
    r"I am ready to (?:begin|process)",
    r"I'm ready to (?:begin|process|edit)",
    r"Please provide (?:the|your) (?:raw )?transcript",
    r"\[The assistant(?:'s response)? (?:would|will|is)",
    r"\[Omitted long matching line\]",
    r"(?:Here is|I have) (?:the )?cleaned (?:the )?(?:text|transcript)",
    r"As (?:an AI|a language model)",
    r"I don't have access to",
    r"I cannot (?:see|access|read)",
    r"(?:The )?(?:input|text) (?:provided )?(?:is|was) (?:empty|blank)",
    r"there is no (?:text|content|input) to",
]

ARTIFACT_REGEX = re.compile("|".join(ARTIFACT_PATTERNS), re.IGNORECASE)
CONTEXT_MARKER_REGEX = re.compile(
    r"\[(?:CONTEXT:|CONTINUING FROM:|previous context|next context)",
    re.IGNORECASE,
)
MARKDOWN_HEADING_REGEX = re.compile(r"(?m)^#{1,6}\s+\S")
MARKDOWN_LIST_REGEX = re.compile(r"(?m)^\s*(?:[-*+]\s+|\d+[.)]\s+)\S")
ORPHAN_LIST_MARKER_REGEX = re.compile(r"(?m)^\s*(?:[-*+]|\d+[.)])\s*$")
MARKDOWN_TABLE_SEPARATOR_REGEX = re.compile(
    r"(?m)^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
MARKDOWN_TABLE_ROW_REGEX = re.compile(r"(?m)^\s*\|.+\|\s*$")
CITATION_MARKER_REGEX = re.compile(
    r"(?:\[[0-9A-Za-z][0-9A-Za-z .:-]{0,20}\]|\([A-Z][A-Za-z-]+,\s*\d{4}\))"
)
REPEATED_TAIL_MIN_PATTERN_CHARS = 12
REPEATED_TAIL_MAX_PATTERN_CHARS = 240
REPEATED_TAIL_MIN_REPEATS = 8


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Validated text and warnings."""

    text: str
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())


def validate_cleaned_text(
    result: str,
    *,
    input_size: int,
    source_text: str | None = None,
) -> ValidationResult:
    """Remove obvious LLM artifacts and report suspicious output."""
    warnings: list[str] = []
    text = result.strip()

    if not text:
        return ValidationResult(text="", warnings=("empty-output",))

    if ARTIFACT_REGEX.search(text):
        warnings.append("artifact-filtered")
        lines = [line for line in text.splitlines() if not ARTIFACT_REGEX.search(line)]
        text = "\n".join(lines).strip()

    if not text:
        warnings.append("empty-after-artifact-filter")
        return ValidationResult(text="", warnings=tuple(warnings))

    if input_size > 100 and len(text) < input_size * 0.2:
        warnings.append("suspiciously-short-output")

    text = _normalize_output_whitespace(text)
    warnings.extend(_markdown_quality_warnings(text, source_text=source_text))
    return ValidationResult(text=text, warnings=tuple(warnings))


def _normalize_output_whitespace(text: str) -> str:
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return text.strip()


def _markdown_quality_warnings(text: str, *, source_text: str | None) -> list[str]:
    warnings: list[str] = []
    if CONTEXT_MARKER_REGEX.search(text):
        warnings.append("context-marker-leak")
    if ORPHAN_LIST_MARKER_REGEX.search(text):
        warnings.append("orphan-list-marker")
    table_rows = MARKDOWN_TABLE_ROW_REGEX.findall(text)
    if len(table_rows) >= 2 and MARKDOWN_TABLE_SEPARATOR_REGEX.search(text) is None:
        warnings.append("malformed-markdown-table")
    if _has_repeated_tail(text):
        warnings.append("repeated-tail")
    if source_text is None:
        return warnings
    if _paragraph_count(source_text) >= 3 and _paragraph_count(text) <= 1 and len(text) > 500:
        warnings.append("collapsed-paragraphs")
    if MARKDOWN_HEADING_REGEX.search(source_text) and MARKDOWN_HEADING_REGEX.search(text) is None:
        warnings.append("missing-markdown-heading")
    if MARKDOWN_LIST_REGEX.search(source_text) and MARKDOWN_LIST_REGEX.search(text) is None:
        warnings.append("missing-markdown-list")
    if _has_markdown_table(source_text) and not _has_markdown_table(text):
        warnings.append("missing-markdown-table")
    if CITATION_MARKER_REGEX.search(source_text) and CITATION_MARKER_REGEX.search(text) is None:
        warnings.append("missing-citation-marker")
    return warnings


def _paragraph_count(text: str) -> int:
    return sum(1 for paragraph in re.split(r"\n\s*\n", text.strip()) if paragraph.strip())


def _has_markdown_table(text: str) -> bool:
    return (
        len(MARKDOWN_TABLE_ROW_REGEX.findall(text)) >= 2
        and MARKDOWN_TABLE_SEPARATOR_REGEX.search(text) is not None
    )


def _has_repeated_tail(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text.strip())
    max_pattern_length = min(
        REPEATED_TAIL_MAX_PATTERN_CHARS,
        len(normalized) // REPEATED_TAIL_MIN_REPEATS,
    )
    if max_pattern_length < REPEATED_TAIL_MIN_PATTERN_CHARS:
        return False

    for pattern_length in range(REPEATED_TAIL_MIN_PATTERN_CHARS, max_pattern_length + 1):
        pattern = normalized[-pattern_length:]
        if len(set(re.findall(r"\w", pattern))) < 2:
            continue
        repeats = 0
        position = len(normalized)
        while position >= pattern_length:
            if normalized[position - pattern_length : position] != pattern:
                break
            repeats += 1
            position -= pattern_length
        if repeats >= REPEATED_TAIL_MIN_REPEATS:
            return True
    return False
