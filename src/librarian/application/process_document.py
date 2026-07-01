"""Application service for document processing runs."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any

from librarian.application.assemble_document import assemble_cleaned_document
from librarian.application.classify_document import ClassifyDocument
from librarian.application.clean_chunks import CleanChunks
from librarian.application.ingest_document import raw_text_key
from librarian.application.ports import (
    ApplicationMetrics,
    ChunkRepository,
    ContentStore,
    DocumentRepository,
    EventSink,
    OutputRepository,
    RunRepository,
)
from librarian.domain.ids import DocumentId, RunId
from librarian.domain.models import (
    CleanedOutput,
    DocumentStatus,
    ProcessingRun,
    RunStage,
    RunStatus,
)
from librarian.observability import NoOpMetricsRecorder, sanitize_error_message, start_span
from librarian.pipeline.chunking import ChunkingPolicy, chunk_text

logger = logging.getLogger("librarian.application.process_document")


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
                raw_text = await self.content.get_text(raw_text_key(document_id))
            await self.events.emit(run_id, RunStage.EXTRACT, "loaded extracted source text")
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
                cached_chunks = await self.outputs.get_cached_cleaned_chunks(
                    chunked,
                    prompt_version=self.cleaner.prompt_version,
                    model_provider=self.cleaner.provider.name,
                    model_name=self.cleaner.model,
                )
                cached_ids = {chunk.chunk.id for chunk in cached_chunks}
                missing_chunks = [chunk for chunk in chunked if chunk.id not in cached_ids]
                # Persist live per-chunk progress so clients can render a
                # real bar during long cleans; cache hits count immediately.
                progress = {"completed": len(cached_chunks)}
                await self.runs.update_run_progress(
                    run_id,
                    completed_chunks=progress["completed"],
                    failed_chunks=0,
                    stage=RunStage.CLEAN,
                )

                async def _note_chunk_cleaned() -> None:
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
                # Never cache an empty/blank cleaning result: doing so would
                # permanently rehydrate lost content on every future re-run.
                cacheable = [chunk for chunk in cleaned_missing if chunk.text.strip()]
                await self.outputs.save_cleaned_chunk_cache(
                    cacheable,
                    prompt_version=self.cleaner.prompt_version,
                    model_provider=self.cleaner.provider.name,
                    model_name=self.cleaner.model,
                )
                cleaned_chunks = sorted(
                    [*cached_chunks, *cleaned_missing],
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
                f"({len(cached_chunks)} cache hit(s))",
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
