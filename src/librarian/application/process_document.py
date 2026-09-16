"""Application service for document processing runs."""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, cast

from librarian.application.assemble_document import assemble_cleaned_document
from librarian.application.classify_document import ClassifyDocument
from librarian.application.clean_chunks import CleanChunks, CleanedChunk
from librarian.application.ingest_document import IngestDocument, raw_text_key
from librarian.application.ports import (
    ApplicationMetrics,
    ChunkRepository,
    ContentStore,
    DocumentRepository,
    EventSink,
    OutputRepository,
    RunRepository,
)
from librarian.application.quality_report import (
    audit_final_document,
    extraction_report_key,
    run_quality_key,
)
from librarian.domain.ids import DocumentId, RunId
from librarian.domain.models import (
    Chunk,
    CleanedOutput,
    DocumentStatus,
    ProcessingRun,
    RunStage,
    RunStatus,
)
from librarian.observability import NoOpMetricsRecorder, sanitize_error_message, start_span
from librarian.pipeline.chunking import ChunkingPolicy, chunk_text
from librarian.pipeline.validation import PAGE_BREAK_REGEX

logger = logging.getLogger("librarian.application.process_document")


def select_pdf_chunks_for_cleaning(
    chunks: list[Chunk], source: str, report: object
) -> tuple[list[Chunk], list[CleanedChunk]]:
    """Preserve verified PDF text and send uncertain pages through the model."""
    if not isinstance(report, dict):
        return chunks, []
    typed_report = cast("dict[str, object]", report)
    if typed_report.get("engine") != "liteparse":
        return chunks, []
    raw_pages = typed_report.get("pages")
    boundaries = [match.start() for match in PAGE_BREAK_REGEX.finditer(source)]
    if not isinstance(raw_pages, list):
        return chunks, []
    pages = cast("list[object]", raw_pages)
    if len(pages) != len(boundaries) + 1:
        return chunks, []
    needing_cleaning: list[Chunk] = []
    preserved: list[CleanedChunk] = []
    for chunk in chunks:
        first = bisect.bisect_right(boundaries, chunk.start_char)
        last = bisect.bisect_left(boundaries, chunk.end_char)
        touched = pages[first : last + 1]
        safe = bool(touched) and all(_verified_pdf_page(page) for page in touched)
        if safe:
            preserved.append(
                CleanedChunk(
                    chunk=chunk,
                    text=chunk.text.strip(),
                    warnings=("verified-source-preserved",),
                )
            )
        else:
            needing_cleaning.append(chunk)
    return needing_cleaning, preserved


def _verified_pdf_page(page: object) -> bool:
    if not isinstance(page, dict):
        return False
    page = cast("dict[str, object]", page)
    coverage = page.get("native_text_coverage")
    return (
        isinstance(coverage, (int, float))
        and coverage >= 0.9
        and page.get("action") not in {"native-text-fallback", "vector-diagram-snapshot"}
        and page.get("kind") not in {"contents", "figure-list", "index"}
        and page.get("warnings") == []
    )


class ProcessingCanceled(RuntimeError):
    """Raised when a run is canceled while work is in progress."""


