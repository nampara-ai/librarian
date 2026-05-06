"""Batch import workflow."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from librarian.application.convert_document import (
    BatchConversionItem,
    ConversionFormat,
    DirectoryOutputMode,
    DocumentConverter,
)
from librarian.application.ingest_document import IngestDocument
from librarian.application.jobs import RunQueue
from librarian.application.process_document import ProcessDocument
from librarian.domain.ids import DocumentId, RunId


class ImportProcessingMode(StrEnum):
    """Import processing behavior."""

    NONE = "none"
    PROCESS = "process"
    QUEUE = "queue"


@dataclass(frozen=True, slots=True)
class ImportItem:
    """One imported source file result."""

    source_path: Path
    converted_path: Path | None
    document_id: DocumentId | None
    run_id: RunId | None
    status: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Batch import result."""

    items: tuple[ImportItem, ...]

    @property
    def converted(self) -> int:
        return sum(1 for item in self.items if item.converted_path is not None)

    @property
    def ingested(self) -> int:
        return sum(1 for item in self.items if item.document_id is not None)

    @property
    def processed(self) -> int:
        return sum(1 for item in self.items if item.status == "processed")

    @property
    def queued(self) -> int:
        return sum(1 for item in self.items if item.status == "queued")

    @property
    def failed(self) -> int:
        return sum(1 for item in self.items if item.status == "failed")


@dataclass(frozen=True, slots=True)
class ImportLibrary:
    """Convert, ingest, and optionally process a directory of files."""

    converter: DocumentConverter
    ingest: IngestDocument
    process: ProcessDocument
    queue_factory: Callable[[], RunQueue] | None = None

    async def import_directory(
        self,
        source_dir: Path,
        *,
        format: ConversionFormat,
        output_mode: DirectoryOutputMode,
        processing_mode: ImportProcessingMode,
        output_dir: Path | None = None,
        subdirectory_name: str = "librarian-converted",
        recursive: bool = False,
        overwrite: bool = False,
    ) -> ImportResult:
        """Run the full import workflow for a directory."""
        converted = await self.converter.convert_directory(
            source_dir,
            format=format,
            output_mode=output_mode,
            output_dir=output_dir,
            subdirectory_name=subdirectory_name,
            recursive=recursive,
            overwrite=overwrite,
        )
        items: list[ImportItem] = []
        for conversion in converted.items:
            items.append(await self._ingest_converted(conversion, processing_mode))
        return ImportResult(items=tuple(items))

    async def _ingest_converted(
        self,
        conversion: BatchConversionItem,
        processing_mode: ImportProcessingMode,
    ) -> ImportItem:
        if conversion.status == "failed" or conversion.output_path is None:
            return ImportItem(
                source_path=conversion.source_path,
                converted_path=conversion.output_path,
                document_id=None,
                run_id=None,
                status="failed",
                error=conversion.error,
            )

        try:
            ingested = await self.ingest.execute(conversion.output_path)
            if processing_mode == ImportProcessingMode.NONE:
                return ImportItem(
                    source_path=conversion.source_path,
                    converted_path=conversion.output_path,
                    document_id=ingested.document.id,
                    run_id=None,
                    status="ingested",
                )
            if processing_mode == ImportProcessingMode.PROCESS:
                run = await self.process.execute(ingested.document.id)
                return ImportItem(
                    source_path=conversion.source_path,
                    converted_path=conversion.output_path,
                    document_id=ingested.document.id,
                    run_id=run.id,
                    status="processed",
                )
            run = await self.process.start(ingested.document.id)
            if self.queue_factory is None:
                raise RuntimeError("Queue processing requires a queue adapter")
            await self.queue_factory().enqueue(run.id)
            return ImportItem(
                source_path=conversion.source_path,
                converted_path=conversion.output_path,
                document_id=ingested.document.id,
                run_id=run.id,
                status="queued",
            )
        except Exception as exc:
            return ImportItem(
                source_path=conversion.source_path,
                converted_path=conversion.output_path,
                document_id=None,
                run_id=None,
                status="failed",
                error=str(exc),
            )
