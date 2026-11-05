"""Explicit source-preservation markers for incomplete editorial work."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

_START = "<!-- librarian:needs-review "
_END = "<!-- /librarian:needs-review -->"
_HEADER = re.compile(re.escape(_START) + r"([^\n]+) -->\n")


def mark_source(
    source: str, *, unit_id: str, chunk: int, reasons: Sequence[str], attempts: int
) -> str:
    metadata = json.dumps(
        {
            "unit_id": unit_id,
            "chunk": chunk,
            "reasons": list(reasons),
            "attempts": attempts,
            "source_chars": len(source),
        },
        ensure_ascii=True,
    )
    metadata = (
        metadata.replace("<", r"\u003c").replace(">", r"\u003e").replace("--", r"\u002d\u002d")
    )
    return _START + metadata + " -->\n" + source + "\n" + _END


def review_items(text: str) -> list[dict[str, object]]:
    """Read length-delimited source, without interpreting source text as markers."""
    result: list[dict[str, object]] = []
    offset = 0
    while match := _HEADER.search(text, offset):
        try:
            raw = json.loads(match[1])
        except ValueError:
            offset = match.end()
            continue
        if not isinstance(raw, dict):
            offset = match.end()
            continue
        item = cast("dict[str, object]", raw)
        count = item.get("source_chars")
        if type(count) is not int or count < 0:
            offset = match.end()
            continue
        end = match.end() + count
        if not text.startswith("\n" + _END, end):
            offset = match.end()
            continue
        result.append({**item, "source": text[match.end() : end]})
        offset = end + len("\n" + _END)
    return result


def partial_key(policy: str, chunk_hash: str, *, provider: str, model: str) -> str:
    import hashlib

    identity = json.dumps([policy, chunk_hash, provider, model])
    return "cleaning:partial:" + hashlib.sha256(identity.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class UnitCheckpoint:
    unit_id: str
    source: str
    kind: str
    separator: str
    accepted_text: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "unit_id": self.unit_id,
            "source": self.source,
            "kind": self.kind,
            "separator": self.separator,
            "accepted_text": self.accepted_text,
        }


def load_checkpoint(raw: str) -> tuple[UnitCheckpoint, ...]:
    try:
        value = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(value, list):
        return ()
    result: list[UnitCheckpoint] = []
    for entry in cast("list[object]", value):
        if not isinstance(entry, dict):
            return ()
        item = cast("dict[str, object]", entry)
        fields = [item.get(key) for key in ("unit_id", "source", "kind", "separator")]
        if not all(isinstance(v, str) for v in fields):
            return ()
        accepted = item.get("accepted_text")
        if accepted is not None and not isinstance(accepted, str):
            return ()
        result.append(
            UnitCheckpoint(
                cast("str", fields[0]),
                cast("str", fields[1]),
                cast("str", fields[2]),
                cast("str", fields[3]),
                accepted,
            )
        )
    if len({item.unit_id for item in result}) != len(result):
        return ()
    return tuple(result)
