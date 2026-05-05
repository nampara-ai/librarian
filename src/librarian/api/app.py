"""FastAPI adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel

from librarian.application.factory import build_container
from librarian.config import Settings
from librarian.domain.ids import DocumentId, RunId
from librarian.domain.models import Document, ProcessingRun
from librarian.taxonomy.dewey import DeweyTaxonomy
from librarian.version import __version__


class HealthResponse(BaseModel):
    status: str


class VersionResponse(BaseModel):
    version: str


class ClassificationResponse(BaseModel):
    classifications: dict[str, str]


class DocumentResponse(BaseModel):
    id: str
    filename: str
    status: str
    byte_size: int


class DocumentsResponse(BaseModel):
    documents: list[DocumentResponse]


class RunRequest(BaseModel):
    document_id: str


class RunResponse(BaseModel):
    id: str
    document_id: str
    status: str
    stage: str
    total_chunks: int
    completed_chunks: int
    failed_chunks: int
    error: str | None = None


class ContentResponse(BaseModel):
    document_id: str
    text: str


class SearchRequest(BaseModel):
    query: str
    limit: int = 20


class SearchResponse(BaseModel):
    document_ids: list[str]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the Librarian API app."""
    settings = settings or Settings()
    taxonomy = DeweyTaxonomy()

    app = FastAPI(
        title="Librarian API",
        version=__version__,
        summary="Local-first corpus cleaner and organizer.",
    )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="healthy")

    @app.get("/version", response_model=VersionResponse)
    async def version() -> VersionResponse:
        return VersionResponse(version=__version__)

    @app.get("/classifications", response_model=ClassificationResponse)
    async def classifications() -> ClassificationResponse:
        return ClassificationResponse(classifications=taxonomy.all())

    @app.post("/documents", response_model=DocumentResponse)
    async def create_document(file: Annotated[UploadFile, File()]) -> DocumentResponse:
        container = await build_container(settings)
        upload_dir = settings.data_dir / "uploads"
        await asyncio.to_thread(upload_dir.mkdir, parents=True, exist_ok=True)
        destination = upload_dir / _safe_filename(file.filename or "upload.txt")
        payload = await file.read()
        await asyncio.to_thread(destination.write_bytes, payload)
        ingested = await container.ingest_document.execute(destination)
        return _document_response(ingested.document)

    @app.get("/documents", response_model=DocumentsResponse)
    async def list_documents() -> DocumentsResponse:
        container = await build_container(settings)
        documents = await container.repository.list()
        return DocumentsResponse(documents=[_document_response(document) for document in documents])

    @app.get("/documents/{document_id}", response_model=DocumentResponse)
    async def get_document(document_id: str) -> DocumentResponse:
        container = await build_container(settings)
        document = await container.repository.get_document(DocumentId(document_id))
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return _document_response(document)

    @app.get("/documents/{document_id}/content", response_model=ContentResponse)
    async def get_document_content(document_id: str) -> ContentResponse:
        container = await build_container(settings)
        output = await container.repository.get_cleaned_output(DocumentId(document_id))
        if output is None:
            raise HTTPException(status_code=404, detail="Cleaned output not found")
        return ContentResponse(document_id=document_id, text=output.text)

    @app.post("/runs", response_model=RunResponse)
    async def create_run(request: RunRequest, background_tasks: BackgroundTasks) -> RunResponse:
        container = await build_container(settings)
        try:
            run = await container.process_document.start(DocumentId(request.document_id))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        background_tasks.add_task(_execute_run, settings, run.id)
        return _run_response(run)

    @app.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(run_id: str) -> RunResponse:
        container = await build_container(settings)
        run = await container.repository.get_run(RunId(run_id))
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return _run_response(run)

    @app.get("/runs/{run_id}/events")
    async def get_run_events(run_id: str) -> dict[str, list[str]]:
        container = await build_container(settings)
        return {"events": list(await container.repository.list_events(RunId(run_id)))}

    @app.post("/search", response_model=SearchResponse)
    async def search(request: SearchRequest) -> SearchResponse:
        container = await build_container(settings)
        results = await container.repository.search(request.query, limit=request.limit)
        return SearchResponse(document_ids=[str(document_id) for document_id in results])

    @app.get("/config")
    async def config() -> dict[str, str | int | None]:
        return {
            "data_dir": str(settings.data_dir),
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "coherence_mode": settings.coherence_mode,
            "chunk_target_chars": settings.chunk_target_chars,
            "chunk_overlap_chars": settings.chunk_overlap_chars,
        }

    return app


def _document_response(document: Document) -> DocumentResponse:
    return DocumentResponse(
        id=str(document.id),
        filename=document.source.filename,
        status=document.status.value,
        byte_size=document.source.byte_size,
    )


def _run_response(run: ProcessingRun) -> RunResponse:
    return RunResponse(
        id=str(run.id),
        document_id=str(run.document_id),
        status=run.status.value,
        stage=run.stage.value,
        total_chunks=run.total_chunks,
        completed_chunks=run.completed_chunks,
        failed_chunks=run.failed_chunks,
        error=run.error,
    )


def _safe_filename(filename: str) -> str:
    return Path(filename).name.replace("/", "_").replace("\\", "_")


async def _execute_run(settings: Settings, run_id: RunId) -> None:
    container = await build_container(settings)
    await container.process_document.execute_existing(run_id)