@dataclass(frozen=True, slots=True)
class ProcessDocument:
    """Run the Librarian pipeline for an ingested document."""

    documents: DocumentRepository
    runs: RunRepository
    chunks: ChunkRepository
    content: ContentStore
    outputs: OutputRepository
    events: EventSink
    cleaner: CleanChunks
    classifier: ClassifyDocument
    chunking_policy: ChunkingPolicy
    ingest_document: IngestDocument | None = None
    metrics: ApplicationMetrics = field(default_factory=NoOpMetricsRecorder)
    tracer: Any | None = None

    async def start(self, document_id: DocumentId) -> ProcessingRun:
        """Create a queued run without executing it."""
        document = await self.documents.get_document(document_id)
        if document is None:
            raise ValueError(f"Document not found: {document_id}")

        run_id = RunId(f"run_{uuid.uuid4().hex[:16]}")
        run = ProcessingRun(id=run_id, document_id=document_id)
        await self.runs.save_run(run)
        try:
            await self.events.emit(run_id, RunStage.INGEST, "queued processing run")
        except Exception as exc:
            await self.runs.update_status(
                run_id,
                status=RunStatus.FAILED,
                stage=RunStage.COMPLETE,
                error=sanitize_error_message(exc),
            )
            raise
        return run

    async def execute(self, document_id: DocumentId) -> ProcessingRun:
        """Create and execute a run."""
        run = await self.start(document_id)
        return await self.execute_existing(run.id)

    async def execute_existing(self, run_id: RunId) -> ProcessingRun:
        """Execute an existing queued run."""
        existing = await self.runs.get_run(run_id)
        if existing is None:
            raise ValueError(f"Run not found: {run_id}")
        if existing.status in {RunStatus.CANCELED, RunStatus.SUCCEEDED}:
            raise ValueError(f"Run is terminal and cannot be executed: {run_id}")
        document_id = existing.document_id
        document = await self.documents.get_document(document_id)
        if document is None:
            raise ValueError(f"Document not found: {document_id}")
        previous_document_status = document.status

        published = False

        try:
            await self.events.emit(run_id, RunStage.INGEST, "started processing run")
            async with self._timed_stage(RunStage.INGEST, run_id, document_id):
                await self.documents.update_document_status(document_id, DocumentStatus.PROCESSING)
                await self._raise_if_canceled(run_id)
            async with self._timed_stage(RunStage.EXTRACT, run_id, document_id):
                await self.runs.update_status(
                    run_id,
                    status=RunStatus.RUNNING,
                    stage=RunStage.EXTRACT,
                )
                extraction_refreshed = (
                    await self.ingest_document.ensure_current(document)
                    if self.ingest_document is not None
                    else False
                )
                raw_text = await self.content.get_text(raw_text_key(document_id))
            await self.events.emit(
                run_id,
                RunStage.EXTRACT,
                "refreshed stale extracted source text"
                if extraction_refreshed
                else "loaded extracted source text",
            )
            await self._raise_if_canceled(run_id)
            async with self._timed_stage(RunStage.NORMALIZE, run_id, document_id):
                await self.runs.update_status(
                    run_id,
                    status=RunStatus.RUNNING,
                    stage=RunStage.NORMALIZE,
                )
                normalized_text = raw_text.strip()
            await self.events.emit(run_id, RunStage.NORMALIZE, "normalized source text")
            await self._raise_if_canceled(run_id)
            async with self._timed_stage(RunStage.CHUNK, run_id, document_id):
                await self.runs.update_status(
                    run_id,
                    status=RunStatus.RUNNING,
                    stage=RunStage.CHUNK,
                )
                chunked = chunk_text(document_id, normalized_text, self.chunking_policy)
                await self.chunks.save_many(chunked)
            await self.events.emit(run_id, RunStage.CHUNK, f"created {len(chunked)} chunk(s)")
            await self._raise_if_canceled(run_id)

            async with self._timed_stage(RunStage.CLEAN, run_id, document_id):
                run = ProcessingRun(
                    id=run_id,
                    document_id=document_id,
                    status=RunStatus.RUNNING,
                    stage=RunStage.CLEAN,
                    total_chunks=len(chunked),
                )
                await self.runs.save_run(run)
                await self._raise_if_canceled(run_id)
                extraction_report: object = None
                if document.source.filename.lower().endswith(".pdf"):
                    try:
                        extraction_report = json.loads(
                            await self.content.get_text(extraction_report_key(document_id))
                        )
                    except (KeyError, ValueError):
                        pass
                model_chunks, verified_chunks = select_pdf_chunks_for_cleaning(
                    chunked, normalized_text, extraction_report
                )
                loaded_cached_chunks = await self.outputs.get_cached_cleaned_chunks(
                    model_chunks,
                    prompt_version=self.cleaner.prompt_version,
                    model_provider=self.cleaner.provider.name,
                    model_name=self.cleaner.model,
                )
                cached_chunks = [self.cleaner.revalidate(chunk) for chunk in loaded_cached_chunks]
                if cached_chunks != loaded_cached_chunks:
                    # Upgrade stale cache entries to the current validation
                    # policy once, then reuse the safe result on later runs.
                    await self.outputs.save_cleaned_chunk_cache(
                        cached_chunks,
                        prompt_version=self.cleaner.prompt_version,
                        model_provider=self.cleaner.provider.name,
                        model_name=self.cleaner.model,
                    )
                cached_ids = {chunk.chunk.id for chunk in cached_chunks}
                missing_chunks = [chunk for chunk in model_chunks if chunk.id not in cached_ids]
                # Persist live per-chunk progress so clients can render a
                # real bar during long cleans; cache hits count immediately.
                progress = {"completed": len(cached_chunks) + len(verified_chunks)}
                await self.runs.update_run_progress(
                    run_id,
                    completed_chunks=progress["completed"],
                    failed_chunks=0,
                    stage=RunStage.CLEAN,
                )

                async def _note_chunk_cleaned(cleaned_chunk: CleanedChunk) -> None:
                    # A later chunk may fail or the app may stop. Persist each
                    # successful result before reporting progress so a retry
                    # can reuse the work already paid for.
                    await self.outputs.save_cleaned_chunks(run_id, [cleaned_chunk])
                    if cleaned_chunk.text.strip():
                        await self.outputs.save_cleaned_chunk_cache(
                            [cleaned_chunk],
                            prompt_version=self.cleaner.prompt_version,
                            model_provider=self.cleaner.provider.name,
                            model_name=self.cleaner.model,
                        )
                    progress["completed"] += 1
                    await self.runs.update_run_progress(
                        run_id,
                        completed_chunks=progress["completed"],
                        failed_chunks=0,
                        stage=RunStage.CLEAN,
                    )

                cleaned_missing = await self.cleaner.execute(
                    missing_chunks, on_chunk_cleaned=_note_chunk_cleaned
                )
                await self._raise_if_canceled(run_id)
                cleaned_chunks = sorted(
                    [*verified_chunks, *cached_chunks, *cleaned_missing],
                    key=lambda item: item.chunk.ordinal,
                )
            async with self._timed_stage(RunStage.VALIDATE, run_id, document_id):
                await self.runs.update_status(
                    run_id,
                    status=RunStatus.RUNNING,
                    stage=RunStage.VALIDATE,
                )
                await self.outputs.save_cleaned_chunks(run_id, cleaned_chunks)
                failed_chunks = sum(1 for chunk in cleaned_chunks if not chunk.text.strip())
                completed_chunks = len(cleaned_chunks) - failed_chunks
                fidelity_fallbacks = sum(
                    "source-preserved-after-fidelity-check" in chunk.warnings
                    for chunk in cleaned_chunks
                )
            await self._raise_if_canceled(run_id)
            await self.runs.update_run_progress(
                run_id,
                completed_chunks=completed_chunks,
                failed_chunks=failed_chunks,
                stage=RunStage.VALIDATE,
            )
            await self.events.emit(
                run_id,
                RunStage.CLEAN,
                f"cleaned {completed_chunks}/{len(chunked)} chunk(s) "
                f"({len(cached_chunks)} cache hit(s), "
                f"{len(verified_chunks)} verified source chunk(s))",
            )
            if fidelity_fallbacks:
                await self.events.emit(
                    run_id,
                    RunStage.VALIDATE,
                    f"preserved source text for {fidelity_fallbacks} chunk(s) that failed "
                    "fidelity checks",
                )
            await self._raise_if_canceled(run_id)

            async with self._timed_stage(RunStage.ASSEMBLE, run_id, document_id):
                await self.runs.update_status(
                    run_id,
                    status=RunStatus.RUNNING,
                    stage=RunStage.ASSEMBLE,
                )
                assembled = assemble_cleaned_document(cleaned_chunks)
            # Guard against publishing an empty document as a success: if every
            # chunk cleaned to nothing (truncation, refusal, provider outage),
            # fail loudly instead of silently shipping a blank output.
            if chunked and not assembled.strip():
                raise ValueError(
                    "cleaning produced no output for a non-empty document "
                    f"({len(chunked)} chunk(s) all blank) — refusing to publish empty result"
                )
            await self._raise_if_canceled(run_id)
            output = CleanedOutput(
                document_id=document_id,
                run_id=run_id,
                text=assembled,
                prompt_version=self.cleaner.prompt_version,
                model_provider=self.cleaner.provider.name,
                model_name=self.cleaner.model,
            )
            async with self._timed_stage(RunStage.CLASSIFY, run_id, document_id):
                await self.runs.update_status(
                    run_id,
                    status=RunStatus.RUNNING,
                    stage=RunStage.CLASSIFY,
                )
                classification = await self.classifier.execute(
                    document_id,
                    assembled,
                    source_filename=document.source.filename,
                )
                final_quality = audit_final_document(
                    normalized_text, assembled, title=classification.title
                )
                await self.content.put_text(
                    run_quality_key(run_id),
                    json.dumps(
                        {
                            **final_quality,
                            "chunks": len(chunked),
                            "cleaned_chunks": completed_chunks,
                            "cached_cleaned_chunks": len(cached_chunks),
                            "verified_source_chunks": len(verified_chunks),
                            "source_preserved_chunks": fidelity_fallbacks,
                        },
                        ensure_ascii=False,
                    ),
                )
                fatal = final_quality["fatal"]
                if isinstance(fatal, list) and fatal:
                    raise ValueError(
                        "final document fidelity check failed: "
                        + ", ".join(cast("list[str]", fatal))
                    )
                warnings = final_quality["warnings"]
                warning_count = (
                    len(cast("list[str]", warnings)) if isinstance(warnings, list) else 0
                )
                await self.events.emit(
                    run_id,
                    RunStage.VALIDATE,
                    "quality report: "
                    f"{final_quality['pages'] or 1} page(s), "
                    f"{final_quality['image_references']} image reference(s), "
                    f"{warning_count} document warning(s)",
                )
            await self._raise_if_canceled(run_id)
            async with self._timed_stage(RunStage.INDEX, run_id, document_id):
                await self.runs.update_status(
                    run_id,
                    status=RunStatus.RUNNING,
                    stage=RunStage.INDEX,
                )

                await self.outputs.publish_successful_run(output, classification)
            published = True
            self.metrics.record_run_finished(status=RunStatus.SUCCEEDED.value)
            latest = await self.runs.get_run(run_id)
            if latest is None:
                raise RuntimeError(f"Run disappeared after processing: {run_id}")
            return latest
        except ProcessingCanceled as exc:
            error_message = sanitize_error_message(exc)
            await self.runs.update_status(
                run_id,
                status=RunStatus.CANCELED,
                stage=RunStage.COMPLETE,
                error=error_message,
            )
            await self.documents.update_document_status(document_id, previous_document_status)
            await self.events.emit(
                run_id,
                RunStage.COMPLETE,
                f"processing canceled: {error_message}",
            )
            self.metrics.record_run_finished(status=RunStatus.CANCELED.value)
            raise
        except asyncio.CancelledError:
            if published:
                raise
            await self.runs.update_status(
                run_id,
                status=RunStatus.FAILED,
                stage=RunStage.COMPLETE,
                error="processing canceled by task cancellation",
            )
            previous_output = await self.outputs.get_cleaned_output(document_id)
            canceled_status = (
                previous_document_status if previous_output is not None else DocumentStatus.FAILED
            )
            await self.documents.update_document_status(document_id, canceled_status)
            with suppress(Exception):
                await self.events.emit(
                    run_id,
                    RunStage.COMPLETE,
                    "processing canceled by task cancellation",
                )
            self.metrics.record_run_finished(status=RunStatus.FAILED.value)
            raise
        except Exception as exc:
            if published:
                raise
            error_message = sanitize_error_message(exc)
            await self.runs.update_status(
                run_id,
                status=RunStatus.FAILED,
                stage=RunStage.COMPLETE,
                error=error_message,
            )
            previous_output = await self.outputs.get_cleaned_output(document_id)
            failed_status = (
                previous_document_status if previous_output is not None else DocumentStatus.FAILED
            )
            await self.documents.update_document_status(document_id, failed_status)
            with suppress(Exception):
                await self.events.emit(
                    run_id,
                    RunStage.COMPLETE,
                    f"processing failed: {error_message}",
                )
            self.metrics.record_run_finished(status=RunStatus.FAILED.value)
            raise

    async def _raise_if_canceled(self, run_id: RunId) -> None:
        if await self.runs.is_run_canceled(run_id):
            raise ProcessingCanceled(f"Run canceled: {run_id}")

    def _timed_stage(self, stage: RunStage, run_id: RunId, document_id: DocumentId):
        return _TimedRunStage(self.metrics, self.tracer, stage, run_id, document_id)


