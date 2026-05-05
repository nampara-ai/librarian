"""FastAPI adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from librarian.application.export_document import ExportedDocument
from librarian.application.factory import build_container
from librarian.application.jobs import InProcessJobRunner
from librarian.config import Settings
from librarian.domain.ids import DocumentId, RunId
from librarian.domain.models import Document, ProcessingRun, RunStatus
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

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        runner = InProcessJobRunner(max_concurrency=settings.job_max_concurrency)
        app.state.job_runner = runner
        try:
            yield
        finally:
            await runner.shutdown()

    app = FastAPI(
        title="Librarian API",
        version=__version__,
        summary="Local-first corpus cleaner and organizer.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def api_key_auth(request: Request, call_next: Any):
        if settings.api_key and request.url.path not in {"/health", "/version"}:
            supplied = request.headers.get("x-api-key")
            if supplied != settings.api_key:
                return JSONResponse(status_code=401, content={"detail": "Invalid API key"})
        return await call_next(request)

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

    @app.get("/documents/{document_id}/export")
    async def export_document(document_id: str, format: str = "json"):
        container = await build_container(settings)
        document = await container.repository.get_document(DocumentId(document_id))
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        output = await container.repository.get_cleaned_output(DocumentId(document_id))
        if output is None:
            raise HTTPException(status_code=404, detail="Cleaned output not found")
        classification = await container.repository.get_classification(DocumentId(document_id))
        exported = ExportedDocument(
            document=document,
            output=output,
            classification=classification,
        )
        return _export_response(exported, format)

    @app.post("/runs", response_model=RunResponse)
    async def create_run(request: RunRequest, http_request: Request) -> RunResponse:
        container = await build_container(settings)
        try:
            run = await container.process_document.start(DocumentId(request.document_id))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        runner = _job_runner(http_request)
        await runner.submit(run.id, lambda: _execute_run(settings, run.id))
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

    @app.get("/runs/{run_id}/events/stream")
    async def stream_run_events(run_id: str) -> StreamingResponse:
        return StreamingResponse(
            _event_stream(settings, RunId(run_id)),
            media_type="text/event-stream",
        )

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


def _job_runner(request: Request) -> InProcessJobRunner:
    runner = request.app.state.job_runner
    if not isinstance(runner, InProcessJobRunner):
        raise RuntimeError("Job runner is not initialized")
    return runner


async def _event_stream(settings: Settings, run_id: RunId) -> AsyncIterator[str]:
    seen = 0
    while True:
        container = await build_container(settings)
        events = list(await container.repository.list_events(run_id))
        for event in events[seen:]:
            yield f"data: {event}\n\n"
        seen = len(events)
        run = await container.repository.get_run(run_id)
        if run is None or run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}:
            yield "event: done\ndata: done\n\n"
            break
        await asyncio.sleep(0.2)


def _export_response(payload: ExportedDocument, format: str):
    normalized = format.lower()
    if normalized == "json":
        return JSONResponse(json.loads(payload.render("json")))
    if normalized == "txt":
        return PlainTextResponse(payload.render("txt"))
    if normalized == "md":
        return PlainTextResponse(payload.render("md"), media_type="text/markdown")
    raise HTTPException(status_code=400, detail="Unsupported export format")
