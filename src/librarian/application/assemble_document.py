"""Application service for final assembly."""

from __future__ import annotations

import re
from collections.abc import Sequence

from librarian.application.clean_chunks import CleanedChunk
from librarian.pipeline.validation import ARTIFACT_REGEX


def assemble_cleaned_document(chunks: Sequence[CleanedChunk]) -> str:
    """Assemble cleaned chunks and perform light boundary cleanup."""
    text = "\n\n".join(chunk.text for chunk in chunks if chunk.text.strip())
    text = remove_context_markers(text)
    text = remove_artifact_lines(text)
    text = remove_consecutive_duplicate_sentences(text)
    text = remove_duplicate_headers(text)
    text = normalize_assembled_whitespace(text)
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
    lines = [line for line in text.splitlines() if not ARTIFACT_REGEX.search(line)]
    return "\n".join(lines)


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
    sentences = re.findall(r"[^.!?]+[.!?]", text)
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


def remove_duplicate_headers(text: str) -> str:
    """Collapse duplicate Markdown headers emitted at chunk boundaries."""
    return re.sub(r"(#{1,6}\s+[^\n]+)\s+\1", r"\1", text)


def normalize_assembled_whitespace(text: str) -> str:
    """Normalize whitespace after final assembly."""
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return text