@dataclass(slots=True)
class _TimedRunStage:
    metrics: ApplicationMetrics
    tracer: Any | None
    stage: RunStage
    run_id: RunId
    document_id: DocumentId
    started_at: float = 0.0
    _span_context: Any | None = None
    _span: Any | None = None

    async def __aenter__(self) -> None:
        self.started_at = time.perf_counter()
        span_context = start_span(
            self.tracer,
            "librarian.run_stage",
            attributes={
                "librarian.run_id": str(self.run_id),
                "librarian.document_id": str(self.document_id),
                "librarian.stage": self.stage.value,
            },
        )
        self._span_context = span_context
        self._span = span_context.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        duration_ms = (time.perf_counter() - self.started_at) * 1000
        status = "failed" if exc_type else "succeeded"
        if self._span is not None:
            self._span.set_attribute("librarian.status", status)
            self._span.set_attribute("librarian.duration_ms", round(duration_ms, 3))
        if self._span_context is not None:
            self._span_context.__exit__(exc_type, exc, traceback)
        self.metrics.record_run_stage(stage=self.stage.value, duration_ms=duration_ms)
        logger.info(
            "run_stage_finished",
            extra={
                "run_id": str(self.run_id),
                "document_id": str(self.document_id),
                "stage": self.stage.value,
                "status": status,
                "duration_ms": round(duration_ms, 3),
            },
        )
