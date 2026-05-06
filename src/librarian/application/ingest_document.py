"""Application service for document ingestion."""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path

from librarian.application.ports import ContentStore, DocumentRepository, TextExtractor
from librarian.domain.ids import DocumentId
from librarian.domain.models import Document, SourceFile


@dataclass(frozen=True, slots=True)
class IngestedDocument:
    """Document plus extracted text."""

    document: Document
    raw_text: str
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class IngestDocument:
    """Ingest a file into the library."""

    documents: DocumentRepository
    content: ContentStore
    extractor: TextExtractor

    async def execute(self, path: Path) -> IngestedDocument:
        source_path, payload = await _read_source(path)
        digest = hashlib.sha256(payload).hexdigest()
        document_id = DocumentId(f"doc_{digest[:16]}")
        existing = await self.documents.get_document(document_id)
        if existing is not None and existing.source.sha256 == digest:
            try:
                raw_text = await self.content.get_text(raw_text_key(document_id))
            except KeyError:
                raw_text = await self.extractor.extract(source_path)
                await self.content.put_text(raw_text_key(document_id), raw_text)
            return IngestedDocument(document=existing, raw_text=raw_text, duplicate=True)

        media_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
        document = Document(
            id=document_id,
            source=SourceFile(
                path=source_path,
                filename=source_path.name,
                media_type=media_type,
                byte_size=len(payload),
                sha256=digest,
            ),
        )
        raw_text = await self.extractor.extract(source_path)
        await self.documents.save_document(document)
        await self.content.put_text(raw_text_key(document_id), raw_text)
        return IngestedDocument(document=document, raw_text=raw_text)


def raw_text_key(document_id: DocumentId) -> str:
    """Content key for a document's extracted source text."""
    return f"raw:{document_id}"


async def _read_source(path: Path) -> tuple[Path, bytes]:
    import asyncio

    def read() -> tuple[Path, bytes]:
        source_path = path.expanduser().resolve()
        return source_path, source_path.read_bytes()

    return await asyncio.to_thread(read)
