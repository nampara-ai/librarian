"""Core domain models.

These models deliberately avoid framework and infrastructure dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from librarian.domain.ids import ChunkId, DocumentId, RunId


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(UTC)


class DocumentStatus(StrEnum):
    """Document lifecycle status."""

    INGESTED = "ingested"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class RunStatus(StrEnum):
    """Processing run lifecycle status."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class RunStage(StrEnum):
    """Pipeline stage names."""

    INGEST = "ingest"
    EXTRACT = "extract"
    NORMALIZE = "normalize"
    CHUNK = "chunk"
    CLEAN = "clean"
    VALIDATE = "validate"
    ASSEMBLE = "assemble"
    CLASSIFY = "classify"
    INDEX = "index"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class SourceFile:
    """Original source file metadata."""

    path: Path
    filename: str
    media_type: str
    byte_size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Document:
    """A logical library document."""

    id: DocumentId
    source: SourceFile
    status: DocumentStatus = DocumentStatus.INGESTED
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class Chunk:
    """A deterministic segment of extracted document text."""

    id: ChunkId
    document_id: DocumentId
    ordinal: int
    text: str
    start_char: int
    end_char: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Classification:
    """Library taxonomy classification."""

    document_id: DocumentId
    code: str
    label: str
    summary: str
    taxonomy: str = "dewey"
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class CleanedOutput:
    """Generated cleaned document output."""

    document_id: DocumentId
    run_id: RunId
    text: str
    prompt_version: str
    model_provider: str
    model_name: str
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class ProcessingRun:
    """One execution of the document processing pipeline."""

    id: RunId
    document_id: DocumentId
    status: RunStatus = RunStatus.QUEUED
    stage: RunStage = RunStage.INGEST
    total_chunks: int = 0
    completed_chunks: int = 0
    failed_chunks: int = 0
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RunEvent:
    """One persisted event emitted during a processing run."""

    run_id: RunId
    stage: RunStage
    message: str
    created_at: datetime
    sequence: int


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One ranked full-text search result."""

    document_id: DocumentId
    run_id: RunId | None
    source: str
    filename: str
    document_status: DocumentStatus
    created_at: datetime
    snippet: str
    score: float
    classification_code: str | None = None
    classification_label: str | None = None


@dataclass(frozen=True, slots=True)
class SearchFacetValue:
    """One search facet bucket."""

    value: str
    count: int
    label: str | None = None


@dataclass(frozen=True, slots=True)
class SearchFacets:
    """Facet counts for a full-text query."""

    classifications: tuple[SearchFacetValue, ...]
    statuses: tuple[SearchFacetValue, ...]
    sources: tuple[SearchFacetValue, ...]
    filenames: tuple[SearchFacetValue, ...]
