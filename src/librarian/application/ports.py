"""Hexagonal ports used by application services."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Protocol

from librarian.domain.ids import DocumentId, RunId
from librarian.domain.models import (
    Chunk,
    Classification,
    CleanedOutput,
    Document,
    ProcessingRun,
    RunStage,
    RunStatus,
)


class DocumentRepository(Protocol):
    """Persistence port for documents."""

    async def save(self, document: Document) -> None: ...

    async def get(self, document_id: DocumentId) -> Document | None: ...

    async def list(self) -> Sequence[Document]: ...


class RunRepository(Protocol):
    """Persistence port for processing runs."""

    async def save(self, run: ProcessingRun) -> None: ...

    async def get(self, run_id: RunId) -> ProcessingRun | None: ...

    async def update_status(
        self,
        run_id: RunId,
        *,
        status: RunStatus,
        stage: RunStage,
        error: str | None = None,
    ) -> None: ...


class ContentStore(Protocol):
    """Storage port for raw and generated text content."""

    async def put_text(self, key: str, text: str) -> str: ...

    async def get_text(self, key: str) -> str: ...


class TextExtractor(Protocol):
    """Port for extracting text from source files."""

    supported_extensions: frozenset[str]

    async def extract(self, path: Path) -> str: ...


class LLMProvider(Protocol):
    """Provider-agnostic LLM port."""

    name: str

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str: ...


class TaxonomyProvider(Protocol):
    """Port for taxonomy lookup and validation."""

    name: str

    def label_for(self, code: str) -> str | None: ...


class SearchIndex(Protocol):
    """Search index port."""

    async def index(self, output: CleanedOutput, classification: Classification | None) -> None: ...

    async def search(self, query: str, *, limit: int = 20) -> Sequence[DocumentId]: ...


class EventSink(Protocol):
    """Structured run event sink."""

    async def emit(self, run_id: RunId, stage: RunStage, message: str) -> None: ...

    async def stream(self, run_id: RunId) -> AsyncIterator[str]: ...


class ChunkRepository(Protocol):
    """Persistence port for chunks."""

    async def save_many(self, chunks: Sequence[Chunk]) -> None: ...

    async def list_for_document(self, document_id: DocumentId) -> Sequence[Chunk]: ...
