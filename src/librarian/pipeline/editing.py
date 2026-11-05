"""Source-aligned editing units and strict reconstruction of model edits."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence
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


def editing_units(
    source: str, *, target_chars: int = 4000, pack_prose_blocks: bool = False
) -> list[EditUnit]:
    """Use modest source spans so a complete response cannot silently skip a passage."""
    units: list[EditUnit] = []

    def add(text: str, kind: str, separator: str) -> None:
        if text.strip():
            if kind == "prose" and units:
                separator += text[: len(text) - len(text.lstrip(" \t"))]
            units.append(EditUnit(f"u{len(units):04d}", text.strip(), kind, separator))

    def prose(text: str) -> None:
        for paragraph in re.split(r"\n\s*\n", text.strip("\r\n")):
            if not paragraph.strip():
                continue
            if "\n" in paragraph:
                pending: list[str] = []
                separator = "\n\n"
                for line in paragraph.splitlines():
                    if pending and len("\n".join([*pending, line])) > target_chars:
                        add("\n".join(pending), "prose", separator)
                        separator = "  \n" if pending[-1].endswith("  ") else "\n"
                        pending = []
                    if len(line) > target_chars:
                        pieces = chunk_text(
                            DocumentId("edit-line"),
                            line,
                            ChunkingPolicy(
                                target_chars=target_chars, overlap_chars=0, min_chunk_chars=100
                            ),
                        )
                        for index, piece in enumerate(pieces):
                            indent = line[: len(line) - len(line.lstrip(" \t"))]
                            add(piece.text, "prose", separator + indent if index == 0 else " ")
                        separator = "  \n" if line.endswith("  ") else "\n"
                    else:
                        pending.append(line)
                if pending:
                    add("\n".join(pending), "prose", separator)
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
    if not pack_prose_blocks:
        return units
    packed: list[EditUnit] = []
    for unit in units:
        if (
            packed
            and unit.kind == packed[-1].kind == "prose"
            and len(packed[-1].text) + len(unit.separator) + len(unit.text) <= target_chars
        ):
            previous = packed[-1]
            packed[-1] = EditUnit(
                previous.id, previous.text + unit.separator + unit.text, "prose", previous.separator
            )
        else:
            packed.append(EditUnit(f"u{len(packed):04d}", unit.text, unit.kind, unit.separator))
    return packed


def decode_edits(
    raw: str, units: Sequence[EditUnit]
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
        if (
            unit.kind == "prose"
            and result.text == unit.text
            and len(re.findall(r"\b\w+\b", unit.text)) > 100
            and len(re.findall(r"[.!?](?:\s|$)", unit.text)) < 2
        ):
            warnings.append("unchanged-unclean-prose")
        if unit.kind == "code" and result.text != unit.text:
            warnings.append("changed-code-block")
        if warnings:
            failures[unit.id] = warnings
        separator = unit.separator
        edited = result.text
        edited = _canonical_unit_text(edited, unit, parts[-1] if parts else "")
        parts.append((separator if parts else "") + edited)
    warnings = tuple(dict.fromkeys(warning for values in failures.values() for warning in values))
    return "".join(parts), warnings, failures


def _canonical_unit_text(text: str, unit: EditUnit, previous: str) -> str:
    """Normalize a unit once, without inventing paragraphs at transport boundaries."""
    if unit.kind != "prose":
        return text
    text = text.translate(str.maketrans({"\u00a0": " ", "\u202f": " ", "‑": "-"}))
    if unit.separator == " " and re.search(r"[.!?][\"'”’)]?$", previous.rstrip()):
        if re.match(
            r"(?:and|but|so|now|then|we|the|you|he|she|they|it|this|that|these|those|"
            r"for|however|there|what|when|where|why|how|as|i)\b",
            text,
        ):
            text = text[0].upper() + text[1:]
    return text


def verification_pairs(
    raw: str, units: Sequence[EditUnit], *, include_unchanged_prose: bool = False
) -> list[dict[str, str]]:
    """Read already-validated JSON edits for the independent meaning-preservation audit."""
    body = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]+?)\s*```", body, re.I)
    if fenced:
        body = fenced.group(1)
    payload = cast("dict[str, list[dict[str, str]]]", json.loads(body))
    rendered: list[str] = []
    for unit, edit in zip(units, payload["edits"], strict=True):
        validated = validate_cleaned_text(
            edit["text"], input_size=len(unit.text), source_text=unit.text
        )
        rendered.append(
            _canonical_unit_text(validated.text, unit, rendered[-1] if rendered else "")
        )
    return [
        {
            "id": unit.id,
            "source": unit.text,
            "edited": rendered[index],
            "context_before": units[index - 1].text[-1200:] if index else "",
            "context_after": units[index + 1].text[:1200] if index + 1 < len(units) else "",
            "edited_context_before": (rendered[index - 1] + unit.separator)[-1200:]
            if index
            else "",
            "edited_context_after": (
                (units[index + 1].separator + rendered[index + 1])[:1200]
                if index + 1 < len(units)
                else ""
            ),
        }
        for index, unit in enumerate(units)
        if unit.text != rendered[index] or (include_unchanged_prose and unit.kind == "prose")
    ]


def coalesce_adjacent_repairs(
    units: Sequence[EditUnit],
    edits: dict[str, str],
    failures: dict[str, list[str]],
    *,
    max_chars: int = 6000,
) -> tuple[list[EditUnit], dict[str, str], dict[str, list[str]]]:
    """Give adjacent rejected prose one bounded, atomic repair window.

    Unit boundaries belong to transport, not to the author's grammar. Repairing
    both sides separately can give two models ownership of the same punctuation.
    Source and candidate remain contiguous and lossless when a window grows.
    """
    combined: list[EditUnit] = []
    updated = dict(edits)
    checks = {key: list(value) for key, value in failures.items()}
    for unit in units:
        if combined:
            left = combined[-1]
            if (
                left.kind == unit.kind == "prose"
                and left.id in checks
                and unit.id in checks
                and left.id in updated
                and unit.id in updated
                and len(left.text) + len(unit.separator) + len(unit.text) <= max_chars
            ):
                combined[-1] = EditUnit(
                    left.id, left.text + unit.separator + unit.text, "prose", left.separator
                )
                updated[left.id] += unit.separator + updated.pop(unit.id)
                checks[left.id] = list(dict.fromkeys(checks[left.id] + checks.pop(unit.id)))
                continue
        combined.append(unit)
    return combined, updated, checks
