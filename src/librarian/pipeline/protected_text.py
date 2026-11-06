"""Reversible source tokens keep numeric data outside generative spelling decisions."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from librarian.pipeline.validation import (
    LITERAL_LINK_REGEX,
    VERBATIM_NUMBER_REGEX,
    SourceContext,
    repairable_number_spans,
)


@dataclass(frozen=True, slots=True)
class NumericReading:
    observed: str
    reading: str

    def as_dict(self) -> dict[str, str]:
        return {
            "observed": self.observed,
            "reading": self.reading,
            "role": "decade-shorthand",
            "century": "unspecified",
        }


@dataclass(frozen=True, slots=True)
class ProtectedText:
    text: str
    prefix: str
    values: tuple[tuple[str, str], ...]
    numeric_readings: tuple[NumericReading, ...] = ()

    def restore(self, candidate: str) -> tuple[str, tuple[str, ...]]:
        warnings: list[str] = []
        expected = {token for token, _ in self.values}
        found = set(re.findall(re.escape(self.prefix) + r"[A-Z]+\]\]", candidate))
        if found - expected:
            warnings.append("unknown-protected-placeholder")
        for token, value in self.values:
            if candidate.count(token) != 1:
                warnings.append("changed-protected-placeholder")
            candidate = candidate.replace(token, value)
        marker_pattern = r"\[\[LBR_[A-Za-z0-9]+_[A-Z]+\]\]"
        original_literals = set(re.findall(marker_pattern, self.text)) - expected
        if set(re.findall(marker_pattern, candidate)) - original_literals:
            warnings.append("unknown-protected-placeholder")
        if self.prefix in candidate:
            warnings.append("unknown-protected-placeholder")
        return candidate, tuple(dict.fromkeys(warnings))


def protect_numerals(source: str) -> ProtectedText:
    """Each occurrence gets its own identity, including repeated equal values."""
    return _protect(source, include_links=False)


def protect_prose_data(
    source: str, *, source_context: str | None = None, context: SourceContext | None = None
) -> ProtectedText:
    """URLs are identifiers, so do not spell-correct them as names or prose."""
    return _protect(source, include_links=True, source_context=source_context, context=context)


def _protect(
    source: str,
    *,
    include_links: bool,
    source_context: str | None = None,
    context: SourceContext | None = None,
) -> ProtectedText:
    salt = 0
    while True:
        digest = hashlib.sha256((str(salt) + "\0" + source).encode()).hexdigest()[:8]
        prefix = f"[[LBR_{digest}_"
        if prefix not in source:
            break
        salt += 1
    values: list[tuple[str, str]] = []

    def replace(value: str) -> str:
        index = len(values)
        label = ""
        while True:
            index, remainder = divmod(index, 26)
            label = chr(65 + remainder) + label
            if not index:
                break
            index -= 1
        token = prefix + label + "]]"
        values.append((token, value))
        return token

    links = (
        [
            (m.start(), m.start() + len(m.group().rstrip(".,!?;:\"'’”")))
            for m in LITERAL_LINK_REGEX.finditer(source)
        ]
        if include_links
        else []
    )
    existing_markers = [
        match.span() for match in re.finditer(r"\[\[LBR_[A-Za-z0-9]+_[A-Z]+\]\]", source)
    ]
    lexical_repairs = (
        repairable_number_spans(source, source_context=source_context, context=context)
        if include_links
        else {}
    )
    numbers = [
        (m.start(), m.end())
        for m in VERBATIM_NUMBER_REGEX.finditer(source)
        if m.span() not in lexical_repairs
        and not any(left <= m.start() < right for left, right in [*links, *existing_markers])
    ]
    pieces: list[str] = []
    cursor = 0
    for left, right in sorted([*links, *numbers]):
        pieces.extend((source[cursor:left], replace(source[left:right])))
        cursor = right
    pieces.append(source[cursor:])
    readings = tuple(
        NumericReading(observed, reading)
        for observed, reading in dict.fromkeys(
            (source[left:right], reading) for (left, right), reading in lexical_repairs.items()
        )
    )
    return ProtectedText("".join(pieces), prefix, tuple(values), readings)
