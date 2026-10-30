"""Application service for final assembly."""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import partial

from librarian.application.clean_chunks import CleanedChunk
from librarian.pipeline.markdown import (
    normalize_markdown_whitespace,
    protected_spans,
    transform_prose,
)
from librarian.pipeline.validation import filter_artifact_lines


def assemble_cleaned_document(chunks: Sequence[CleanedChunk]) -> str:
    """Assemble cleaned chunks and perform light boundary cleanup."""
    parts: list[str] = []
    previous_sentence = ""
    for chunk in chunks:
        source = chunk.chunk.text
        text = filter_artifact_lines(chunk.text, source_text=source)
        if "[CONTEXT:" not in source and "[CONTINUING FROM:" not in source:
            text = transform_prose(text, remove_context_markers)
        # Only a generated duplicate at a known seam may be removed. Repetition
        # present in the current source chunk remains intentional source content.
        protected = protected_spans(text)
        if (
            previous_sentence
            and not source.strip().casefold().startswith(previous_sentence)
            and not (protected and protected[0][0] == 0)
        ):
            text = _drop_leading_duplicate_sentence(text, previous_sentence)
        text = transform_prose(text, partial(remove_duplicate_headers, source_text=source))
        if "```" not in source and "~~~" not in source:
            text = remove_empty_code_fences(text)
        if text.strip():
            parts.append(text.strip())
            previous_sentence = _last_sentence(text)
    text = normalize_assembled_whitespace("\n\n".join(parts))
    return text.strip()


def remove_context_markers(text: str) -> str:
    """Remove carry-forward context notes if a provider echoed them."""
    return re.sub(
        r"^\[(?:CONTEXT: This continues from|CONTINUING FROM):.*?\]\s*",
        "",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )


def remove_artifact_lines(text: str) -> str:
    """Drop obvious assistant-artifact lines from assembled output."""
    return filter_artifact_lines(text)


def remove_consecutive_duplicate_sentences(text: str) -> str:
    """Remove exact consecutive duplicate sentences introduced at chunk boundaries."""
    parts = re.split(r"(\n{2,})", text)
    cleaned: list[str] = []
    previous_sentence = ""
    for part in parts:
        if not part or part.startswith("\n"):
            cleaned.append(part)
            continue
        if "\n" in part or part.lstrip().startswith("#"):
            part = _drop_leading_duplicate_sentence(part, previous_sentence)
            cleaned.append(part)
            previous_sentence = _last_sentence(part) or previous_sentence
            continue
        paragraph, previous_sentence = _dedupe_sentences_in_paragraph(part, previous_sentence)
        cleaned.append(paragraph)
    return "".join(cleaned)


def _dedupe_sentences_in_paragraph(paragraph: str, previous_sentence: str) -> tuple[str, str]:
    tokens = re.split(r"(?<=[.!?])(\s+)", paragraph)
    if len(tokens) < 3:
        return paragraph, _last_sentence(paragraph) or previous_sentence
    output: list[str] = []
    last_sentence = previous_sentence
    index = 0
    while index < len(tokens):
        sentence = tokens[index]
        separator = tokens[index + 1] if index + 1 < len(tokens) else ""
        normalized = sentence.strip().lower()
        if normalized and normalized == last_sentence:
            index += 2
            continue
        output.append(sentence)
        output.append(separator)
        if normalized:
            last_sentence = normalized
        index += 2
    return "".join(output).rstrip(), last_sentence


def _last_sentence(text: str) -> str:
    sentences = re.findall(r"[^.!?]+[.!?]", text.strip().rsplit("\n\n", 1)[-1])
    if not sentences:
        return ""
    return sentences[-1].strip().lower()


def _drop_leading_duplicate_sentence(text: str, previous_sentence: str) -> str:
    if not previous_sentence:
        return text
    match = re.match(r"\s*([^.!?]+[.!?])(\s*)", text)
    if match is None:
        return text
    if match.group(1).strip().lower() != previous_sentence:
        return text
    return text[match.end() :]


def remove_duplicate_headers(text: str, *, source_text: str = "") -> str:
    """Collapse duplicate Markdown headers emitted at chunk boundaries."""

    def replace(match: re.Match[str]) -> str:
        return match.group() if match.group() in source_text else match.group(1)

    return re.sub(r"(#{1,6}\s+[^\n]+)\s+\1", replace, text)


def remove_empty_code_fences(text: str) -> str:
    """Remove fenced code blocks that contain no content."""
    return re.sub(
        r"(?m)(?:^|\n)[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*\n"
        r"[ \t]*\n?[ \t]*(?P=fence)[ \t]*(?:\n|$)",
        "",
        text,
    )


def normalize_assembled_whitespace(text: str) -> str:
    """Normalize whitespace after final assembly."""
    return normalize_markdown_whitespace(text)
