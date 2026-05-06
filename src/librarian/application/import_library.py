"""Batch import workflow."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from librarian.application.convert_document import (
    BatchConversionItem,
    ConversionFormat,
    DirectoryOutputMode,
    DocumentConverter,
    classify_conversion_error,
    conversion_output_exclusions,
    conversion_output_path,
    discover_supported_files,
    unique_output_path,
    validate_directory_output,
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
        return sum(1 for item in self.items if item.status in {"ingested", "processed", "queued"})

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
    def skipped(self) -> int:
        return sum(1 for item in self.items if item.status == "skipped")

    @property
    def failed(self) -> int:
        return sum(1 for item in self.items if item.status == "failed")

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-serializable result payload."""
        return {
            "summary": {
                "converted": self.converted,
                "ingested": self.ingested,
                "processed": self.processed,
                "queued": self.queued,
                "skipped": self.skipped,
                "failed": self.failed,
            },
            "items": [
                {
                    "source_path": str(item.source_path),
                    "converted_path": str(item.converted_path) if item.converted_path else None,
                    "document_id": str(item.document_id) if item.document_id else None,
                    "run_id": str(item.run_id) if item.run_id else None,
                    "status": item.status,
                    "error": item.error,
                }
                for item in self.items
            ],
        }


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
        manifest_path: Path | None = None,
        resume: bool = False,
        write_sidecar: bool = False,
    ) -> ImportResult:
        """Run the full import workflow for a directory."""
        validate_directory_output(
            source_dir=source_dir,
            output_mode=output_mode,
            output_dir=output_dir,
        )
        manifest = await _load_manifest(manifest_path) if resume else {}
        files = discover_supported_files(
            source_dir,
            recursive=recursive,
            supported_extensions=self.converter.extractor.supported_extensions,
            exclude_paths=conversion_output_exclusions(
                source_dir=source_dir,
                output_mode=output_mode,
                output_dir=output_dir,
                subdirectory_name=subdirectory_name,
            ),
        )
        items: list[ImportItem] = []
        for source_path in files:
            previous = manifest.get(str(source_path))
            if previous and _can_resume(previous, processing_mode):
                item = _item_from_manifest(previous)
            else:
                destination = conversion_output_path(
                    source_path,
                    source_dir=source_dir,
                    format=format,
                    output_mode=output_mode,
                    output_dir=output_dir,
                    subdirectory_name=subdirectory_name,
                )
                if not overwrite:
                    destination = await unique_output_path(destination)
                try:
                    await self.converter.convert_file(
                        source_path,
                        destination,
                        format=format,
                        overwrite=overwrite,
                        write_sidecar=write_sidecar,
                    )
                    conversion = BatchConversionItem(
                        source_path=source_path,
                        output_path=destination,
                        status="converted",
                    )
                except Exception as exc:
                    conversion = BatchConversionItem(
                        source_path=source_path,
                        output_path=destination,
                        status="failed",
                        error=str(exc),
                        error_type=classify_conversion_error(exc),
                    )
                item = await self._ingest_converted(conversion, processing_mode)
            items.append(item)
            if manifest_path is not None:
                await _write_manifest(manifest_path, ImportResult(items=tuple(items)))
        result = ImportResult(items=tuple(items))
        if manifest_path is not None:
            await _write_manifest(manifest_path, result)
        return result

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


async def write_import_report(path: Path, result: ImportResult) -> None:
    """Write an import result JSON report."""
    payload = json.dumps(result.to_json_dict(), indent=2)
    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_text, payload, encoding="utf-8")


async def _load_manifest(path: Path | None) -> dict[str, Mapping[str, object]]:
    if path is None or not await asyncio.to_thread(path.exists):
        return {}
    payload = cast(
        dict[str, object],
        json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8")),
    )
    items_obj = payload.get("items", [])
    if not isinstance(items_obj, list):
        return {}
    items = cast(list[object], items_obj)
    records: dict[str, Mapping[str, object]] = {}
    for item_obj in items:
        if isinstance(item_obj, dict):
            item = cast(Mapping[str, object], item_obj)
            source_path = item.get("source_path")
            if isinstance(source_path, str):
                records[source_path] = item
    return records


async def _write_manifest(path: Path, result: ImportResult) -> None:
    await write_import_report(path, result)


def _can_resume(
    previous: Mapping[str, object],
    processing_mode: ImportProcessingMode,
) -> bool:
    status = previous.get("status")
    if status in {"failed", None}:
        return False
    if processing_mode == ImportProcessingMode.NONE:
        return status in {"ingested", "processed", "queued", "skipped"}
    if processing_mode == ImportProcessingMode.PROCESS:
        return status == "processed"
    return status == "queued"


def _item_from_manifest(previous: Mapping[str, object]) -> ImportItem:
    source_path = Path(str(previous["source_path"]))
    converted = previous.get("converted_path")
    document_id = previous.get("document_id")
    run_id = previous.get("run_id")
    return ImportItem(
        source_path=source_path,
        converted_path=Path(str(converted)) if converted else None,
        document_id=DocumentId(str(document_id)) if document_id else None,
        run_id=RunId(str(run_id)) if run_id else None,
        status="skipped",
        error=None,
    )
