"""Conservative Markdown boundaries shared by chunking and output cleanup."""

from __future__ import annotations

import re
from collections.abc import Callable

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


def protected_spans(text: str) -> list[tuple[int, int]]:
    """Keep fenced code and complete Markdown tables intact, including large blocks."""
    lines = text.splitlines(keepends=True)
    spans: list[tuple[int, int]] = []
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    index = 0
    while index < len(lines):
        fence = _FENCE.match(lines[index].rstrip("\r\n"))
        if fence:
            marker = fence.group(1)
            end = index + 1
            close = re.compile(rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}\s*$")
            while end < len(lines) and not close.fullmatch(lines[end].rstrip("\r\n")):
                end += 1
            end = min(len(lines), end + 1)
            spans.append((offsets[index], offsets[end]))
            index = end
        elif (
            index + 1 < len(lines)
            and "|" in lines[index]
            and _TABLE_SEPARATOR.fullmatch(lines[index + 1].strip())
        ):
            end = index + 2
            while end < len(lines) and lines[end].strip() and "|" in lines[end]:
                end += 1
            spans.append((offsets[index], offsets[end]))
            index = end
        else:
            index += 1
    return spans


def transform_prose(text: str, transform: Callable[[str], str]) -> str:
    """Apply a textual cleanup only outside protected Markdown blocks."""
    pieces: list[str] = []
    start = 0
    for left, right in protected_spans(text):
        pieces.extend((transform(text[start:left]), text[left:right]))
        start = right
    pieces.append(transform(text[start:]))
    return "".join(pieces)


def normalize_markdown_whitespace(text: str) -> str:
    """Preserve indentation and hard line breaks; normalize only excess prose blank lines."""
    return transform_prose(text, lambda prose: re.sub(r"\n{4,}", "\n\n\n", prose)).strip()
