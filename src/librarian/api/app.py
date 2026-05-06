"""FastAPI adapter."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, cast

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from librarian.application.convert_document import (
    ConversionFormat,
    DirectoryOutputMode,
    DocumentConverter,
)
from librarian.application.export_document import ExportedDocument
from librarian.application.factory import build_container
from librarian.application.import_library import ImportLibrary, ImportProcessingMode
from librarian.application.jobs import InProcessJobRunner
from librarian.config import Settings
from librarian.domain.ids import DocumentId, RunId
from librarian.domain.models import Document, ProcessingRun, RunStage, RunStatus
from librarian.ingest.extractors import CompositeExtractor
from librarian.observability import MetricsRecorder, configure_logging
from librarian.storage.sqlite import SQLiteRunQueue
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
    total: int
    limit: int
    offset: int


class ErrorResponse(BaseModel):
    detail: str


class RunRequest(BaseModel):
    document_id: str


class ImportRequest(BaseModel):
    source_dir: str
    format: str = "md"
    output_mode: str = "subdirectory"
    output_dir: str | None = None
    subdirectory_name: str = "librarian-converted"
    recursive: bool = False
    overwrite: bool = False
    processing_mode: str = "process"


class ImportResponse(BaseModel):
    converted: int
    ingested: int
    processed: int
    queued: int
    skipped: int
    failed: int
    items: list[dict[str, object]]


class RunResponse(BaseModel):
    id: str
    document_id: str
    status: str
    stage: str
    total_chunks: int
    completed_chunks: int
    failed_chunks: int
    error: str | None = None


class RunsResponse(BaseModel):
    runs: list[RunResponse]
    limit: int
    offset: int


class ContentResponse(BaseModel):
    document_id: str
    text: str


class SearchRequest(BaseModel):
    query: str
    limit: int = Field(default=20, ge=1, le=500)


class SearchResponse(BaseModel):
    document_ids: list[str]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the Librarian API app."""
    settings = settings or Settings()
    _validate_api_security(settings)
    configure_logging(level=settings.log_level, log_format=settings.log_format)
    logger = logging.getLogger("librarian.api")
    taxonomy = DeweyTaxonomy()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        runner = InProcessJobRunner(max_concurrency=settings.job_max_concurrency)
        app.state.job_runner = runner
        app.state.metrics = MetricsRecorder()
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

    @app.middleware("http")
    async def request_observability(request: Request, call_next: Any):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = int(response.status_code)
            response.headers["x-request-id"] = request_id
            return response
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            metrics = getattr(request.app.state, "metrics", None)
            if isinstance(metrics, MetricsRecorder) and settings.metrics_enabled:
                metrics.record(status_code=status_code, duration_ms=duration_ms)
            logger.info(
                "http_request",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                    "duration_ms": round(duration_ms, 3),
                },
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
        destination = upload_dir / _unique_upload_filename(file.filename or "upload.txt")
        await _write_limited_upload(file, destination, max_bytes=settings.api_max_upload_bytes)
        try:
            ingested = await container.ingest_document.execute(destination)
        except Exception as exc:
            await _cleanup_upload(destination)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _document_response(ingested.document)

    @app.get("/metrics")
    async def metrics(request: Request) -> dict[str, object]:
        current = getattr(request.app.state, "metrics", None)
        if not isinstance(current, MetricsRecorder) or not settings.metrics_enabled:
            raise HTTPException(status_code=404, detail="Metrics disabled")
        return current.snapshot()

    @app.get("/documents", response_model=DocumentsResponse)
    async def list_documents(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> DocumentsResponse:
        container = await build_container(settings)
        documents = list(await container.repository.list())
        page = documents[offset : offset + limit]
        return DocumentsResponse(
            documents=[_document_response(document) for document in page],
            total=len(documents),
            limit=limit,
            offset=offset,
        )

    @app.get("/documents/{document_id}", response_model=DocumentResponse)
    async def get_document(document_id: str) -> DocumentResponse:
        container = await build_container(settings)
        document = await container.repository.get_document(DocumentId(document_id))
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return _document_response(document)

    @app.delete("/documents/{document_id}")
    async def delete_document(document_id: str) -> dict[str, str]:
        container = await build_container(settings)
        document = await container.repository.get_document(DocumentId(document_id))
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        await container.repository.delete_document(DocumentId(document_id))
        await _cleanup_owned_upload(settings, document.source.path)
        return {"status": "deleted", "document_id": document_id}

    @app.post("/documents/{document_id}/reprocess", response_model=RunResponse)
    async def reprocess_document(document_id: str, http_request: Request) -> RunResponse:
        container = await build_container(settings)
        document = await container.repository.get_document(DocumentId(document_id))
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        run = await container.process_document.start(DocumentId(document_id))
        await _submit_run(settings, http_request, run.id)
        return _run_response(run)

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
        await _submit_run(settings, http_request, run.id)
        return _run_response(run)

    @app.get("/runs", response_model=RunsResponse)
    async def list_runs(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> RunsResponse:
        container = await build_container(settings)
        runs = await container.repository.list_runs(limit=limit, offset=offset)
        return RunsResponse(
            runs=[_run_response(run) for run in runs],
            limit=limit,
            offset=offset,
        )

    @app.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(run_id: str) -> RunResponse:
        container = await build_container(settings)
        run = await container.repository.get_run(RunId(run_id))
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return _run_response(run)

    @app.post("/runs/{run_id}/cancel", response_model=RunResponse)
    async def cancel_run(run_id: str) -> RunResponse:
        container = await build_container(settings)
        run = await container.repository.get_run(RunId(run_id))
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}:
            raise HTTPException(status_code=400, detail="Run is terminal and cannot be canceled")
        await container.repository.update_status(
            run.id,
            status=RunStatus.CANCELED,
            stage=RunStage.COMPLETE,
            error="canceled by user",
        )
        if settings.job_backend == "sqlite":
            await SQLiteRunQueue(container.database).cancel(run.id, error="canceled by user")
        latest = await container.repository.get_run(run.id)
        if latest is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return _run_response(latest)

    @app.post("/runs/{run_id}/retry", response_model=RunResponse)
    async def retry_run(run_id: str, http_request: Request) -> RunResponse:
        container = await build_container(settings)
        run = await container.repository.get_run(RunId(run_id))
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        if run.status != RunStatus.FAILED:
            raise HTTPException(status_code=400, detail="Run is not failed")
        retry = await container.process_document.start(run.document_id)
        await _submit_run(settings, http_request, retry.id)
        return _run_response(retry)

    @app.post("/imports", response_model=ImportResponse)
    async def import_documents(request: ImportRequest) -> ImportResponse:
        if settings.api_import_root is None:
            raise HTTPException(status_code=400, detail="API import root is not configured")
        container = await build_container(settings)
        try:
            conversion_format = ConversionFormat(request.format)
            output_mode = DirectoryOutputMode(request.output_mode)
            processing_mode = ImportProcessingMode(request.processing_mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _validate_subdirectory_name(request.subdirectory_name)
        source_dir = _resolve_api_path(Path(request.source_dir), settings=settings)
        if not source_dir.is_dir():
            raise HTTPException(status_code=400, detail="source_dir must be an existing directory")
        output_dir = (
            _resolve_api_path(Path(request.output_dir), settings=settings)
            if request.output_dir
            else None
        )
        if output_mode == DirectoryOutputMode.NEW_DIRECTORY and output_dir is None:
            raise HTTPException(
                status_code=400,
                detail="output_dir is required when output_mode is new-directory",
            )
        if processing_mode == ImportProcessingMode.QUEUE and settings.job_backend != "sqlite":
            raise HTTPException(
                status_code=400,
                detail="queue processing requires LIBRARIAN_JOB_BACKEND=sqlite",
            )
        importer = ImportLibrary(
            converter=DocumentConverter(_build_extractor(settings)),
            ingest=container.ingest_document,
            process=container.process_document,
            queue_factory=lambda: SQLiteRunQueue(container.database),
        )
        result = await importer.import_directory(
            source_dir,
            format=conversion_format,
            output_mode=output_mode,
            processing_mode=processing_mode,
            output_dir=output_dir,
            subdirectory_name=request.subdirectory_name,
            recursive=request.recursive,
            overwrite=request.overwrite,
        )
        items = cast(list[dict[str, object]], result.to_json_dict()["items"])
        return ImportResponse(
            converted=result.converted,
            ingested=result.ingested,
            processed=result.processed,
            queued=result.queued,
            skipped=result.skipped,
            failed=result.failed,
            items=items,
        )

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
        try:
            results = await container.repository.search(request.query, limit=request.limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return SearchResponse(document_ids=[str(document_id) for document_id in results])

    @app.get("/config")
    async def config() -> dict[str, str | int | float | bool | None]:
        return {
            "data_dir": str(settings.data_dir),
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "llm_base_url": settings.llm_base_url,
            "llm_api_key_env": settings.llm_api_key_env,
            "llm_timeout_seconds": settings.llm_timeout_seconds,
            "llm_max_concurrency": settings.llm_max_concurrency,
            "llm_max_retries": settings.llm_max_retries,
            "llm_retry_base_delay_seconds": settings.llm_retry_base_delay_seconds,
            "llm_retry_max_delay_seconds": settings.llm_retry_max_delay_seconds,
            "job_backend": settings.job_backend,
            "job_max_concurrency": settings.job_max_concurrency,
            "job_worker_id": settings.job_worker_id,
            "job_lease_seconds": settings.job_lease_seconds,
            "job_max_attempts": settings.job_max_attempts,
            "api_import_root": str(settings.api_import_root) if settings.api_import_root else None,
            "api_max_upload_bytes": settings.api_max_upload_bytes,
            "coherence_mode": settings.coherence_mode,
            "chunk_target_chars": settings.chunk_target_chars,
            "chunk_overlap_chars": settings.chunk_overlap_chars,
            "ocr_language": settings.ocr_language,
            "ocr_timeout_seconds": settings.ocr_timeout_seconds,
            "ocr_pdf_dpi": settings.ocr_pdf_dpi,
            "ocr_pdf_max_pages": settings.ocr_pdf_max_pages,
            "universal_max_input_bytes": settings.universal_max_input_bytes,
            "universal_timeout_seconds": settings.universal_timeout_seconds,
            "log_level": settings.log_level,
            "log_format": settings.log_format,
            "metrics_enabled": settings.metrics_enabled,
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
    safe = Path(filename).name.replace("/", "_").replace("\\", "_").strip()
    if safe in {".", ".."}:
        return "upload.txt"
    return safe or "upload.txt"


def _unique_upload_filename(filename: str) -> str:
    return str(Path(uuid.uuid4().hex) / _safe_filename(filename))


async def _write_limited_upload(file: UploadFile, destination: Path, *, max_bytes: int) -> None:
    await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
    total = 0
    try:
        with destination.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail="Upload exceeds configured size limit",
                    )
                await asyncio.to_thread(handle.write, chunk)
    except Exception:
        await _cleanup_upload(destination)
        raise


async def _cleanup_upload(destination: Path) -> None:
    await asyncio.to_thread(destination.unlink, missing_ok=True)
    try:
        await asyncio.to_thread(destination.parent.rmdir)
    except OSError:
        pass


async def _cleanup_owned_upload(settings: Settings, path: Path) -> None:
    upload_root = await asyncio.to_thread(_resolve_path, settings.data_dir / "uploads")
    try:
        resolved = await asyncio.to_thread(_resolve_path, path)
    except OSError:
        return
    try:
        resolved.relative_to(upload_root)
    except ValueError:
        return
    await _cleanup_upload(resolved)


def _resolve_path(path: Path) -> Path:
    return path.expanduser().resolve()


def _validate_subdirectory_name(name: str) -> None:
    path = Path(name)
    unsafe_part = any(part in {"", ".", ".."} for part in path.parts)
    if path.is_absolute() or not path.parts or unsafe_part:
        raise HTTPException(
            status_code=400,
            detail="subdirectory_name must be a relative safe path",
        )


def _build_extractor(settings: Settings) -> CompositeExtractor:
    return CompositeExtractor(
        ocr_language=settings.ocr_language,
        ocr_timeout_seconds=settings.ocr_timeout_seconds,
        ocr_pdf_dpi=settings.ocr_pdf_dpi,
        ocr_pdf_max_pages=settings.ocr_pdf_max_pages,
        universal_max_input_bytes=settings.universal_max_input_bytes,
        universal_timeout_seconds=settings.universal_timeout_seconds,
    )


def _is_public_bind(host: str) -> bool:
    return host in {"0.0.0.0", "::", "[::]"}  # noqa: S104


def _validate_api_security(settings: Settings) -> None:
    if not _is_public_bind(settings.api_host):
        return
    if not settings.api_key:
        raise RuntimeError("LIBRARIAN_API_KEY is required when binding the API publicly")
    if settings.api_import_root is None:
        raise RuntimeError("LIBRARIAN_API_IMPORT_ROOT is required when binding the API publicly")


def _resolve_api_path(path: Path, *, settings: Settings) -> Path:
    resolved = path.expanduser().resolve()
    if settings.api_import_root is None:
        raise HTTPException(status_code=400, detail="API import root is not configured")
    root = settings.api_import_root.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        detail = f"Path must be under import root: {root}"
        raise HTTPException(status_code=400, detail=detail) from exc
    return resolved


async def _execute_run(settings: Settings, run_id: RunId) -> None:
    container = await build_container(settings)
    await container.process_document.execute_existing(run_id)


async def _submit_run(settings: Settings, request: Request, run_id: RunId) -> None:
    if settings.job_backend == "in-process":
        runner = _job_runner(request)
        await runner.submit(run_id, lambda: _execute_run(settings, run_id))
        return
    if settings.job_backend == "sqlite":
        container = await build_container(settings)
        await SQLiteRunQueue(container.database).enqueue(run_id)
        return
    raise RuntimeError(f"Unsupported job backend: {settings.job_backend}")


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
