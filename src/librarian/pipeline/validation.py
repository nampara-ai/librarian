"""Output validation and LLM artifact filtering."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from librarian.pipeline.markdown import (
    normalize_markdown_whitespace,
    protected_spans,
    transform_prose,
)

FIDELITY_WARNINGS = frozenset(
    {
        "added-verbatim-number",
        "collapsed-paragraphs",
        "context-marker-leak",
        "changed-markdown-images",
        "changed-page-breaks",
        "empty-after-artifact-filter",
        "empty-output",
        "malformed-markdown-table",
        "missing-citation-marker",
        "missing-markdown-heading",
        "missing-markdown-list",
        "missing-markdown-table",
        "missing-verbatim-number",
        "repeated-tail",
        "new-adjacent-repetition",
        "new-lowercase-paragraph",
        "substantial-content-loss",
        "suspiciously-short-output",
        "invalid-edit-structure",
        "truncated-model-output",
        "changed-protected-placeholder",
        "unknown-protected-placeholder",
        "invalid-prose-patch",
        "unchanged-unclean-prose",
        "changed-code-block",
        "changed-table-shape",
        "changed-table-values",
        "semantic-fidelity-failed",
        "editorial-quality-failed",
        "changed-literal-link",
        "invalid-fidelity-verification",
    }
)

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
LOCAL_IMAGE_REFERENCE_REGEX = re.compile(
    r"!\[[^\]]*\]\((?![a-z][a-z0-9+.-]*:|/|#)([^)\s]+)\)", re.IGNORECASE
)
PAGE_BREAK_REGEX = re.compile(r"<!--\s*page-break\s*-->", re.IGNORECASE)
ALL_IMAGE_REFERENCE_REGEX = re.compile(r"!\[[^\]]*\]\(([^)\n]+)\)")
UNCLEAR_TRANSCRIPTION_REGEX = re.compile(r"\[transcription unclear:\s*([^\]]+)\]", re.IGNORECASE)
VERBATIM_NUMBER_REGEX = re.compile(
    r"(?<=\b[vV])\d+(?:[.,]\d+)+|(?<!\w)\d+(?:[.,]\d+)*|(?<!\d)\d+\b"
)
LITERAL_LINK_REGEX = re.compile(
    r"(?<![\w@.-])(?:https?://|www\.)[^\s<>()\[\]]+"
    r"|(?<![\w@.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}(?:/[^\s<>()\[\]]*)?"
)


def literal_links(text: str) -> list[str]:
    return [m.group().rstrip(".,!?;:\"'’”") for m in LITERAL_LINK_REGEX.finditer(text)]


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

    filtered = filter_artifact_lines(text, source_text=source_text)
    if filtered != text:
        warnings.append("artifact-filtered")
        text = filtered

    if not text:
        warnings.append("empty-after-artifact-filter")
        return ValidationResult(text="", warnings=tuple(warnings))

    if input_size > 100 and len(text) < input_size * 0.2:
        warnings.append("suspiciously-short-output")

    text = _normalize_output_whitespace(text)
    warnings.extend(_markdown_quality_warnings(text, source_text=source_text))
    return ValidationResult(text=text, warnings=tuple(warnings))


def _normalize_output_whitespace(text: str) -> str:
    return normalize_markdown_whitespace(text)


def filter_artifact_lines(text: str, *, source_text: str | None = None) -> str:
    """Remove generated assistant lines, never quoted source or code containing the same words."""
    source = (source_text or "").casefold()

    def filter_prose(prose: str) -> str:
        kept: list[str] = []
        for line in prose.splitlines(keepends=True):
            match = ARTIFACT_REGEX.match(line.lstrip())
            if match is None or match.group().casefold() in source:
                kept.append(line)
        return "".join(kept)

    return transform_prose(text, filter_prose).strip()


def _citation_markers(text: str) -> Counter[str]:
    """Compare identities across bracket, Markdown footnote, and HTML superscript forms."""
    markers: Counter[str] = Counter()
    pattern = re.compile(
        r"\[(?:\^)?([0-9A-Za-z][0-9A-Za-z .:-]{0,20})\]"
        r"|<sup>\s*([0-9]+)\s*</sup>"
        r"|(\([A-Z][A-Za-z-]+,\s*\d{4}\))",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        identity = next(value for value in match.groups() if value is not None)
        markers[" ".join(identity.split())] += 1
    return markers


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
    if UNCLEAR_TRANSCRIPTION_REGEX.search(text):
        warnings.append("uncertain-transcription")
    if source_text is None:
        return warnings
    if Counter(literal_links(source_text)) != Counter(literal_links(text)):
        warnings.append("changed-literal-link")
    if _new_adjacent_repetition(source_text, text):
        warnings.append("new-adjacent-repetition")
    before = _lowercase_paragraphs(source_text)
    if any(count > before[word] for word, count in _lowercase_paragraphs(text).items()):
        warnings.append("new-lowercase-paragraph")
    if _paragraph_count(source_text) >= 3 and _paragraph_count(text) <= 1 and len(text) > 500:
        warnings.append("collapsed-paragraphs")
    if MARKDOWN_HEADING_REGEX.search(source_text) and MARKDOWN_HEADING_REGEX.search(text) is None:
        warnings.append("missing-markdown-heading")
    if MARKDOWN_LIST_REGEX.search(source_text) and MARKDOWN_LIST_REGEX.search(text) is None:
        warnings.append("missing-markdown-list")
    if _has_markdown_table(source_text) and not _has_markdown_table(text):
        warnings.append("missing-markdown-table")
    source_citations = _citation_markers(source_text)
    output_citations = _citation_markers(text)
    if source_citations - output_citations:
        warnings.append("missing-citation-marker")
    source_images = Counter(ALL_IMAGE_REFERENCE_REGEX.findall(source_text))
    output_images = Counter(ALL_IMAGE_REFERENCE_REGEX.findall(text))
    if source_images != output_images:
        warnings.append("changed-markdown-images")
    source_code = [
        source_text[a:b].strip()
        for a, b in protected_spans(source_text)
        if source_text[a:b].lstrip().startswith(("```", "~~~"))
    ]
    output_code = [
        text[a:b].strip()
        for a, b in protected_spans(text)
        if text[a:b].lstrip().startswith(("```", "~~~"))
    ]
    if source_code and source_code != output_code:
        warnings.append("changed-code-block")
    source_tables = _table_cells(source_text)
    output_tables = _table_cells(text)
    if source_tables:
        if [len(row) for row in source_tables] != [len(row) for row in output_tables]:
            warnings.append("changed-table-shape")
        elif [[VERBATIM_NUMBER_REGEX.findall(cell) for cell in row] for row in source_tables] != [
            [VERBATIM_NUMBER_REGEX.findall(cell) for cell in row] for row in output_tables
        ]:
            warnings.append("changed-table-values")
    if len(PAGE_BREAK_REGEX.findall(source_text)) != len(PAGE_BREAK_REGEX.findall(text)):
        warnings.append("changed-page-breaks")
    source_numbers = Counter(VERBATIM_NUMBER_REGEX.findall(source_text))
    output_numbers = Counter(VERBATIM_NUMBER_REGEX.findall(text))
    if source_numbers - output_numbers:
        warnings.append("missing-verbatim-number")
    if output_numbers - source_numbers:
        warnings.append("added-verbatim-number")
    source_words = re.findall(r"\b\w+\b", source_text)
    output_words = re.findall(r"\b\w+\b", text)
    if len(source_words) >= 200 and len(output_words) < len(source_words) * 0.8:
        warnings.append("substantial-content-loss")
    return warnings


def _lowercase_paragraphs(text: str) -> Counter[str]:
    found: Counter[str] = Counter()

    def inspect(prose: str) -> str:
        paragraphs = re.split(r"\n[ \t]*\n", prose)
        for previous, current in zip(paragraphs, paragraphs[1:], strict=False):
            if not re.search(r"[.!?][\"'”’)]?$", previous.rstrip()):
                continue
            opening = re.match(
                r"(?:and|but|or|so|now|then|we|the|you|he|she|they|it|this|that|"
                r"these|those|for|however|there|what|when|where|why|how|as|i)\b",
                current.lstrip(),
            )
            if opening:
                found[opening.group()] += 1
        return prose

    transform_prose(text, inspect)
    return found


def _new_adjacent_repetition(source: str, candidate: str) -> bool:
    """Reject a newly copied adjacent phrase, without deleting intentional source rhetoric."""

    def prose_only(value: str) -> str:
        pieces: list[str] = []
        cursor = 0
        for left, right in protected_spans(value):
            pieces.append(value[cursor:left])
            cursor = right
        pieces.append(value[cursor:])
        return re.sub(r"\s+", " ", " ".join(pieces)).casefold()

    original, edited = prose_only(source), prose_only(candidate)
    repeated = re.compile(r"\b((?:[\w'-]+\s+){3}[\w'-]+)[.!?…;,:]*\s+\1\b")
    for match in repeated.finditer(edited):
        phrase = re.compile(r"(?<!\w)" + re.escape(match.group(1)) + r"(?!\w)")
        if len(phrase.findall(edited)) > len(phrase.findall(original)):
            return True
    return False


def _paragraph_count(text: str) -> int:
    return sum(1 for paragraph in re.split(r"\n\s*\n", text.strip()) if paragraph.strip())


def _table_cells(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for left, right in protected_spans(text):
        block = text[left:right]
        if block.lstrip().startswith(("```", "~~~")):
            continue
        for line in block.splitlines():
            if MARKDOWN_TABLE_SEPARATOR_REGEX.fullmatch(line):
                continue
            body = line.strip()
            if body.startswith("|"):
                body = body[1:]
            if body.endswith("|") and not body.endswith("\\|"):
                body = body[:-1]
            rows.append([cell.strip() for cell in re.split(r"(?<!\\)\|", body)])
    return rows


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
