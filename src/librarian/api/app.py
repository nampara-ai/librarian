"""FastAPI adapter."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import secrets
import sqlite3
import tempfile
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from urllib.parse import quote, unquote

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from librarian.application.convert_document import (
    ConversionFormat,
    DirectoryOutputMode,
    DocumentConverter,
    conversion_output_exclusions,
    iter_supported_files,
)
from librarian.application.export_document import ExportedDocument, ExportFormat
from librarian.application.factory import build_container, build_ingest_container
from librarian.application.import_library import ImportLibrary, ImportProcessingMode
from librarian.application.jobs import InProcessJobRunner
from librarian.application.ports import SearchScope
from librarian.config import Settings
from librarian.domain.ids import DocumentId, RunId
from librarian.domain.models import (
    Document,
    DocumentStatus,
    ProcessingRun,
    RunEvent,
    RunStage,
    RunStatus,
)
from librarian.ingest.extractors import (
    ARCHIVE_EXTENSIONS,
    ZIP_CONTAINER_EXTENSIONS,
    CompositeExtractor,
    archive_signature_label,
)
from librarian.llm import LazyLLMProvider
from librarian.observability import (
    MetricsRecorder,
    TracingHandle,
    configure_logging,
    configure_tracing,
    sanitize_error_message,
    start_request_span,
)
from librarian.storage.sqlite import SQLiteDatabase, SQLiteRunQueue, normalize_search_query
from librarian.taxonomy.dewey import DeweyTaxonomy
from librarian.version import __version__

ApiKeyScope = Literal["read", "write"]
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cache-Control": "no-store",
}
_MAX_UPLOAD_FILENAME_BYTES = 255
_READ_SCOPE_RESTRICTED_PATHS = frozenset({"/config", "/metrics", "/metrics/prometheus"})
_OPENAPI_ERROR_STATUS_CODES = ("400", "401", "403", "404", "413", "422", "429", "500", "503")
@dataclass(frozen=True, slots=True)
class ApiCredential:
    """One configured API credential and its effective scope."""

    key: str
    scope: ApiKeyScope
    hashed: bool = False


class HealthResponse(BaseModel):
    status: str


class ReadinessResponse(BaseModel):
    status: str
    database: str
    storage: str
    applied_migrations: int


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
    detail: object
    code: str


class BatchDocumentItemResponse(BaseModel):
    filename: str
    status: str
    document: DocumentResponse | None = None
    error: ErrorResponse | None = None


class BatchDocumentsResponse(BaseModel):
    documents: list[BatchDocumentItemResponse]
    ingested: int
    failed: int


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
    manifest_path: str | None = None
    resume: bool = False


class ImportResponse(BaseModel):
    converted: int
    ingested: int
    processed: int
    queued: int
    skipped: int
    failed: int
    items: list[dict[str, object]]


class ImportStatusResponse(BaseModel):
    converted: int
    ingested: int
    processed: int
    queued: int
    skipped: int
    failed: int
    total: int
    limit: int
    offset: int
    items: list[dict[str, object]]


class PdfPageManifestPageResponse(BaseModel):
    page_number: int | None
    source: str | None
    status: str | None
    chars: int | None
    confidence: float | None
    corrected: bool
    attempts: int
    duration_ms: float | None
    image_path: str | None
    warnings: list[str]
    error: str | None


class PdfPageManifestStatusResponse(BaseModel):
    manifest_path: str
    source_sha256: str
    page_count: int
    statuses: dict[str, int]
    sources: dict[str, int]
    warnings: dict[str, int]
    corrected_pages: int
    attempts: int
    average_confidence: float | None
    failures_only: bool
    total: int
    limit: int
    offset: int
    pages: list[PdfPageManifestPageResponse]


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
    total_chars: int
    offset: int
    limit: int
    truncated: bool


class ExportDocumentResponse(BaseModel):
    document_id: str
    filename: str
    classification: str | None
    text: str


class SearchRequest(BaseModel):
    query: str
    limit: int = Field(default=20, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
    phrase: bool = False
    classification_code: str | None = None
    document_status: str | None = None
    filename_contains: str | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    scope: str = "cleaned"


class SearchResponse(BaseModel):
    document_ids: list[str]
    total: int
    limit: int
    offset: int


class SearchResultResponse(BaseModel):
    document_id: str
    run_id: str | None
    source: str
    filename: str
    document_status: str
    snippet: str
    score: float
    classification_code: str | None = None
    classification_label: str | None = None


class SearchResultsResponse(BaseModel):
    results: list[SearchResultResponse]
    total: int
    limit: int
    offset: int


class SearchFacetValueResponse(BaseModel):
    value: str
    count: int
    label: str | None = None


class SearchFacetsResponse(BaseModel):
    classifications: list[SearchFacetValueResponse]
    statuses: list[SearchFacetValueResponse]
    sources: list[SearchFacetValueResponse]
    filenames: list[SearchFacetValueResponse]


class DeleteDocumentResponse(BaseModel):
    status: str
    document_id: str


class RunEventsResponse(BaseModel):
    events: list[str]
    limit: int
    offset: int


class RunEventResponse(BaseModel):
    sequence: int
    stage: str
    message: str
    created_at: str


class RunEventRecordsResponse(BaseModel):
    events: list[RunEventResponse]
    limit: int
    offset: int


class MetricsResponse(BaseModel):
    uptime_seconds: float
    requests_total: int
    errors_total: int
    average_request_duration_ms: float
    status_counts: dict[str, int]
    run_stage_duration_ms_total: dict[str, float]
    run_stage_counts: dict[str, int]
    runs_completed_total: int
    runs_failed_total: int
    runs_canceled_total: int
    queue_claims_total: int
    queue_failures_total: int
    average_queue_wait_ms: float
    conversion_failures_total: int
    conversion_failures_by_type: dict[str, int]
    ocr_pages_total: int
    ocr_failures_total: int
    ocr_corrected_pages_total: int
    ocr_page_duration_ms_total: float
    ocr_pages_per_second: float
    ocr_pages_by_status: dict[str, int]
    llm_prompt_tokens_total: int
    llm_completion_tokens_total: int
    llm_tokens_total: int
    llm_estimated_cost_usd_total: float
    llm_tokens_by_model: dict[str, int]
    llm_estimated_cost_usd_by_model: dict[str, float]


class ConfigResponse(BaseModel):
    data_dir: str
    llm_provider: str
    llm_model: str
    llm_base_url: str | None
    llm_api_key_env: str
    llm_timeout_seconds: float
    llm_max_concurrency: int
    llm_max_retries: int
    llm_retry_base_delay_seconds: float
    llm_retry_max_delay_seconds: float
    llm_prompt_cost_per_1k_tokens_usd: float
    llm_completion_cost_per_1k_tokens_usd: float
    llm_max_prompt_chars: int
    llm_max_response_chars: int
    cleaning_prompt_version: str
    classification_prompt_version: str
    cleaning_mode: str
    job_backend: str
    job_max_concurrency: int
    job_worker_id: str
    job_lease_seconds: int
    job_max_attempts: int
    api_import_root: str | None
    api_max_request_bytes: int
    api_max_upload_bytes: int
    api_max_batch_files: int
    api_max_batch_bytes: int
    api_max_import_files: int
    api_max_import_bytes: int
    api_max_import_manifest_bytes: int
    api_max_content_chars: int
    api_rate_limit_per_minute: int
    api_trusted_proxy_cidrs: str | None
    api_audit_retention_days: int
    api_auth_keys_configured: int
    coherence_mode: str
    chunk_target_chars: int
    chunk_overlap_chars: int
    max_source_bytes: int
    text_max_input_bytes: int
    docx_max_input_bytes: int
    pdf_max_input_bytes: int
    pdf_max_pages: int
    ocr_language: str
    ocr_timeout_seconds: int
    ocr_pdf_dpi: int
    ocr_pdf_max_pages: int
    ocr_preprocess_mode: str
    ocr_threshold: int
    ocr_preserve_page_images: bool
    ocr_llm_correction: str
    ocr_llm_model: str | None
    ocr_low_confidence_threshold: float
    ocr_page_concurrency: int
    ocr_fail_on_page_error: bool
    universal_max_input_bytes: int
    universal_timeout_seconds: int
    log_level: str
    log_format: str
    metrics_enabled: bool
    otel_enabled: bool
    otel_service_name: str
    otel_endpoint: str | None


class FixedWindowRateLimiter:
    """Small per-process fixed-window request limiter."""

    def __init__(self, *, limit_per_minute: int, window_seconds: int = 60) -> None:
        self.limit_per_minute = limit_per_minute
        self.window_seconds = window_seconds
        self._buckets: dict[str, tuple[float, int]] = {}
        self._last_pruned_at = 0.0

    def allow(self, identity: str, *, now: float) -> tuple[bool, int]:
        """Return whether the request is allowed and the Retry-After seconds."""
        if self.limit_per_minute <= 0:
            return True, 0
        if now - self._last_pruned_at >= self.window_seconds:
            self._prune(now)
        window_started, count = self._buckets.get(identity, (now, 0))
        elapsed = now - window_started
        if elapsed >= self.window_seconds:
            self._prune(now)
            self._buckets[identity] = (now, 1)
            return True, 0
        if count >= self.limit_per_minute:
            retry_after = max(1, int(self.window_seconds - elapsed))
            return False, retry_after
        self._buckets[identity] = (window_started, count + 1)
        return True, 0

    @property
    def active_bucket_count(self) -> int:
        """Return the number of active identity buckets."""
        return len(self._buckets)

    def _prune(self, now: float) -> None:
        expired_before = now - self.window_seconds
        self._buckets = {
            identity: bucket
            for identity, bucket in self._buckets.items()
            if bucket[0] > expired_before
        }
        self._last_pruned_at = now


class RequestBodyLimitMiddleware:
    """ASGI middleware that caps streamed request bodies without Content-Length."""

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return
        if _has_content_length(scope):
            await self.app(scope, receive, send)
            return

        received_bytes = 0
        body_file = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
        try:
            more_body = True
            while more_body:
                message = await receive()
                if message.get("type") != "http.request":
                    await self.app(scope, _single_message_receive(message), send)
                    return
                body = message.get("body", b"")
                if isinstance(body, bytes):
                    received_bytes += len(body)
                    if received_bytes > self.max_bytes:
                        response = _with_security_headers(
                            JSONResponse(
                                status_code=413,
                                content={
                                    "detail": (
                                        f"Request body contains more than {self.max_bytes} bytes, "
                                        f"exceeding configured limit {self.max_bytes}"
                                    ),
                                    "code": "request_too_large",
                                },
                            )
                        )
                        await response(scope, receive, send)
                        return
                    body_file.write(body)
                more_body = bool(message.get("more_body", False))
            body_file.seek(0)

            async def replay_receive() -> dict[str, Any]:
                chunk = body_file.read(64 * 1024)
                if chunk:
                    return {
                        "type": "http.request",
                        "body": chunk,
                        "more_body": body_file.tell() < received_bytes,
                    }
                return {"type": "http.request", "body": b"", "more_body": False}

            await self.app(scope, replay_receive, send)
        finally:
            body_file.close()


def _has_content_length(scope: dict[str, Any]) -> bool:
    headers = scope.get("headers", [])
    return any(name.lower() == b"content-length" for name, _value in headers)


def _single_message_receive(first_message: dict[str, Any]) -> Any:
    consumed = False

    async def receive() -> dict[str, Any]:
        nonlocal consumed
        if not consumed:
            consumed = True
            return first_message
        return {"type": "http.disconnect"}

    return receive


def _install_openapi_docs(app: FastAPI, *, include_auth: bool) -> None:
    """Document API-wide auth and stable error responses in OpenAPI."""
    public_paths = {"/health", "/ready", "/version"}

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            summary=app.summary,
            description=app.description,
            routes=app.routes,
        )
        components_obj: object = schema.setdefault("components", {})
        if isinstance(components_obj, dict):
            components = cast(dict[str, object], components_obj)
            if include_auth:
                security_schemes_obj: object = components.setdefault("securitySchemes", {})
                if isinstance(security_schemes_obj, dict):
                    security_schemes = cast(dict[str, object], security_schemes_obj)
                    security_schemes["ApiKeyAuth"] = {
                        "type": "apiKey",
                        "in": "header",
                        "name": "x-api-key",
                    }
                    security_schemes["BearerAuth"] = {
                        "type": "http",
                        "scheme": "bearer",
                    }
        security_requirement: list[dict[str, list[str]]] = [
            {"ApiKeyAuth": []},
            {"BearerAuth": []},
        ]
        error_response = {
            "description": "Error response with stable machine-readable code.",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ErrorResponse"},
                },
            },
        }
        paths_obj: object = schema.get("paths", {})
        if isinstance(paths_obj, dict):
            paths = cast(dict[str, object], paths_obj)
            for path, methods_obj in paths.items():
                if not isinstance(methods_obj, dict):
                    continue
                methods = cast(dict[str, object], methods_obj)
                for operation_obj in methods.values():
                    if isinstance(operation_obj, dict):
                        operation = cast(dict[str, object], operation_obj)
                        if include_auth and path not in public_paths:
                            operation.setdefault("security", security_requirement)
                        responses_obj = operation.setdefault("responses", {})
                        if isinstance(responses_obj, dict):
                            responses = cast(dict[str, object], responses_obj)
                            for status_code in _OPENAPI_ERROR_STATUS_CODES:
                                responses[status_code] = error_response
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the Librarian API app."""
    settings = settings or Settings()
    _validate_api_security(settings)
    configure_logging(level=settings.log_level, log_format=settings.log_format)
    tracing = configure_tracing(
        enabled=settings.otel_enabled,
        service_name=settings.otel_service_name,
        endpoint=settings.otel_endpoint,
        headers=settings.otel_headers,
    )
    logger = logging.getLogger("librarian.api")
    taxonomy = DeweyTaxonomy()
    rate_limiter = FixedWindowRateLimiter(
        limit_per_minute=settings.api_rate_limit_per_minute,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        await SQLiteDatabase(settings.database_path).initialize()
        runner = InProcessJobRunner(max_concurrency=settings.job_max_concurrency)
        app.state.job_runner = runner
        app.state.settings = settings
        app.state.metrics = MetricsRecorder()
        app.state.tracing = tracing
        try:
            yield
        finally:
            await runner.shutdown()
            if isinstance(tracing, TracingHandle):
                tracing.shutdown()

    app = FastAPI(
        title="Librarian API",
        version=__version__,
        summary="Local-first corpus cleaner and organizer.",
        lifespan=lifespan,
    )
    _install_openapi_docs(app, include_auth=bool(_configured_api_credentials(settings)))
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=settings.api_max_request_bytes)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": exc.detail,
                "code": _api_error_code(status_code=exc.status_code, detail=exc.detail),
            },
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": exc.errors(), "code": "validation_error"},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled_api_exception",
            extra={
                "method": request.method,
                "path": request.url.path,
                "error": sanitize_error_message(exc),
            },
        )
        return _with_security_headers(
            JSONResponse(
                status_code=500,
                content={"detail": "Internal server error", "code": "server_error"},
            )
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any):
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            if name not in response.headers:
                response.headers[name] = value
        return response

    @app.middleware("http")
    async def request_size_limit(request: Request, call_next: Any):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                request_bytes = int(content_length)
            except ValueError:
                request_bytes = 0
            if request_bytes > settings.api_max_request_bytes:
                return _with_security_headers(
                    JSONResponse(
                        status_code=413,
                        content={
                            "detail": (
                                f"Request body contains {request_bytes} bytes, "
                                f"exceeding configured limit {settings.api_max_request_bytes}"
                            ),
                            "code": "request_too_large",
                        },
                    )
                )
        return await call_next(request)

    @app.middleware("http")
    async def api_key_auth(request: Request, call_next: Any):
        credentials = _configured_api_credentials(settings)
        if credentials and request.url.path not in {"/health", "/ready", "/version"}:
            supplied = _supplied_api_key(request)
            credential = _matched_api_credential(supplied, credentials) if supplied else None
            if credential is None:
                _log_api_security_event(
                    logger,
                    settings,
                    request,
                    event="api_auth_failed",
                    credential_present=supplied is not None,
                )
                await _record_api_audit_event(
                    settings,
                    request,
                    event="api_auth_failed",
                    credential_present=supplied is not None,
                )
                return _with_security_headers(
                    JSONResponse(
                        status_code=401,
                        content={"detail": "Invalid API key", "code": "invalid_api_key"},
                    )
                )
            if not _api_credential_allows(credential, request):
                _log_api_security_event(
                    logger,
                    settings,
                    request,
                    event="api_scope_denied",
                    credential_scope=credential.scope,
                )
                await _record_api_audit_event(
                    settings,
                    request,
                    event="api_scope_denied",
                    credential_scope=credential.scope,
                )
                return _with_security_headers(
                    JSONResponse(
                        status_code=403,
                        content={
                            "detail": "API key scope does not allow this operation",
                            "code": "insufficient_scope",
                        },
                    )
                )
        return await call_next(request)

    @app.middleware("http")
    async def rate_limit_requests(request: Request, call_next: Any):
        if (
            settings.api_rate_limit_per_minute <= 0
            or request.url.path in {"/health", "/ready", "/version"}
        ):
            return await call_next(request)
        allowed, retry_after_seconds = rate_limiter.allow(
            _rate_limit_identity(request, settings),
            now=time.monotonic(),
        )
        if not allowed:
            _log_api_security_event(
                logger,
                settings,
                request,
                event="api_rate_limited",
                retry_after_seconds=retry_after_seconds,
            )
            await _record_api_audit_event(
                settings,
                request,
                event="api_rate_limited",
                retry_after_seconds=retry_after_seconds,
            )
            return _with_security_headers(
                JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded", "code": "rate_limited"},
                    headers={"Retry-After": str(retry_after_seconds)},
                )
            )
        return await call_next(request)

    @app.middleware("http")
    async def request_observability(request: Request, call_next: Any):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        start = time.perf_counter()
        status_code = 500
        try:
            tracer = tracing.tracer if isinstance(tracing, TracingHandle) else None
            with start_request_span(
                tracer,
                method=request.method,
                path=request.url.path,
                request_id=request_id,
            ) as span:
                response = await call_next(request)
                status_code = int(response.status_code)
                span.set_attribute("http.response.status_code", status_code)
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

    @app.get("/ready", response_model=ReadinessResponse)
    async def ready() -> ReadinessResponse:
        try:
            await asyncio.to_thread(_verify_writable_data_dir, settings)
            result = await SQLiteDatabase(settings.database_path).verify()
        except (FileNotFoundError, OSError, RuntimeError, sqlite3.DatabaseError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if not result.ok:
            raise HTTPException(status_code=503, detail="Database verification failed")
        return ReadinessResponse(
            status="ready",
            database="ok",
            storage="ok",
            applied_migrations=result.applied_migrations,
        )

    @app.get("/version", response_model=VersionResponse)
    async def version() -> VersionResponse:
        return VersionResponse(version=__version__)

    @app.get("/classifications", response_model=ClassificationResponse)
    async def classifications() -> ClassificationResponse:
        return ClassificationResponse(classifications=taxonomy.all())

    @app.post("/documents", response_model=DocumentResponse)
    async def create_document(
        file: Annotated[UploadFile, File()],
        request: Request,
    ) -> DocumentResponse:
        return await _ingest_upload(settings, file, metrics=_application_metrics(request))

    @app.post("/documents/batch", response_model=BatchDocumentsResponse)
    async def create_documents_batch(
        files: Annotated[list[UploadFile], File()],
        request: Request,
    ) -> BatchDocumentsResponse:
        if len(files) > settings.api_max_batch_files:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Batch upload contains {len(files)} files, "
                    f"exceeding configured limit {settings.api_max_batch_files}"
                ),
            )
        batch_bytes = await _uploaded_files_size(files)
        if batch_bytes > settings.api_max_batch_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Batch upload contains {batch_bytes} bytes, "
                    f"exceeding configured limit {settings.api_max_batch_bytes}"
                ),
            )
        items: list[BatchDocumentItemResponse] = []
        ingested_count = 0
        failed_count = 0
        for file in files:
            filename = _safe_filename(file.filename or "upload.txt")
            try:
                document = await _ingest_upload(
                    settings,
                    file,
                    metrics=_application_metrics(request),
                )
            except HTTPException as exc:
                failed_count += 1
                detail = exc.detail
                items.append(
                    BatchDocumentItemResponse(
                        filename=filename,
                        status="failed",
                        error=ErrorResponse(
                            detail=detail,
                            code=_api_error_code(status_code=exc.status_code, detail=detail),
                        ),
                    )
                )
                continue
            except Exception as exc:
                failed_count += 1
                detail = str(exc)
                items.append(
                    BatchDocumentItemResponse(
                        filename=filename,
                        status="failed",
                        error=ErrorResponse(
                            detail=detail,
                            code=_api_error_code(status_code=400, detail=detail),
                        ),
                    )
                )
                continue
            ingested_count += 1
            items.append(
                BatchDocumentItemResponse(
                    filename=filename,
                    status="ingested",
                    document=document,
                )
            )
        return BatchDocumentsResponse(
            documents=items,
            ingested=ingested_count,
            failed=failed_count,
        )

    @app.get("/metrics", response_model=MetricsResponse)
    async def metrics(request: Request) -> MetricsResponse:
        current = getattr(request.app.state, "metrics", None)
        if not isinstance(current, MetricsRecorder) or not settings.metrics_enabled:
            raise HTTPException(status_code=404, detail="Metrics disabled")
        return MetricsResponse.model_validate(current.snapshot())

    @app.get("/metrics/prometheus", response_class=PlainTextResponse)
    async def prometheus_metrics(request: Request) -> PlainTextResponse:
        current = getattr(request.app.state, "metrics", None)
        if not isinstance(current, MetricsRecorder) or not settings.metrics_enabled:
            raise HTTPException(status_code=404, detail="Metrics disabled")
        return PlainTextResponse(
            current.prometheus_text(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/documents", response_model=DocumentsResponse)
    async def list_documents(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> DocumentsResponse:
        container = await build_ingest_container(settings)
        page = await container.repository.list(limit=limit, offset=offset)
        total = await container.repository.count_documents()
        return DocumentsResponse(
            documents=[_document_response(document) for document in page],
            total=total,
            limit=limit,
            offset=offset,
        )

    @app.get("/documents/{document_id}", response_model=DocumentResponse)
    async def get_document(document_id: str) -> DocumentResponse:
        container = await build_ingest_container(settings)
        document = await container.repository.get_document(DocumentId(document_id))
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return _document_response(document)

    @app.delete("/documents/{document_id}", response_model=DeleteDocumentResponse)
    async def delete_document(document_id: str) -> DeleteDocumentResponse:
        container = await build_ingest_container(settings)
        document = await container.repository.get_document(DocumentId(document_id))
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        await container.repository.delete_document(DocumentId(document_id))
        await _cleanup_owned_upload(settings, document.source.path)
        return DeleteDocumentResponse(status="deleted", document_id=document_id)

    @app.post("/documents/{document_id}/reprocess", response_model=RunResponse)
    async def reprocess_document(document_id: str, http_request: Request) -> RunResponse:
        container = await build_container(
            settings,
            metrics=_application_metrics(http_request),
            tracer=_application_tracer(http_request),
        )
        document = await container.repository.get_document(DocumentId(document_id))
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        run = await container.process_document.start(DocumentId(document_id))
        try:
            await _submit_run(settings, http_request, run.id)
        except Exception as exc:
            await _fail_unsubmitted_run(container, run.id, exc)
            raise HTTPException(status_code=503, detail=f"Run submission failed: {exc}") from exc
        return _run_response(run)

    @app.get("/documents/{document_id}/content", response_model=ContentResponse)
    async def get_document_content(
        document_id: str,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int | None, Query(ge=1)] = None,
    ) -> ContentResponse:
        container = await build_ingest_container(settings)
        output = await container.repository.get_cleaned_output(DocumentId(document_id))
        if output is None:
            raise HTTPException(status_code=404, detail="Cleaned output not found")
        content_limit = min(limit or settings.api_max_content_chars, settings.api_max_content_chars)
        total_chars = len(output.text)
        end = min(offset + content_limit, total_chars)
        return ContentResponse(
            document_id=document_id,
            text=output.text[offset:end],
            total_chars=total_chars,
            offset=offset,
            limit=content_limit,
            truncated=end < total_chars,
        )

    @app.get(
        "/documents/{document_id}/export",
        responses={
            200: {
                "description": "Latest cleaned output exported as JSON, plain text, or Markdown.",
                "model": ExportDocumentResponse,
                "content": {
                    "text/plain": {"schema": {"type": "string"}},
                    "text/markdown": {"schema": {"type": "string"}},
                },
            }
        },
    )
    async def export_document(document_id: str, format: str = "json"):
        export_format = _normalize_export_format(format)
        container = await build_ingest_container(settings)
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
        return _export_response(exported, export_format)

    @app.post("/runs", response_model=RunResponse)
    async def create_run(request: RunRequest, http_request: Request) -> RunResponse:
        container = await build_container(
            settings,
            metrics=_application_metrics(http_request),
            tracer=_application_tracer(http_request),
        )
        try:
            run = await container.process_document.start(DocumentId(request.document_id))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            await _submit_run(settings, http_request, run.id)
        except Exception as exc:
            await _fail_unsubmitted_run(container, run.id, exc)
            raise HTTPException(status_code=503, detail=f"Run submission failed: {exc}") from exc
        return _run_response(run)

    @app.get("/runs", response_model=RunsResponse)
    async def list_runs(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> RunsResponse:
        container = await build_ingest_container(settings)
        runs = await container.repository.list_runs(limit=limit, offset=offset)
        return RunsResponse(
            runs=[_run_response(run) for run in runs],
            limit=limit,
            offset=offset,
        )

    @app.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(run_id: str) -> RunResponse:
        container = await build_ingest_container(settings)
        run = await container.repository.get_run(RunId(run_id))
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return _run_response(run)

    @app.post("/runs/{run_id}/cancel", response_model=RunResponse)
    async def cancel_run(run_id: str) -> RunResponse:
        container = await build_ingest_container(settings)
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
        container = await build_container(
            settings,
            metrics=_application_metrics(http_request),
            tracer=_application_tracer(http_request),
        )
        run = await container.repository.get_run(RunId(run_id))
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        if run.status != RunStatus.FAILED:
            raise HTTPException(status_code=400, detail="Run is not failed")
        retry = await container.process_document.start(run.document_id)
        try:
            await _submit_run(settings, http_request, retry.id)
        except Exception as exc:
            await _fail_unsubmitted_run(container, retry.id, exc)
            raise HTTPException(status_code=503, detail=f"Run submission failed: {exc}") from exc
        return _run_response(retry)

    @app.post("/imports", response_model=ImportResponse)
    async def import_documents(request: ImportRequest, http_request: Request) -> ImportResponse:
        if settings.api_import_root is None:
            raise HTTPException(status_code=400, detail="API import root is not configured")
        try:
            conversion_format = ConversionFormat(request.format)
            output_mode = DirectoryOutputMode(request.output_mode)
            processing_mode = ImportProcessingMode(request.processing_mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _validate_subdirectory_name(request.subdirectory_name)
        source_path = _resolve_api_path(Path(request.source_dir), settings=settings)
        if not source_path.exists():
            raise HTTPException(status_code=400, detail="source_dir must exist")
        source_dir = source_path if source_path.is_dir() else source_path.parent
        output_dir = (
            _resolve_api_writable_path(
                Path(request.output_dir),
                settings=settings,
                label="output_dir",
            )
            if request.output_dir
            else None
        )
        manifest_path = (
            _resolve_api_writable_path(
                Path(request.manifest_path),
                settings=settings,
                label="manifest_path",
            )
            if request.manifest_path
            else None
        )
        if output_mode == DirectoryOutputMode.NEW_DIRECTORY and output_dir is None:
            raise HTTPException(
                status_code=400,
                detail="output_dir is required when output_mode is new-directory",
            )
        _validate_conversion_output_dir(
            source_dir=source_dir,
            output_mode=output_mode,
            output_dir=output_dir,
        )
        if processing_mode == ImportProcessingMode.QUEUE and settings.job_backend != "sqlite":
            raise HTTPException(
                status_code=400,
                detail="queue processing requires LIBRARIAN_JOB_BACKEND=sqlite",
            )
        metrics = _application_metrics(http_request)
        extractor = _build_extractor(settings, metrics=metrics)
        _validate_api_import_budget(
            source_path,
            recursive=request.recursive,
            allowed_root=settings.api_import_root,
            supported_extensions=extractor.supported_extensions,
            output_mode=output_mode,
            output_dir=output_dir,
            subdirectory_name=request.subdirectory_name,
            max_files=settings.api_max_import_files,
            max_bytes=settings.api_max_import_bytes,
        )
        container = (
            await build_ingest_container(settings, metrics=metrics)
            if processing_mode == ImportProcessingMode.NONE
            else await build_container(
                settings,
                metrics=metrics,
                tracer=_application_tracer(http_request),
            )
        )
        importer = ImportLibrary(
            converter=DocumentConverter(extractor, metrics),
            ingest=container.ingest_document,
            process=getattr(container, "process_document", None),
            queue_factory=lambda: SQLiteRunQueue(container.database),
            manifest_max_bytes=settings.api_max_import_manifest_bytes,
        )
        try:
            result = await importer.import_path(
                source_path,
                format=conversion_format,
                output_mode=output_mode,
                processing_mode=processing_mode,
                output_dir=output_dir,
                subdirectory_name=request.subdirectory_name,
                recursive=request.recursive,
                overwrite=request.overwrite,
                manifest_path=manifest_path,
                resume=request.resume,
                allowed_root=settings.api_import_root,
            )
        except ValueError as exc:
            status_code = 413 if "manifest_path contains more than" in str(exc).lower() else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
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

    @app.get("/imports/status", response_model=ImportStatusResponse)
    async def import_status(
        manifest_path: str,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 500,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> ImportStatusResponse:
        if settings.api_import_root is None:
            raise HTTPException(status_code=400, detail="API import root is not configured")
        path = _resolve_api_manifest_read_path(
            Path(manifest_path),
            settings=settings,
            label="manifest_path",
        )
        if not path.exists():
            raise HTTPException(status_code=404, detail="Import manifest not found")
        try:
            manifest_text = await asyncio.to_thread(
                _read_limited_text_file,
                path,
                max_bytes=settings.api_max_import_manifest_bytes,
                label="Import manifest",
            )
            payload_obj = json.loads(manifest_text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid import manifest: {exc}") from exc
        if not isinstance(payload_obj, dict):
            raise HTTPException(status_code=400, detail="Invalid import manifest")
        payload = cast(dict[str, object], payload_obj)
        summary_obj = payload.get("summary", {})
        items_obj = payload.get("items", [])
        summary = cast(dict[str, object], summary_obj) if isinstance(summary_obj, dict) else {}
        items = cast(list[dict[str, object]], items_obj) if isinstance(items_obj, list) else []
        return ImportStatusResponse(
            converted=_summary_int(summary, "converted"),
            ingested=_summary_int(summary, "ingested"),
            processed=_summary_int(summary, "processed"),
            queued=_summary_int(summary, "queued"),
            skipped=_summary_int(summary, "skipped"),
            failed=_summary_int(summary, "failed"),
            total=len(items),
            limit=limit,
            offset=offset,
            items=items[offset : offset + limit],
        )

    @app.get("/imports/page-manifest", response_model=PdfPageManifestStatusResponse)
    async def import_page_manifest_status(
        manifest_path: str,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 500,
        offset: Annotated[int, Query(ge=0)] = 0,
        failures_only: bool = False,
    ) -> PdfPageManifestStatusResponse:
        if settings.api_import_root is None:
            raise HTTPException(status_code=400, detail="API import root is not configured")
        path = _resolve_api_manifest_read_path(
            Path(manifest_path),
            settings=settings,
            label="manifest_path",
        )
        if not path.exists():
            raise HTTPException(status_code=404, detail="PDF page manifest not found")
        try:
            payload, pages = _read_pdf_page_manifest(
                path,
                max_bytes=settings.api_max_import_manifest_bytes,
            )
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        visible_pages = [
            page
            for page in pages
            if not failures_only or str(page.get("status") or "") == "failed"
        ]
        page_window = visible_pages[offset : offset + limit]
        confidences = [
            float(confidence)
            for page in pages
            if isinstance((confidence := page.get("confidence")), int | float)
        ]
        attempts = sum(
            int(attempt_count)
            for page in pages
            if isinstance((attempt_count := page.get("attempts")), int)
        )
        return PdfPageManifestStatusResponse(
            manifest_path=str(path),
            source_sha256=str(payload.get("source_sha256") or ""),
            page_count=_manifest_int(payload.get("page_count"), default=len(pages)),
            statuses=_count_manifest_values(pages, "status"),
            sources=_count_manifest_values(pages, "source"),
            warnings=_count_manifest_warnings(pages),
            corrected_pages=sum(1 for page in pages if page.get("corrected") is True),
            attempts=attempts,
            average_confidence=(
                round(sum(confidences) / len(confidences), 1) if confidences else None
            ),
            failures_only=failures_only,
            total=len(visible_pages),
            limit=limit,
            offset=offset,
            pages=[_pdf_page_manifest_page_response(page) for page in page_window],
        )

    @app.get("/runs/{run_id}/events", response_model=RunEventsResponse)
    async def get_run_events(
        run_id: str,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 500,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> RunEventsResponse:
        container = await build_ingest_container(settings)
        run = await container.repository.get_run(RunId(run_id))
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return RunEventsResponse(
            events=list(await container.repository.list_events(run.id, limit=limit, offset=offset)),
            limit=limit,
            offset=offset,
        )

    @app.get("/runs/{run_id}/events/records", response_model=RunEventRecordsResponse)
    async def get_run_event_records(
        run_id: str,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 500,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> RunEventRecordsResponse:
        container = await build_ingest_container(settings)
        run = await container.repository.get_run(RunId(run_id))
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return RunEventRecordsResponse(
            events=[
                _run_event_response(event)
                for event in await container.repository.list_event_records(
                    run.id,
                    limit=limit,
                    offset=offset,
                )
            ],
            limit=limit,
            offset=offset,
        )

    @app.get(
        "/runs/{run_id}/events/stream",
        responses={
            200: {
                "description": "Server-sent event stream of run progress messages.",
                "content": {
                    "text/event-stream": {"schema": {"type": "string"}},
                },
            }
        },
    )
    async def stream_run_events(run_id: str) -> StreamingResponse:
        container = await build_ingest_container(settings)
        run = await container.repository.get_run(RunId(run_id))
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return StreamingResponse(
            _event_stream(settings, RunId(run_id)),
            media_type="text/event-stream",
        )

    @app.get(
        "/runs/{run_id}/events/records/stream",
        responses={
            200: {
                "description": "Server-sent event stream of structured run progress records.",
                "content": {
                    "text/event-stream": {"schema": {"type": "string"}},
                },
            }
        },
    )
    async def stream_run_event_records(run_id: str) -> StreamingResponse:
        container = await build_ingest_container(settings)
        run = await container.repository.get_run(RunId(run_id))
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return StreamingResponse(
            _event_record_stream(settings, RunId(run_id)),
            media_type="text/event-stream",
        )

    @app.post("/search", response_model=SearchResponse)
    async def search(request: SearchRequest) -> SearchResponse:
        document_status = _parse_document_status(request.document_status)
        search_scope = _parse_search_scope(request.scope)
        _validate_search_date_window(request)
        _validate_search_query(request)
        container = await build_ingest_container(settings)
        try:
            results = await container.search_library.search(
                request.query,
                limit=request.limit,
                offset=request.offset,
                classification_code=request.classification_code,
                document_status=document_status,
                filename_contains=request.filename_contains,
                created_after=request.created_after,
                created_before=request.created_before,
                scope=search_scope,
                phrase=request.phrase,
            )
            total = await container.search_library.count(
                request.query,
                classification_code=request.classification_code,
                document_status=document_status,
                filename_contains=request.filename_contains,
                created_after=request.created_after,
                created_before=request.created_before,
                scope=search_scope,
                phrase=request.phrase,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return SearchResponse(
            document_ids=[str(document_id) for document_id in results],
            total=total,
            limit=request.limit,
            offset=request.offset,
        )

    @app.post("/search/results", response_model=SearchResultsResponse)
    async def search_results(request: SearchRequest) -> SearchResultsResponse:
        document_status = _parse_document_status(request.document_status)
        search_scope = _parse_search_scope(request.scope)
        _validate_search_date_window(request)
        _validate_search_query(request)
        container = await build_ingest_container(settings)
        try:
            results = await container.search_library.results(
                request.query,
                limit=request.limit,
                offset=request.offset,
                classification_code=request.classification_code,
                document_status=document_status,
                filename_contains=request.filename_contains,
                created_after=request.created_after,
                created_before=request.created_before,
                scope=search_scope,
                phrase=request.phrase,
            )
            total = await container.search_library.count(
                request.query,
                classification_code=request.classification_code,
                document_status=document_status,
                filename_contains=request.filename_contains,
                created_after=request.created_after,
                created_before=request.created_before,
                scope=search_scope,
                phrase=request.phrase,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return SearchResultsResponse(
            results=[
                SearchResultResponse(
                    document_id=str(result.document_id),
                    run_id=str(result.run_id) if result.run_id else None,
                    source=result.source,
                    filename=result.filename,
                    document_status=result.document_status.value,
                    snippet=result.snippet,
                    score=result.score,
                    classification_code=result.classification_code,
                    classification_label=result.classification_label,
                )
                for result in results
            ],
            total=total,
            limit=request.limit,
            offset=request.offset,
        )

    @app.post("/search/facets", response_model=SearchFacetsResponse)
    async def search_facets(request: SearchRequest) -> SearchFacetsResponse:
        search_scope = _parse_search_scope(request.scope)
        document_status = _parse_document_status(request.document_status)
        _validate_search_date_window(request)
        _validate_search_query(request)
        container = await build_ingest_container(settings)
        try:
            facets = await container.search_library.facets(
                request.query,
                classification_code=request.classification_code,
                document_status=document_status,
                filename_contains=request.filename_contains,
                created_after=request.created_after,
                created_before=request.created_before,
                scope=search_scope,
                phrase=request.phrase,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return SearchFacetsResponse(
            classifications=[
                SearchFacetValueResponse(value=item.value, count=item.count, label=item.label)
                for item in facets.classifications
            ],
            statuses=[
                SearchFacetValueResponse(value=item.value, count=item.count, label=item.label)
                for item in facets.statuses
            ],
            sources=[
                SearchFacetValueResponse(value=item.value, count=item.count, label=item.label)
                for item in facets.sources
            ],
            filenames=[
                SearchFacetValueResponse(value=item.value, count=item.count, label=item.label)
                for item in facets.filenames
            ],
        )

    @app.get("/config", response_model=ConfigResponse)
    async def config() -> ConfigResponse:
        return ConfigResponse(
            data_dir=str(settings.data_dir),
            llm_provider=settings.llm_provider,
            llm_model=settings.llm_model,
            llm_base_url=settings.llm_base_url,
            llm_api_key_env=settings.llm_api_key_env,
            llm_timeout_seconds=settings.llm_timeout_seconds,
            llm_max_concurrency=settings.llm_max_concurrency,
            llm_max_retries=settings.llm_max_retries,
            llm_retry_base_delay_seconds=settings.llm_retry_base_delay_seconds,
            llm_retry_max_delay_seconds=settings.llm_retry_max_delay_seconds,
            llm_prompt_cost_per_1k_tokens_usd=settings.llm_prompt_cost_per_1k_tokens_usd,
            llm_completion_cost_per_1k_tokens_usd=(
                settings.llm_completion_cost_per_1k_tokens_usd
            ),
            llm_max_prompt_chars=settings.llm_max_prompt_chars,
            llm_max_response_chars=settings.llm_max_response_chars,
            cleaning_prompt_version=settings.cleaning_prompt_version,
            classification_prompt_version=settings.classification_prompt_version,
            cleaning_mode=settings.cleaning_mode,
            job_backend=settings.job_backend,
            job_max_concurrency=settings.job_max_concurrency,
            job_worker_id=settings.job_worker_id,
            job_lease_seconds=settings.job_lease_seconds,
            job_max_attempts=settings.job_max_attempts,
            api_import_root=str(settings.api_import_root) if settings.api_import_root else None,
            api_max_request_bytes=settings.api_max_request_bytes,
            api_max_upload_bytes=settings.api_max_upload_bytes,
            api_max_batch_files=settings.api_max_batch_files,
            api_max_batch_bytes=settings.api_max_batch_bytes,
            api_max_import_files=settings.api_max_import_files,
            api_max_import_bytes=settings.api_max_import_bytes,
            api_max_import_manifest_bytes=settings.api_max_import_manifest_bytes,
            api_max_content_chars=settings.api_max_content_chars,
            api_rate_limit_per_minute=settings.api_rate_limit_per_minute,
            api_trusted_proxy_cidrs=settings.api_trusted_proxy_cidrs,
            api_audit_retention_days=settings.api_audit_retention_days,
            api_auth_keys_configured=len(_configured_api_credentials(settings)),
            coherence_mode=settings.coherence_mode,
            chunk_target_chars=settings.chunk_target_chars,
            chunk_overlap_chars=settings.chunk_overlap_chars,
            max_source_bytes=settings.max_source_bytes,
            text_max_input_bytes=settings.text_max_input_bytes,
            docx_max_input_bytes=settings.docx_max_input_bytes,
            pdf_max_input_bytes=settings.pdf_max_input_bytes,
            pdf_max_pages=settings.pdf_max_pages,
            ocr_language=settings.ocr_language,
            ocr_timeout_seconds=settings.ocr_timeout_seconds,
            ocr_pdf_dpi=settings.ocr_pdf_dpi,
            ocr_pdf_max_pages=settings.ocr_pdf_max_pages,
            ocr_preprocess_mode=settings.ocr_preprocess_mode,
            ocr_threshold=settings.ocr_threshold,
            ocr_preserve_page_images=settings.ocr_preserve_page_images,
            ocr_llm_correction=settings.ocr_llm_correction,
            ocr_llm_model=settings.ocr_llm_model,
            ocr_low_confidence_threshold=settings.ocr_low_confidence_threshold,
            ocr_page_concurrency=settings.ocr_page_concurrency,
            ocr_fail_on_page_error=settings.ocr_fail_on_page_error,
            universal_max_input_bytes=settings.universal_max_input_bytes,
            universal_timeout_seconds=settings.universal_timeout_seconds,
            log_level=settings.log_level,
            log_format=settings.log_format,
            metrics_enabled=settings.metrics_enabled,
            otel_enabled=settings.otel_enabled,
            otel_service_name=settings.otel_service_name,
            otel_endpoint=settings.otel_endpoint,
        )

    return app


def _with_security_headers(response: JSONResponse) -> JSONResponse:
    for name, value in _SECURITY_HEADERS.items():
        if name not in response.headers:
            response.headers[name] = value
    return response


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


async def _ingest_upload(
    settings: Settings,
    file: UploadFile,
    *,
    metrics: MetricsRecorder | None = None,
) -> DocumentResponse:
    filename = _safe_filename(file.filename or "upload.txt")
    _reject_disallowed_upload_filename(filename)
    container = await build_ingest_container(settings, metrics=metrics)
    upload_dir = await asyncio.to_thread(_ensure_upload_root, settings)
    destination = upload_dir / _unique_upload_filename(filename)
    await _write_limited_upload(
        file,
        destination,
        filename=filename,
        max_bytes=settings.api_max_upload_bytes,
    )
    try:
        ingested = await container.ingest_document.execute(destination)
    except Exception as exc:
        await _cleanup_upload(destination)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if ingested.duplicate:
        await _cleanup_upload(destination)
    return _document_response(ingested.document)


def _run_event_response(event: RunEvent) -> RunEventResponse:
    return RunEventResponse(
        sequence=event.sequence,
        stage=event.stage.value,
        message=event.message,
        created_at=event.created_at.isoformat(),
    )


def _parse_document_status(value: str | None) -> DocumentStatus | None:
    if value is None:
        return None
    try:
        return DocumentStatus(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid document_status") from exc


def _parse_search_scope(value: str) -> SearchScope:
    if value in {"cleaned", "raw"}:
        return cast(SearchScope, value)
    raise HTTPException(status_code=400, detail="Invalid search scope")


def _validate_search_date_window(request: SearchRequest) -> None:
    if (
        request.created_after is not None
        and request.created_before is not None
        and request.created_after > request.created_before
    ):
        raise HTTPException(
            status_code=400,
            detail="created_after must be before or equal to created_before",
        )


def _validate_search_query(request: SearchRequest) -> None:
    try:
        normalize_search_query(request.query, phrase=request.phrase)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _summary_int(summary: dict[str, object], key: str) -> int:
    value = summary.get(key, 0)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _read_pdf_page_manifest(
    path: Path,
    *,
    max_bytes: int,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    if path.is_symlink():
        raise ValueError(f"PDF page manifest path must not be a symlink: {path}")
    if path.is_dir():
        raise ValueError("PDF page manifest must be a JSON file, not a directory")
    try:
        manifest_text = _read_limited_text_file(
            path,
            max_bytes=max_bytes,
            label="PDF page manifest",
        )
        payload_obj = json.loads(manifest_text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid PDF page manifest: {exc}") from exc
    if not isinstance(payload_obj, dict):
        raise ValueError("PDF page manifest must be a JSON object")
    payload = cast(dict[str, object], payload_obj)
    if payload.get("artifact_type") != "pdf-page-extraction-manifest":
        raise ValueError("PDF page manifest has unexpected artifact_type")
    pages_obj = payload.get("pages")
    if not isinstance(pages_obj, list):
        raise ValueError("PDF page manifest is missing pages")
    pages: list[dict[str, object]] = []
    for page in cast(list[object], pages_obj):
        if not isinstance(page, dict):
            raise ValueError("PDF page manifest contains an invalid page record")
        pages.append(cast(dict[str, object], page))
    return payload, pages


def _pdf_page_manifest_page_response(page: dict[str, object]) -> PdfPageManifestPageResponse:
    return PdfPageManifestPageResponse(
        page_number=_manifest_int_or_none(page.get("page_number")),
        source=_manifest_str_or_none(page.get("source")),
        status=_manifest_str_or_none(page.get("status")),
        chars=_manifest_int_or_none(page.get("chars")),
        confidence=_manifest_float_or_none(page.get("confidence")),
        corrected=page.get("corrected") is True,
        attempts=_manifest_int(page.get("attempts"), default=0),
        duration_ms=_manifest_float_or_none(page.get("duration_ms")),
        image_path=_manifest_str_or_none(page.get("image_path")),
        warnings=_manifest_warning_list(page),
        error=_manifest_str_or_none(page.get("error")),
    )


def _count_manifest_values(pages: list[dict[str, object]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for page in pages:
        value = page.get(key)
        if isinstance(value, str) and value:
            counts[value] = counts.get(value, 0) + 1
    return counts


def _count_manifest_warnings(pages: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for page in pages:
        for warning in _manifest_warning_list(page):
            counts[warning] = counts.get(warning, 0) + 1
    return counts


def _manifest_warning_list(page: dict[str, object]) -> list[str]:
    warnings_obj = page.get("warnings")
    if not isinstance(warnings_obj, list):
        return []
    return [
        warning
        for warning in cast(list[object], warnings_obj)
        if isinstance(warning, str)
    ]


def _manifest_int(value: object, *, default: int) -> int:
    parsed = _manifest_int_or_none(value)
    return parsed if parsed is not None else default


def _manifest_int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _manifest_float_or_none(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _manifest_str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _api_error_code(*, status_code: int, detail: object) -> str:
    if isinstance(detail, str):
        normalized = detail.lower()
        if "not found" in normalized:
            return "not_found"
        if "invalid api key" in normalized:
            return "invalid_api_key"
        if "scope" in normalized and "operation" in normalized:
            return "insufficient_scope"
        if "request body contains" in normalized and "configured limit" in normalized:
            return "request_too_large"
        if "upload exceeds" in normalized:
            return "upload_too_large"
        if "batch upload contains" in normalized and "configured limit" in normalized:
            return "batch_too_large"
        if "import contains" in normalized and "configured limit" in normalized:
            return "import_too_large"
        if "import manifest contains" in normalized and "configured limit" in normalized:
            return "import_manifest_too_large"
        if "pdf page manifest contains" in normalized and "configured limit" in normalized:
            return "page_manifest_too_large"
        if "manifest_path contains more than" in normalized and "configured limit" in normalized:
            return "import_manifest_too_large"
        if "upload directory" in normalized or "upload data_dir" in normalized:
            return "invalid_upload_path"
        if "run submission failed" in normalized:
            return "run_submission_failed"
        if "requires librarian_job_backend=sqlite" in normalized:
            return "queue_backend_required"
        if "import root" in normalized or "path must be under" in normalized:
            return "invalid_import_path"
        if "manifest_path" in normalized:
            return "invalid_manifest_path"
        if "pdf page manifest" in normalized:
            return "invalid_manifest_path"
        if "output_dir" in normalized:
            return "invalid_output_dir"
        if "subdirectory_name" in normalized:
            return "invalid_subdirectory_name"
        if "source_dir" in normalized:
            return "invalid_source_dir"
        if "document_status" in normalized:
            return "invalid_document_status"
        if "search scope" in normalized:
            return "invalid_search_scope"
        if "search query exceeds configured limit" in normalized:
            return "search_query_too_large"
        if "invalid search query" in normalized:
            return "invalid_search_query"
        if "created_after" in normalized and "created_before" in normalized:
            return "invalid_search_window"
        if "archive inputs are not supported" in normalized:
            return "archive_not_supported"
        if "unsupported file extension" in normalized:
            return "unsupported_file_type"
        if "terminal" in normalized and "cancel" in normalized:
            return "run_terminal"
        if "not failed" in normalized:
            return "run_not_failed"
    if status_code == 401:
        return "unauthorized"
    if status_code == 403:
        return "forbidden"
    if status_code == 404:
        return "not_found"
    if status_code == 413:
        return "payload_too_large"
    if status_code == 429:
        return "rate_limited"
    if status_code == 422:
        return "validation_error"
    if status_code == 503:
        return "service_unavailable"
    if status_code >= 500:
        return "server_error"
    return "bad_request" if status_code == 400 else "http_error"


def _safe_filename(filename: str) -> str:
    basename = unquote(filename, errors="ignore").replace("\\", "/").split("/")[-1]
    safe = "".join(char for char in basename if char >= " " and char != "\x7f").strip()
    if safe in {".", ".."}:
        return "upload.txt"
    if not safe:
        return "upload.txt"
    return _truncate_utf8_filename(safe, max_bytes=_MAX_UPLOAD_FILENAME_BYTES)


def _truncate_utf8_filename(filename: str, *, max_bytes: int) -> str:
    if len(filename.encode("utf-8")) <= max_bytes:
        return filename
    stem, separator, extension = filename.rpartition(".")
    suffix = f"{separator}{extension}" if stem and separator and extension else ""
    if suffix and len(suffix.encode("utf-8")) < max_bytes:
        return f"{_utf8_prefix(stem, max_bytes=max_bytes - len(suffix.encode('utf-8')))}{suffix}"
    return _utf8_prefix(filename, max_bytes=max_bytes)


def _utf8_prefix(value: str, *, max_bytes: int) -> str:
    used = 0
    output: list[str] = []
    for char in value:
        char_size = len(char.encode("utf-8"))
        if used + char_size > max_bytes:
            break
        output.append(char)
        used += char_size
    return "".join(output) or "upload.txt"


def _reject_disallowed_upload_filename(filename: str) -> None:
    extension = Path(filename).suffix.lower()
    if extension in ARCHIVE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Archive inputs are not supported by default: {extension}",
        )


def _reject_disallowed_upload_signature(filename: str, chunk: bytes) -> None:
    label = archive_signature_label(chunk)
    if label is None:
        return
    if label == "zip" and Path(filename).suffix.lower() in ZIP_CONTAINER_EXTENSIONS:
        return
    if label == "tar":
        raise HTTPException(
            status_code=400,
            detail="Archive inputs are not supported by default: tar signature detected",
        )
    raise HTTPException(
        status_code=400,
        detail="Archive inputs are not supported by default: archive signature detected",
    )


def _unique_upload_filename(filename: str) -> str:
    return str(Path(uuid.uuid4().hex) / _safe_filename(filename))


async def _write_limited_upload(
    file: UploadFile,
    destination: Path,
    *,
    filename: str,
    max_bytes: int,
) -> None:
    await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
    total = 0
    first_chunk = True
    try:
        with destination.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                if first_chunk:
                    _reject_disallowed_upload_signature(filename, chunk)
                    first_chunk = False
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


async def _uploaded_files_size(files: list[UploadFile]) -> int:
    total = 0
    for file in files:
        size = getattr(file, "size", None)
        if isinstance(size, int):
            total += max(size, 0)
            continue
        current = await asyncio.to_thread(file.file.tell)
        end = await asyncio.to_thread(file.file.seek, 0, 2)
        await asyncio.to_thread(file.file.seek, current)
        total += int(end)
    return total


async def _cleanup_upload(destination: Path) -> None:
    await asyncio.to_thread(destination.unlink, missing_ok=True)
    try:
        await asyncio.to_thread(destination.parent.rmdir)
    except OSError:
        pass


async def _cleanup_owned_upload(settings: Settings, path: Path) -> None:
    try:
        upload_root = await asyncio.to_thread(_ensure_upload_root, settings)
    except HTTPException:
        return
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


def _ensure_upload_root(settings: Settings) -> Path:
    expanded_data_dir = settings.data_dir.expanduser()
    if _path_crosses_symlink(expanded_data_dir):
        raise HTTPException(
            status_code=400,
            detail="Upload data_dir must not be or cross a symlink",
        )
    data_dir = expanded_data_dir.resolve()
    upload_root = data_dir / "uploads"
    data_dir.mkdir(parents=True, exist_ok=True)
    if upload_root.exists() and upload_root.is_symlink():
        raise HTTPException(status_code=400, detail="Upload directory must not be a symlink")
    upload_root.mkdir(exist_ok=True)
    resolved = upload_root.resolve()
    try:
        resolved.relative_to(data_dir)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Upload directory must stay under data_dir",
        ) from exc
    return resolved


def _verify_writable_data_dir(settings: Settings) -> None:
    data_dir = settings.data_dir.expanduser()
    if _path_crosses_symlink(data_dir):
        raise RuntimeError("data_dir must not be or cross a symlink")
    data_dir.mkdir(parents=True, exist_ok=True)
    resolved = data_dir.resolve()
    handle = tempfile.NamedTemporaryFile(prefix=".librarian-ready-", dir=resolved, delete=False)
    try:
        handle.write(b"ok")
        handle.flush()
    finally:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)


def _path_crosses_symlink(path: Path) -> bool:
    for current in (*reversed(path.parents), path):
        if current.exists() and current.is_symlink():
            return True
    return False


def _validate_subdirectory_name(name: str) -> None:
    path = Path(name)
    unsafe_part = any(part in {"", ".", ".."} for part in path.parts)
    if path.is_absolute() or not path.parts or unsafe_part:
        raise HTTPException(
            status_code=400,
            detail="subdirectory_name must be a relative safe path",
        )


def _validate_conversion_output_dir(
    *,
    source_dir: Path,
    output_mode: DirectoryOutputMode,
    output_dir: Path | None,
) -> None:
    if output_mode != DirectoryOutputMode.NEW_DIRECTORY or output_dir is None:
        return
    try:
        source_dir.relative_to(output_dir)
    except ValueError:
        return
    raise HTTPException(
        status_code=400,
        detail="output_dir must not be source_dir or an ancestor of source_dir",
    )


def _validate_api_import_budget(
    source_path: Path,
    *,
    recursive: bool,
    allowed_root: Path,
    supported_extensions: frozenset[str],
    output_mode: DirectoryOutputMode,
    output_dir: Path | None,
    subdirectory_name: str,
    max_files: int,
    max_bytes: int,
) -> None:
    if source_path.is_file():
        import_files = [source_path] if source_path.suffix.lower() in supported_extensions else []
        _validate_import_budget_values(import_files, max_files=max_files, max_bytes=max_bytes)
        return
    else:
        exclusions = conversion_output_exclusions(
            source_dir=source_path,
            output_mode=output_mode,
            output_dir=output_dir,
            subdirectory_name=subdirectory_name,
        )
        import_count = 0
        import_bytes = 0
        for path in iter_supported_files(
            source_path,
            supported_extensions=supported_extensions,
            recursive=recursive,
            allowed_root=allowed_root,
            exclude_paths=exclusions,
        ):
            import_count += 1
            if import_count > max_files:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"Import contains more than {max_files} files, "
                        f"exceeding configured limit {max_files}"
                    ),
                )
            import_bytes += _safe_file_size(path)
            if import_bytes > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"Import contains more than {max_bytes} bytes, "
                        f"exceeding configured limit {max_bytes}"
                    ),
                )


def _validate_import_budget_values(
    import_files: list[Path],
    *,
    max_files: int,
    max_bytes: int,
) -> None:
    import_count = len(import_files)
    if import_count > max_files:
        raise HTTPException(
            status_code=413,
            detail=f"Import contains {import_count} files, exceeding configured limit {max_files}",
        )
    import_bytes = sum(_safe_file_size(path) for path in import_files)
    if import_bytes > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Import contains {import_bytes} bytes, "
                f"exceeding configured limit {max_bytes}"
            ),
        )


def _safe_file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _read_limited_text_file(path: Path, *, max_bytes: int, label: str) -> str:
    with path.open("rb") as handle:
        payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"{label} contains more than {max_bytes} bytes, "
                f"exceeding configured limit {max_bytes}"
            ),
        )
    return payload.decode("utf-8")


def _build_extractor(
    settings: Settings,
    *,
    metrics: MetricsRecorder | None = None,
) -> CompositeExtractor:
    return CompositeExtractor(
        ocr_language=settings.ocr_language,
        ocr_timeout_seconds=settings.ocr_timeout_seconds,
        ocr_pdf_dpi=settings.ocr_pdf_dpi,
        ocr_pdf_max_pages=settings.ocr_pdf_max_pages,
        ocr_preprocess_mode=settings.ocr_preprocess_mode,
        ocr_threshold=settings.ocr_threshold,
        ocr_preserve_page_images=settings.ocr_preserve_page_images,
        ocr_correction_provider=LazyLLMProvider(settings, metrics=metrics),
        ocr_correction_mode=settings.ocr_llm_correction,
        ocr_correction_model=settings.ocr_llm_model or settings.llm_model,
        ocr_low_confidence_threshold=settings.ocr_low_confidence_threshold,
        ocr_max_correction_response_chars=settings.llm_max_response_chars,
        ocr_page_concurrency=settings.ocr_page_concurrency,
        ocr_fail_on_page_error=settings.ocr_fail_on_page_error,
        text_max_input_bytes=settings.text_max_input_bytes,
        docx_max_input_bytes=settings.docx_max_input_bytes,
        pdf_max_input_bytes=settings.pdf_max_input_bytes,
        pdf_max_pages=settings.pdf_max_pages,
        universal_max_input_bytes=settings.universal_max_input_bytes,
        universal_timeout_seconds=settings.universal_timeout_seconds,
        metrics=metrics,
    )


def _is_public_bind(host: str) -> bool:
    return host in {"0.0.0.0", "::", "[::]"}  # noqa: S104


def _validate_api_security(settings: Settings) -> None:
    if not _is_public_bind(settings.api_host):
        return
    if not _configured_api_credentials(settings):
        raise RuntimeError(
            "LIBRARIAN_API_KEY, LIBRARIAN_API_KEYS, LIBRARIAN_API_KEY_SHA256, "
            "or LIBRARIAN_API_KEY_HASHES is required when binding the API publicly"
        )
    if settings.api_import_root is None:
        raise RuntimeError("LIBRARIAN_API_IMPORT_ROOT is required when binding the API publicly")


def _configured_api_credentials(settings: Settings) -> tuple[ApiCredential, ...]:
    values: list[ApiCredential] = []
    if settings.api_key:
        values.append(ApiCredential(key=settings.api_key, scope="write"))
    if settings.api_keys:
        values.extend(
            _parse_api_credential_entry(entry)
            for entry in settings.api_keys.split(",")
            if entry.strip()
        )
    if settings.api_key_sha256:
        values.append(_api_hash_credential(settings.api_key_sha256, scope="write"))
    if settings.api_key_hashes:
        values.extend(
            _parse_api_hash_entry(entry)
            for entry in settings.api_key_hashes.split(",")
            if entry.strip()
        )
    deduped: dict[tuple[bool, str], ApiCredential] = {}
    for credential in values:
        dedupe_key = (credential.hashed, credential.key)
        existing = deduped.get(dedupe_key)
        if existing is None or (existing.scope == "read" and credential.scope == "write"):
            deduped[dedupe_key] = credential
    return tuple(deduped.values())


def _parse_api_credential_entry(entry: str) -> ApiCredential:
    raw = entry.strip()
    prefix, separator, value = raw.partition(":")
    if separator and prefix.lower() in {"read", "readonly", "read-only"}:
        key = value.strip()
        if key:
            return ApiCredential(key=key, scope="read")
    if separator and prefix.lower() in {"write", "admin", "full"}:
        key = value.strip()
        if key:
            return ApiCredential(key=key, scope="write")
    return ApiCredential(key=raw, scope="write")


def _parse_api_hash_entry(entry: str) -> ApiCredential:
    raw = entry.strip()
    prefix, separator, value = raw.partition(":")
    if separator and prefix.lower() in {"read", "readonly", "read-only"}:
        return _api_hash_credential(value, scope="read")
    if separator and prefix.lower() in {"write", "admin", "full"}:
        return _api_hash_credential(value, scope="write")
    return _api_hash_credential(raw, scope="write")


def _api_hash_credential(value: str, *, scope: ApiKeyScope) -> ApiCredential:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise RuntimeError("API key SHA-256 hashes must be 64 lowercase or uppercase hex chars")
    return ApiCredential(key=normalized, scope=scope, hashed=True)


def _matched_api_credential(
    supplied: str,
    configured: tuple[ApiCredential, ...],
) -> ApiCredential | None:
    supplied_hash = hashlib.sha256(supplied.encode("utf-8")).hexdigest()
    for credential in configured:
        candidate = supplied_hash if credential.hashed else supplied
        if secrets.compare_digest(candidate, credential.key):
            return credential
    return None


def _supplied_api_key(request: Request) -> str | None:
    header_key = request.headers.get("x-api-key")
    if header_key:
        return header_key
    authorization = request.headers.get("authorization")
    if not authorization:
        return None
    scheme, separator, token = authorization.partition(" ")
    if separator and scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _api_credential_allows(credential: ApiCredential, request: Request) -> bool:
    if credential.scope == "write":
        return True
    if request.url.path in _READ_SCOPE_RESTRICTED_PATHS:
        return False
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return True
    return request.method == "POST" and request.url.path in {
        "/search",
        "/search/results",
        "/search/facets",
    }


def _log_api_security_event(
    logger: logging.Logger,
    settings: Settings,
    request: Request,
    *,
    event: str,
    **extra: object,
) -> None:
    logger.warning(
        event,
        extra={
            "method": request.method,
            "path": request.url.path,
            "client_host": _client_host(request, settings),
            **extra,
        },
    )


async def _record_api_audit_event(
    settings: Settings,
    request: Request,
    *,
    event: str,
    credential_present: bool = False,
    credential_scope: ApiKeyScope | None = None,
    retry_after_seconds: int | None = None,
) -> None:
    await asyncio.to_thread(
        _record_api_audit_event_sync,
        settings,
        request.method,
        request.url.path,
        _client_host(request, settings),
        event,
        credential_present,
        credential_scope,
        retry_after_seconds,
    )


def _record_api_audit_event_sync(
    settings: Settings,
    method: str,
    path: str,
    client_host: str,
    event: str,
    credential_present: bool,
    credential_scope: ApiKeyScope | None,
    retry_after_seconds: int | None,
) -> None:
    database = SQLiteDatabase(settings.database_path)
    with database.connect() as connection:
        if settings.api_audit_retention_days > 0:
            cutoff = datetime.now(UTC) - timedelta(days=settings.api_audit_retention_days)
            connection.execute(
                "DELETE FROM api_audit_events WHERE created_at < ?",
                (cutoff.isoformat(),),
            )
        connection.execute(
            """
            INSERT INTO api_audit_events (
              event, method, path, client_host, credential_present,
              credential_scope, retry_after_seconds, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event,
                method,
                path,
                client_host,
                1 if credential_present else 0,
                credential_scope,
                retry_after_seconds,
                datetime.now(UTC).isoformat(),
            ),
        )


def _rate_limit_identity(request: Request, settings: Settings) -> str:
    supplied_key = _supplied_api_key(request)
    if supplied_key:
        digest = hashlib.sha256(supplied_key.encode("utf-8")).hexdigest()
        return f"api-key:{digest}"
    client_host = _client_host(request, settings)
    return f"ip:{client_host}"


def _client_host(request: Request, settings: Settings) -> str:
    client_host = request.client.host if request.client else "unknown"
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for and _trusted_proxy_client_host(client_host, settings):
        forwarded_host = forwarded_for.split(",", maxsplit=1)[0].strip()
        if _valid_forwarded_ip(forwarded_host):
            client_host = forwarded_host
    return client_host


def _trusted_proxy_client_host(client_host: str, settings: Settings) -> bool:
    if not settings.api_trusted_proxy_cidrs:
        return False
    try:
        client_ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    return any(
        client_ip in ipaddress.ip_network(entry.strip(), strict=False)
        for entry in settings.api_trusted_proxy_cidrs.split(",")
        if entry.strip()
    )


def _valid_forwarded_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


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


def _resolve_api_writable_path(path: Path, *, settings: Settings, label: str) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise HTTPException(status_code=400, detail=f"{label} must not be a symlink")
    if _path_crosses_symlink(expanded):
        raise HTTPException(status_code=400, detail=f"{label} must not cross a symlinked parent")
    absolute = expanded if expanded.is_absolute() else Path.cwd() / expanded
    resolved = absolute.resolve()
    if settings.api_import_root is None:
        raise HTTPException(status_code=400, detail="API import root is not configured")
    root = settings.api_import_root.expanduser().resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        detail = f"Path must be under import root: {root}"
        raise HTTPException(status_code=400, detail=detail) from exc
    return absolute


def _resolve_api_manifest_read_path(path: Path, *, settings: Settings, label: str) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise HTTPException(status_code=400, detail=f"{label} must not be a symlink")
    if _path_crosses_symlink(expanded):
        raise HTTPException(status_code=400, detail=f"{label} must not cross a symlinked parent")
    return _resolve_api_path(expanded, settings=settings)


async def _execute_run(
    settings: Settings,
    run_id: RunId,
    metrics: MetricsRecorder | None = None,
    tracer: Any | None = None,
) -> None:
    container = await build_container(settings, metrics=metrics, tracer=tracer)
    await container.process_document.execute_existing(run_id)


async def _submit_run(settings: Settings, request: Request, run_id: RunId) -> None:
    if settings.job_backend == "in-process":
        runner = _job_runner(request)
        metrics = _application_metrics(request)
        tracer = _application_tracer(request)
        await runner.submit(run_id, lambda: _execute_run(settings, run_id, metrics, tracer))
        return
    if settings.job_backend == "sqlite":
        container = await build_container(settings)
        await SQLiteRunQueue(container.database).enqueue(run_id)
        return
    raise RuntimeError(f"Unsupported job backend: {settings.job_backend}")


def _application_metrics(request: Request) -> MetricsRecorder | None:
    metrics = getattr(request.app.state, "metrics", None)
    state_settings = getattr(request.app.state, "settings", None)
    if (
        isinstance(metrics, MetricsRecorder)
        and isinstance(state_settings, Settings)
        and state_settings.metrics_enabled
    ):
        return metrics
    return None


def _application_tracer(request: Request) -> Any | None:
    tracing = getattr(request.app.state, "tracing", None)
    if isinstance(tracing, TracingHandle):
        return tracing.tracer
    return None


async def _fail_unsubmitted_run(container: Any, run_id: RunId, exc: Exception) -> None:
    await container.repository.update_status(
        run_id,
        status=RunStatus.FAILED,
        stage=RunStage.COMPLETE,
        error=f"submission failed: {exc}",
    )


def _job_runner(request: Request) -> InProcessJobRunner:
    runner = request.app.state.job_runner
    if not isinstance(runner, InProcessJobRunner):
        raise RuntimeError("Job runner is not initialized")
    return runner


async def _event_stream(settings: Settings, run_id: RunId) -> AsyncIterator[str]:
    seen = 0
    while True:
        container = await build_ingest_container(settings)
        events = list(await container.repository.list_events(run_id, limit=500, offset=seen))
        for event in events:
            yield f"data: {event}\n\n"
        seen += len(events)
        run = await container.repository.get_run(run_id)
        if run is None or run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}:
            yield "event: done\ndata: done\n\n"
            break
        await asyncio.sleep(0.2)


async def _event_record_stream(settings: Settings, run_id: RunId) -> AsyncIterator[str]:
    seen = 0
    while True:
        container = await build_ingest_container(settings)
        events = list(await container.repository.list_event_records(run_id, limit=500, offset=seen))
        for event in events:
            payload = _run_event_response(event).model_dump()
            yield f"event: run-event\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
        seen += len(events)
        run = await container.repository.get_run(run_id)
        if run is None or run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}:
            yield "event: done\ndata: done\n\n"
            break
        await asyncio.sleep(0.2)


def _normalize_export_format(format: str) -> ExportFormat:
    normalized = format.lower()
    if normalized not in {"json", "txt", "md"}:
        raise HTTPException(status_code=400, detail="Unsupported export format")
    return cast(ExportFormat, normalized)


def _export_response(payload: ExportedDocument, format: ExportFormat):
    headers = {"Content-Disposition": _content_disposition(_export_filename(payload, format))}
    if format == "json":
        return JSONResponse(json.loads(payload.render("json")), headers=headers)
    if format == "txt":
        return PlainTextResponse(payload.render("txt"), headers=headers)
    if format == "md":
        return PlainTextResponse(payload.render("md"), media_type="text/markdown", headers=headers)
    raise ValueError(f"Unsupported export format: {format}")


def _export_filename(payload: ExportedDocument, format: str) -> str:
    safe_source = _safe_filename(payload.document.source.filename)
    stem = Path(safe_source).stem.strip(". ") or "document"
    extension = "json" if format == "json" else format
    return _truncate_utf8_filename(f"{stem}.{extension}", max_bytes=_MAX_UPLOAD_FILENAME_BYTES)


def _content_disposition(filename: str) -> str:
    ascii_name = "".join(
        char if 32 <= ord(char) < 127 and char not in {'"', "\\", ";"} else "_"
        for char in filename
    ).strip()
    if not ascii_name:
        ascii_name = "document"
    encoded_name = quote(filename, safe="")
    return f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
