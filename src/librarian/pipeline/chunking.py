"""Deterministic text chunking."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk

_BOUNDARY_PATTERN = re.compile(r"\n(?=#{1,6}\s)|\n{2,}|(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class ChunkingPolicy:
    """Chunking controls.

    ``overlap_chars`` is **not** re-emitted into adjacent chunks (doing so
    duplicated ~``overlap_chars`` of content at every chunk seam in the final
    document). Chunks tile the source exactly at clean sentence/paragraph
    boundaries; cross-boundary continuity for the cleaner is provided separately
    as read-only *context* (see ``CleanChunks``), sized by ``overlap_chars``.
    """

    target_chars: int = 12_000
    overlap_chars: int = 800
    min_chunk_chars: int = 500

    def __post_init__(self) -> None:
        if self.target_chars <= 0:
            raise ValueError("target_chars must be positive")
        if self.overlap_chars < 0:
            raise ValueError("overlap_chars cannot be negative")
        if self.overlap_chars >= self.target_chars:
            raise ValueError("overlap_chars must be smaller than target_chars")
        if self.min_chunk_chars < 0:
            raise ValueError("min_chunk_chars cannot be negative")


def chunk_text(document_id: DocumentId, text: str, policy: ChunkingPolicy) -> list[Chunk]:
    """Split text into deterministic chunks.

    The splitter prefers semantic boundaries and falls back to hard boundaries
    when source material contains very long paragraphs.
    """
    normalized = text.strip()
    if not normalized:
        return []

    chunks: list[Chunk] = []
    start = 0
    ordinal = 0
    text_length = len(normalized)

    while start < text_length:
        desired_end = min(start + policy.target_chars, text_length)
        end = _choose_boundary(normalized, start, desired_end, policy)
        chunk_body = normalized[start:end].strip()

        if chunk_body:
            chunk_start = _skip_leading_space(normalized, start)
            chunk_end = chunk_start + len(chunk_body)
            chunks.append(_make_chunk(document_id, ordinal, chunk_body, chunk_start, chunk_end))
            ordinal += 1

        if end >= text_length:
            break

        # Tile exactly: the next chunk starts where this one ended (a boundary
        # chosen by _choose_boundary), so no source region is emitted twice.
        # Continuity is handled by read-only cleaning context, not re-emission.
        start = _skip_leading_space(normalized, end)

    return chunks


def _choose_boundary(text: str, start: int, desired_end: int, policy: ChunkingPolicy) -> int:
    if desired_end >= len(text):
        return len(text)

    lower_bound = start + max(policy.min_chunk_chars, policy.target_chars // 2)
    candidates = [
        match.start()
        for match in _BOUNDARY_PATTERN.finditer(text, lower_bound, min(len(text), desired_end + 1))
    ]
    if candidates:
        return candidates[-1]

    return desired_end


def _make_chunk(
    document_id: DocumentId,
    ordinal: int,
    text: str,
    start_char: int,
    end_char: int,
) -> Chunk:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    identity = hashlib.sha256(f"{document_id}:{ordinal}:{digest}".encode()).hexdigest()
    chunk_id = ChunkId(f"chk_{identity[:16]}")
    return Chunk(
        id=chunk_id,
        document_id=document_id,
        ordinal=ordinal,
        text=text,
        start_char=start_char,
        end_char=end_char,
        sha256=digest,
    )


def _skip_leading_space(text: str, offset: int) -> int:
    while offset < len(text) and text[offset].isspace():
        offset += 1
    return offset
