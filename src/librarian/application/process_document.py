"""Application service for document processing runs."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from librarian.application.assemble_document import assemble_cleaned_document
from librarian.application.classify_document import ClassifyDocument
from librarian.application.clean_chunks import CleanChunks
from librarian.application.ingest_document import raw_text_key
from librarian.application.ports import (
    ChunkRepository,
    ContentStore,
    DocumentRepository,
    EventSink,
    OutputRepository,
    RunRepository,
    SearchIndex,
)
from librarian.domain.ids import DocumentId, RunId
from librarian.domain.models import (
    CleanedOutput,
    DocumentStatus,
    ProcessingRun,
    RunStage,
    RunStatus,
)
from librarian.pipeline.chunking import ChunkingPolicy, chunk_text


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
    search: SearchIndex
    events: EventSink
    cleaner: CleanChunks
    classifier: ClassifyDocument
    chunking_policy: ChunkingPolicy

    async def start(self, document_id: DocumentId) -> ProcessingRun:
        """Create a queued run without executing it."""
        document = await self.documents.get_document(document_id)
        if document is None:
            raise ValueError(f"Document not found: {document_id}")

        run_id = RunId(f"run_{uuid.uuid4().hex[:16]}")
        run = ProcessingRun(id=run_id, document_id=document_id)
        await self.runs.save_run(run)
        await self.events.emit(run_id, RunStage.INGEST, "queued processing run")
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

        await self.events.emit(run_id, RunStage.INGEST, "started processing run")
        await self.documents.update_document_status(document_id, DocumentStatus.PROCESSING)

        try:
            await self._raise_if_canceled(run_id)
            await self.runs.update_status(
                run_id,
                status=RunStatus.RUNNING,
                stage=RunStage.CHUNK,
            )
            raw_text = await self.content.get_text(raw_text_key(document_id))
            await self._raise_if_canceled(run_id)
            chunked = chunk_text(document_id, raw_text, self.chunking_policy)
            await self.chunks.save_many(chunked)
            await self.events.emit(run_id, RunStage.CHUNK, f"created {len(chunked)} chunk(s)")
            await self._raise_if_canceled(run_id)

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
            cleaned_missing = await self.cleaner.execute(missing_chunks)
            await self._raise_if_canceled(run_id)
            await self.outputs.save_cleaned_chunk_cache(
                cleaned_missing,
                prompt_version=self.cleaner.prompt_version,
                model_provider=self.cleaner.provider.name,
                model_name=self.cleaner.model,
            )
            cleaned_chunks = sorted(
                [*cached_chunks, *cleaned_missing],
                key=lambda item: item.chunk.ordinal,
            )
            await self.outputs.save_cleaned_chunks(run_id, cleaned_chunks)
            failed_chunks = sum(1 for chunk in cleaned_chunks if not chunk.text.strip())
            completed_chunks = len(cleaned_chunks) - failed_chunks
            await self._raise_if_canceled(run_id)
            await self.runs.update_run_progress(
                run_id,
                completed_chunks=completed_chunks,
                failed_chunks=failed_chunks,
                stage=RunStage.CLEAN,
            )
            await self.events.emit(
                run_id,
                RunStage.CLEAN,
                f"cleaned {completed_chunks}/{len(chunked)} chunk(s) "
                f"({len(cached_chunks)} cache hit(s))",
            )
            await self._raise_if_canceled(run_id)

            await self.runs.update_status(
                run_id,
                status=RunStatus.RUNNING,
                stage=RunStage.ASSEMBLE,
            )
            assembled = assemble_cleaned_document(cleaned_chunks)
            await self._raise_if_canceled(run_id)
            output = CleanedOutput(
                document_id=document_id,
                run_id=run_id,
                text=assembled,
                prompt_version=self.cleaner.prompt_version,
                model_provider=self.cleaner.provider.name,
                model_name=self.cleaner.model,
            )
            await self.outputs.save_cleaned_output(output)
            await self._raise_if_canceled(run_id)
            classification = await self.classifier.execute(document_id, assembled)
            await self._raise_if_canceled(run_id)

            await self.search.index(output, classification)
            await self.documents.update_document_status(document_id, DocumentStatus.READY)
            await self.outputs.save_classification(classification)
            await self.events.emit(run_id, RunStage.INDEX, "stored output and search index")
            await self._raise_if_canceled(run_id)

            await self.runs.update_status(
                run_id,
                status=RunStatus.SUCCEEDED,
                stage=RunStage.COMPLETE,
            )
            await self.events.emit(run_id, RunStage.COMPLETE, "processing complete")
            latest = await self.runs.get_run(run_id)
            if latest is None:
                raise RuntimeError(f"Run disappeared after processing: {run_id}")
            return latest
        except ProcessingCanceled as exc:
            await self.runs.update_status(
                run_id,
                status=RunStatus.CANCELED,
                stage=RunStage.COMPLETE,
                error=str(exc),
            )
            await self.documents.update_document_status(document_id, previous_document_status)
            await self.events.emit(run_id, RunStage.COMPLETE, f"processing canceled: {exc}")
            raise
        except Exception as exc:
            await self.runs.update_status(
                run_id,
                status=RunStatus.FAILED,
                stage=RunStage.COMPLETE,
                error=str(exc),
            )
            await self.documents.update_document_status(document_id, DocumentStatus.FAILED)
            await self.events.emit(run_id, RunStage.COMPLETE, f"processing failed: {exc}")
            raise

    async def _raise_if_canceled(self, run_id: RunId) -> None:
        if await self.runs.is_run_canceled(run_id):
            raise ProcessingCanceled(f"Run canceled: {run_id}")
