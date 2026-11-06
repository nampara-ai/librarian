"""Repair identified candidate spans while preserving every other byte of the draft."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import cast

from librarian.domain.ids import DocumentId
from librarian.pipeline.chunking import ChunkingPolicy, chunk_text


@dataclass(frozen=True, slots=True)
class CandidateSpan:
    id: str
    text: str
    start: int
    end: int


def candidate_spans(candidate: str) -> list[CandidateSpan]:
    spans: list[CandidateSpan] = []
    for paragraph in re.finditer(r"\S(?:[\s\S]*?\S)?(?=\r?\n[ \t]*\r?\n|\s*$)", candidate):
        chunks = chunk_text(
            DocumentId("candidate"),
            paragraph.group(),
            ChunkingPolicy(target_chars=500, overlap_chars=0, min_chunk_chars=100),
        )
        for chunk in chunks:
            start, end = paragraph.start() + chunk.start_char, paragraph.start() + chunk.end_char
            spans.append(CandidateSpan(f"p{len(spans)}", candidate[start:end], start, end))
    return spans


def apply_prose_patches(candidate: str, raw: str) -> tuple[str, tuple[str, ...]]:
    body = raw.strip()
    fence = re.fullmatch(r"```(?:json)?\s*([\s\S]+?)\s*```", body, re.I)
    if fence:
        body = fence.group(1)
    try:
        payload: object = json.loads(body)
    except ValueError:
        return candidate, ("invalid-prose-patch",)
    if not isinstance(payload, dict) or set(cast("dict[str, object]", payload)) != {"patches"}:
        return candidate, ("invalid-prose-patch",)
    patches = cast("dict[str, object]", payload)["patches"]
    if not isinstance(patches, list) or not 1 <= len(cast("list[object]", patches)) <= 8:
        return candidate, ("invalid-prose-patch",)
    indexed = {span.id: span for span in candidate_spans(candidate)}
    edits: list[tuple[int, int, str]] = []
    for item in cast("list[object]", patches):
        if not isinstance(item, dict):
            return candidate, ("invalid-prose-patch",)
        patch = cast("dict[str, object]", item)
        identifier, old, text = patch.get("id"), patch.get("find"), patch.get("replace")
        if (
            set(patch) != {"id", "find", "replace"}
            or not isinstance(identifier, str)
            or not isinstance(old, str)
            or not isinstance(text, str)
            or identifier not in indexed
            or not 1 <= len(old) <= 500
            or len(text) > 2000
        ):
            return candidate, ("invalid-prose-patch",)
        span = indexed[identifier]
        if old == text or span.text.count(old) != 1:
            return candidate, ("invalid-prose-patch",)
        start = span.start + span.text.index(old)
        edits.append((start, start + len(old), text))
    edits.sort()
    if any(a[1] > b[0] for a, b in zip(edits, edits[1:], strict=False)):
        return candidate, ("invalid-prose-patch",)
    result = candidate
    for start, end, text in sorted(edits, reverse=True):
        result = result[:start] + text + result[end:]
    return result, ()
