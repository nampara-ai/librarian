"""Hexagonal ports used by application services."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from librarian.domain.ids import DocumentId, RunId
from librarian.domain.models import (
    Chunk,
    Classification,
    CleanedOutput,
    Document,
    DocumentStatus,
    ProcessingRun,
    RunStage,
    RunStatus,
    SearchFacets,
    SearchResult,
)

SearchScope = Literal["cleaned", "raw"]


class DocumentRepository(Protocol):
    """Persistence port for documents."""

    async def save_document(self, document: Document) -> None: ...

    async def get_document(self, document_id: DocumentId) -> Document | None: ...

    async def list(self, *, limit: int = 100, offset: int = 0) -> Sequence[Document]: ...

    async def count_documents(self) -> int: ...

    async def update_document_status(
        self,
        document_id: DocumentId,
        status: DocumentStatus,
    ) -> None: ...


class RunRepository(Protocol):
    """Persistence port for processing runs."""

    async def save_run(self, run: ProcessingRun) -> None: ...

    async def get_run(self, run_id: RunId) -> ProcessingRun | None: ...

    async def is_run_canceled(self, run_id: RunId) -> bool: ...

    async def update_status(
        self,
        run_id: RunId,
        *,
        status: RunStatus,
        stage: RunStage,
        error: str | None = None,
    ) -> None: ...

    async def update_run_progress(
        self,
        run_id: RunId,
        *,
        completed_chunks: int,
        failed_chunks: int,
        stage: RunStage,
        status: RunStatus = RunStatus.RUNNING,
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


class EventSink(Protocol):
    """Structured run event sink."""

    async def emit(self, run_id: RunId, stage: RunStage, message: str) -> None: ...

    def stream(self, run_id: RunId) -> AsyncIterator[str]: ...


class ApplicationMetrics(Protocol):
    """Metrics sink for application services."""

    def record_run_stage(self, *, stage: str, duration_ms: float) -> None: ...

    def record_run_finished(self, *, status: str) -> None: ...

    def record_queue_claim(self, *, wait_ms: float) -> None: ...

    def record_queue_failure(self) -> None: ...

    def record_conversion_failure(
        self,
        *,
        failure_type: str,
        source_extension: str,
    ) -> None: ...

    def record_ocr_page(
        self,
        *,
        source: str,
        status: str,
        duration_ms: float,
        corrected: bool = False,
    ) -> None: ...

    def record_llm_usage(
        self,
        *,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        estimated_cost_usd: float = 0.0,
    ) -> None: ...


class ChunkRepository(Protocol):
    """Persistence port for chunks."""

    async def save_many(self, chunks: Sequence[Chunk]) -> None: ...

    async def list_for_document(self, document_id: DocumentId) -> Sequence[Chunk]: ...


class OutputRepository(Protocol):
    """Persistence port for generated output."""

    async def save_cleaned_output(self, output: CleanedOutput) -> None: ...

    async def save_cleaned_chunks(self, run_id: RunId, chunks: Sequence[Any]) -> None: ...

    async def get_cached_cleaned_chunks(
        self,
        chunks: Sequence[Chunk],
        *,
        prompt_version: str,
        model_provider: str,
        model_name: str,
    ) -> Sequence[Any]: ...

    async def save_cleaned_chunk_cache(
        self,
        chunks: Sequence[Any],
        *,
        prompt_version: str,
        model_provider: str,
        model_name: str,
    ) -> None: ...

    async def get_cleaned_output(self, document_id: DocumentId) -> CleanedOutput | None: ...

    async def save_classification(self, classification: Classification) -> None: ...

    async def get_classification(self, document_id: DocumentId) -> Classification | None: ...

    async def publish_successful_run(
        self,
        output: CleanedOutput,
        classification: Classification,
    ) -> None: ...


class SearchIndex(Protocol):
    """Port for full-text or semantic search adapters."""

    async def index(
        self,
        output: CleanedOutput,
        classification: Classification | None,
    ) -> None: ...

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        classification_code: str | None = None,
        classification_prefix: str | None = None,
        document_status: DocumentStatus | None = None,
        filename_contains: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        scope: SearchScope = "cleaned",
        phrase: bool = False,
    ) -> Sequence[DocumentId]: ...

    async def search_results(
        self,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        classification_code: str | None = None,
        classification_prefix: str | None = None,
        document_status: DocumentStatus | None = None,
        filename_contains: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        scope: SearchScope = "cleaned",
        phrase: bool = False,
    ) -> Sequence[SearchResult]: ...

    async def search_count(
        self,
        query: str,
        *,
        classification_code: str | None = None,
        classification_prefix: str | None = None,
        document_status: DocumentStatus | None = None,
        filename_contains: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        scope: SearchScope = "cleaned",
        phrase: bool = False,
    ) -> int: ...

    async def search_facets(
        self,
        query: str,
        *,
        classification_code: str | None = None,
        classification_prefix: str | None = None,
        document_status: DocumentStatus | None = None,
        filename_contains: str | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        scope: SearchScope = "cleaned",
        phrase: bool = False,
    ) -> SearchFacets: ...
