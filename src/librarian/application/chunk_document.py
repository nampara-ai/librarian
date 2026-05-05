"""Application service for deterministic document chunking."""

from __future__ import annotations

from dataclasses import dataclass

from librarian.domain.ids import DocumentId
from librarian.domain.models import Chunk
from librarian.pipeline.chunking import ChunkingPolicy, chunk_text


@dataclass(frozen=True, slots=True)
class ChunkDocument:
    """Chunk extracted text using the configured policy."""

    policy: ChunkingPolicy

    async def execute(self, document_id: DocumentId, text: str) -> list[Chunk]:
        return chunk_text(document_id, text, self.policy)
