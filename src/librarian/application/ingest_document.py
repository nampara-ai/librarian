"""Application service for document ingestion."""

from __future__ import annotations

import hashlib
import json
import mimetypes
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from librarian.application.ports import (
    AssetStore,
    ContentStore,
    DocumentRepository,
    TextExtractor,
)
from librarian.application.quality_report import extraction_report_key
from librarian.domain.ids import DocumentId
from librarian.domain.models import (
    Document,
    DocumentAsset,
    ExtractionPayload,
    SourceFile,
)


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
    assets: AssetStore | None = None
    max_source_bytes: int | None = None

    async def execute(self, path: Path) -> IngestedDocument:
        source_path, payload = await _read_source(path, max_source_bytes=self.max_source_bytes)
        digest = hashlib.sha256(payload).hexdigest()
        document_id = DocumentId(f"doc_{digest[:16]}")
        existing = await self.documents.get_document(document_id)
        if existing is not None and existing.source.sha256 == digest:
            try:
                raw_text = await self.content.get_text(raw_text_key(document_id))
            except KeyError:
                extracted = await _extract_payload(self.extractor, source_path)
                raw_text = extracted.text
                await self.content.put_text(raw_text_key(document_id), raw_text)
                await self._save_assets(document_id, extracted)
                await self._save_extraction_report(document_id)
            else:
                # Databases created before asset persistence need one fresh
                # extraction. The durable marker also distinguishes a valid
                # empty asset set from an interrupted migration/re-extraction.
                if self.assets is not None and not await self.assets.document_assets_initialized(
                    document_id
                ):
                    extracted = await _extract_payload(self.extractor, source_path)
                    raw_text = extracted.text
                    await self.content.put_text(raw_text_key(document_id), raw_text)
                    await self._save_assets(document_id, extracted)
                    await self._save_extraction_report(document_id)
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
        extracted = await _extract_payload(self.extractor, source_path)
        raw_text = extracted.text
        # Persist the document row and its raw text atomically when the backend
        # supports it, so a crash can't leave a document with no extractable text
        # (which would fail processing with a KeyError).
        save_atomic = getattr(self.documents, "save_document_with_content", None)
        if callable(save_atomic):
            save_atomic = cast("Callable[..., Awaitable[None]]", save_atomic)
            await save_atomic(document, raw_text_key(document_id), raw_text)
        else:
            await self.documents.save_document(document)
            await self.content.put_text(raw_text_key(document_id), raw_text)
        await self._save_assets(document_id, extracted)
        await self._save_extraction_report(document_id)
        return IngestedDocument(document=document, raw_text=raw_text)

    async def _save_extraction_report(self, document_id: DocumentId) -> None:
        metadata = getattr(self.extractor, "last_metadata", None)
        if not isinstance(metadata, dict):
            return
        await self.content.put_text(
            extraction_report_key(document_id),
            json.dumps(metadata, ensure_ascii=False, default=str),
        )

    async def _save_assets(self, document_id: DocumentId, payload: ExtractionPayload) -> None:
        if self.assets is None:
            return
        converted = [
            DocumentAsset(
                document_id=document_id,
                filename=asset.filename,
                media_type=asset.media_type,
                data=asset.data,
                sha256=asset.sha256,
            )
            for asset in payload.assets
        ]
        await self.assets.replace_document_assets(document_id, converted)


def raw_text_key(document_id: DocumentId) -> str:
    """Content key for a document's extracted source text."""
    return f"raw:{document_id}"


async def _extract_payload(extractor: TextExtractor, path: Path) -> ExtractionPayload:
    rich_extract = getattr(extractor, "extract_with_assets", None)
    if callable(rich_extract):
        typed_extract = cast("Callable[[Path], Awaitable[ExtractionPayload]]", rich_extract)
        return await typed_extract(path)
    return ExtractionPayload(text=await extractor.extract(path))


async def _read_source(path: Path, *, max_source_bytes: int | None = None) -> tuple[Path, bytes]:
    import asyncio

    def read() -> tuple[Path, bytes]:
        source_path = path.expanduser().resolve()
        stat = source_path.stat()
        if max_source_bytes is not None and stat.st_size > max_source_bytes:
            raise ValueError(f"Source file exceeds {max_source_bytes} bytes: {source_path}")
        if max_source_bytes is None:
            return source_path, source_path.read_bytes()
        with source_path.open("rb") as handle:
            payload = handle.read(max_source_bytes + 1)
        if len(payload) > max_source_bytes:
            raise ValueError(f"Source file exceeds {max_source_bytes} bytes: {source_path}")
        return source_path, payload

    return await asyncio.to_thread(read)
