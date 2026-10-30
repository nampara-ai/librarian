"""Source-aligned editing units and strict reconstruction of model edits."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import cast

from librarian.domain.ids import DocumentId
from librarian.pipeline.chunking import ChunkingPolicy, chunk_text
from librarian.pipeline.markdown import protected_spans
from librarian.pipeline.validation import VERBATIM_NUMBER_REGEX, validate_cleaned_text


@dataclass(frozen=True, slots=True)
class EditUnit:
    id: str
    text: str
    kind: str
    separator: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "protected_numbers": dict(Counter(VERBATIM_NUMBER_REGEX.findall(self.text))),
        }


def editing_units(source: str, *, target_chars: int = 4000) -> list[EditUnit]:
    """Use modest source spans so a complete response cannot silently skip a passage."""
    units: list[EditUnit] = []

    def add(text: str, kind: str, separator: str) -> None:
        if text.strip():
            units.append(EditUnit(f"u{len(units):04d}", text.strip(), kind, separator))

    def prose(text: str) -> None:
        for paragraph in re.split(r"\n\s*\n", text.strip()):
            if not paragraph.strip():
                continue
            if "\n" in paragraph:
                add(paragraph, "prose", "\n\n")
                continue
            pieces = chunk_text(
                DocumentId("edit-unit"),
                paragraph,
                ChunkingPolicy(target_chars=target_chars, overlap_chars=0, min_chunk_chars=100),
            )
            for index, piece in enumerate(pieces):
                add(piece.text, "prose", "\n\n" if index == 0 else " ")

    start = 0
    for left, right in protected_spans(source):
        prose(source[start:left])
        block = source[left:right].strip()
        add(block, "code" if block.startswith(("```", "~~~")) else "table", "\n\n")
        start = right
    prose(source[start:])
    return units


def decode_edits(
    raw: str, units: list[EditUnit]
) -> tuple[str, tuple[str, ...], dict[str, list[str]]]:
    """Require every ID exactly once in source order, then validate every edited span."""
    body = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]+?)\s*```", body, re.I)
    if fenced:
        body = fenced.group(1)
    try:
        value = json.loads(body)
    except ValueError:
        return "", ("invalid-edit-structure",), {}
    if not isinstance(value, dict) or set(cast("dict[str, object]", value)) != {"edits"}:
        return "", ("invalid-edit-structure",), {}
    edits = cast("dict[str, object]", value).get("edits")
    if not isinstance(edits, list) or len(cast("list[object]", edits)) != len(units):
        return "", ("invalid-edit-structure",), {}
    parts: list[str] = []
    failures: dict[str, list[str]] = {}
    for unit, item in zip(units, cast("list[object]", edits), strict=True):
        if not isinstance(item, dict):
            return "", ("invalid-edit-structure",), {}
        entry = cast("dict[str, object]", item)
        text = entry.get("text")
        if set(entry) != {"id", "text"} or entry.get("id") != unit.id or not isinstance(text, str):
            return "", ("invalid-edit-structure",), {}
        result = validate_cleaned_text(text, input_size=len(unit.text), source_text=unit.text)
        warnings = list(result.warnings)
        if unit.kind == "code" and result.text != unit.text:
            warnings.append("changed-code-block")
        if warnings:
            failures[unit.id] = warnings
        separator = unit.separator
        parts.append((separator if parts else "") + result.text)
    warnings = tuple(dict.fromkeys(warning for values in failures.values() for warning in values))
    return "".join(parts), warnings, failures


def verification_pairs(raw: str, units: list[EditUnit]) -> list[dict[str, str]]:
    """Read already-validated JSON edits for the independent meaning-preservation audit."""
    body = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]+?)\s*```", body, re.I)
    if fenced:
        body = fenced.group(1)
    payload = cast("dict[str, list[dict[str, str]]]", json.loads(body))
    return [
        {
            "id": unit.id,
            "source": unit.text,
            "edited": edit["text"],
            "context_before": units[index - 1].text[-1200:] if index else "",
            "context_after": units[index + 1].text[:1200] if index + 1 < len(units) else "",
        }
        for index, (unit, edit) in enumerate(zip(units, payload["edits"], strict=True))
        if unit.text != edit["text"]
    ]
