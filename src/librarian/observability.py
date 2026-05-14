"""Logging and metrics helpers for service adapters."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from importlib import import_module
from threading import Lock
from types import TracebackType
from typing import Any

_SECRET_REPLACEMENTS = (
    (
        re.compile(r"(?i)\b(api[_-]?key|token|secret|password)(\s*[:=]\s*)([^\s,;]+)"),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"(?i)\b(authorization:\s*bearer\s+)([^\s,;]+)"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "[REDACTED]"),
)
_MAX_STORED_ERROR_CHARS = 1_000


class JsonFormatter(logging.Formatter):
    """Small JSON log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": redact_secrets(record.getMessage()),
            "timestamp": self.formatTime(record, self.datefmt),
        }
        for key in (
            "request_id",
            "method",
            "path",
            "status_code",
            "duration_ms",
            "run_id",
            "document_id",
            "stage",
            "status",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = redact_secrets(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


class RedactingFormatter(logging.Formatter):
    """Plain-text formatter that applies the shared secret redaction policy."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


def configure_logging(*, level: str, log_format: str) -> None:
    """Configure root logging once for CLI/API processes."""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(RedactingFormatter("%(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(level.upper())


@dataclass(frozen=True, slots=True)
class TracingHandle:
    """Runtime handle for optional OpenTelemetry tracing."""

    tracer: Any
    provider: Any

    def shutdown(self) -> None:
        """Flush and stop the configured tracer provider."""
        shutdown = getattr(self.provider, "shutdown", None)
        if callable(shutdown):
            shutdown()


def configure_tracing(
    *,
    enabled: bool,
    service_name: str,
    endpoint: str | None = None,
    headers: str | None = None,
) -> TracingHandle | None:
    """Configure optional OpenTelemetry OTLP/HTTP tracing."""
    if not enabled:
        return None
    try:
        trace = import_module("opentelemetry.trace")
        resources = import_module("opentelemetry.sdk.resources")
        tracing = import_module("opentelemetry.sdk.trace")
        trace_export = import_module("opentelemetry.sdk.trace.export")
        otlp_export = import_module(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter"
        )
    except ImportError as exc:
        raise RuntimeError(
            "OpenTelemetry tracing requires installing the 'otel' extra: "
            'pip install "nampara-librarian[otel]"'
        ) from exc

    resource = resources.Resource.create({"service.name": service_name})
    provider = tracing.TracerProvider(resource=resource)
    exporter_kwargs: dict[str, object] = {}
    if endpoint:
        exporter_kwargs["endpoint"] = endpoint
    parsed_headers = parse_otel_headers(headers)
    if parsed_headers:
        exporter_kwargs["headers"] = parsed_headers
    exporter = otlp_export.OTLPSpanExporter(**exporter_kwargs)
    provider.add_span_processor(trace_export.BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    return TracingHandle(
        tracer=trace.get_tracer("librarian"),
        provider=provider,
    )


def parse_otel_headers(headers: str | None) -> dict[str, str]:
    """Parse comma-separated OTLP header assignments."""
    if headers is None or not headers.strip():
        return {}
    parsed: dict[str, str] = {}
    for part in headers.split(","):
        key, separator, value = part.partition("=")
        if not separator:
            raise ValueError("OpenTelemetry headers must use key=value pairs")
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError("OpenTelemetry header names must not be empty")
        parsed[key] = value
    return parsed


class NullSpan:
    """No-op span used when OpenTelemetry is disabled."""

    def __enter__(self) -> NullSpan:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    def set_attribute(self, key: str, value: str | int | float | bool) -> None:
        """Ignore span attributes."""
        del key, value


def start_request_span(
    tracer: Any | None,
    *,
    method: str,
    path: str,
    request_id: str,
) -> Any:
    """Start an HTTP request span if tracing is enabled."""
    if tracer is None:
        return NullSpan()
    return tracer.start_as_current_span(
        f"{method} {path}",
        attributes={
            "http.request.method": method,
            "url.path": path,
            "librarian.request_id": request_id,
        },
    )


def start_span(
    tracer: Any | None,
    name: str,
    *,
    attributes: dict[str, str | int | float | bool] | None = None,
) -> Any:
    """Start a named span if tracing is enabled."""
    if tracer is None:
        return NullSpan()
    return tracer.start_as_current_span(name, attributes=attributes or {})


def redact_secrets(value: str) -> str:
    """Redact common credential patterns before writing logs."""
    redacted = value
    for pattern, replacement in _SECRET_REPLACEMENTS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def sanitize_error_message(value: object, *, max_chars: int = _MAX_STORED_ERROR_CHARS) -> str:
    """Return a bounded, redacted error message safe for run records and APIs."""
    text = redact_secrets(str(value))
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}...[truncated]"


@dataclass(slots=True)
class MetricsRecorder:
    """In-memory process metrics for the API adapter."""

    started_at: float = field(default_factory=time.time)
    requests_total: int = 0
    errors_total: int = 0
    request_duration_ms_total: float = 0.0
    status_counts: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    run_stage_duration_ms_total: dict[str, float] = field(
        default_factory=lambda: dict[str, float]()
    )
    run_stage_counts: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    runs_completed_total: int = 0
    runs_failed_total: int = 0
    runs_canceled_total: int = 0
    queue_wait_ms_total: float = 0.0
    queue_claims_total: int = 0
    queue_failures_total: int = 0
    conversion_failures_total: int = 0
    conversion_failures_by_type: dict[str, int] = field(
        default_factory=lambda: dict[str, int]()
    )
    ocr_pages_total: int = 0
    ocr_failures_total: int = 0
    ocr_corrected_pages_total: int = 0
    ocr_page_duration_ms_total: float = 0.0
    ocr_pages_by_status: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    llm_prompt_tokens_total: int = 0
    llm_completion_tokens_total: int = 0
    llm_tokens_total: int = 0
    llm_estimated_cost_usd_total: float = 0.0
    llm_tokens_by_model: dict[str, int] = field(default_factory=lambda: dict[str, int]())
    llm_estimated_cost_usd_by_model: dict[str, float] = field(
        default_factory=lambda: dict[str, float]()
    )
    _lock: Lock = field(default_factory=Lock, repr=False)

    def record(self, *, status_code: int, duration_ms: float) -> None:
        """Record one HTTP request."""
        with self._lock:
            self.requests_total += 1
            self.request_duration_ms_total += duration_ms
            if status_code >= 500:
                self.errors_total += 1
            status_key = str(status_code)
            self.status_counts[status_key] = self.status_counts.get(status_key, 0) + 1

    def record_run_stage(self, *, stage: str, duration_ms: float) -> None:
        """Record processing time spent in one run stage."""
        with self._lock:
            self.run_stage_duration_ms_total[stage] = (
                self.run_stage_duration_ms_total.get(stage, 0.0) + duration_ms
            )
            self.run_stage_counts[stage] = self.run_stage_counts.get(stage, 0) + 1

    def record_run_finished(self, *, status: str) -> None:
        """Record a terminal run outcome."""
        with self._lock:
            if status == "succeeded":
                self.runs_completed_total += 1
            elif status == "failed":
                self.runs_failed_total += 1
            elif status == "canceled":
                self.runs_canceled_total += 1

    def record_queue_claim(self, *, wait_ms: float) -> None:
        """Record one claimed durable queue item."""
        with self._lock:
            self.queue_claims_total += 1
            self.queue_wait_ms_total += wait_ms

    def record_queue_failure(self) -> None:
        """Record one durable queue processing failure."""
        with self._lock:
            self.queue_failures_total += 1

    def record_conversion_failure(
        self,
        *,
        failure_type: str,
        source_extension: str,
    ) -> None:
        """Record a classified conversion failure."""
        key = f"{failure_type}:{source_extension or '<none>'}"
        with self._lock:
            self.conversion_failures_total += 1
            self.conversion_failures_by_type[key] = (
                self.conversion_failures_by_type.get(key, 0) + 1
            )

    def record_ocr_page(
        self,
        *,
        source: str,
        status: str,
        duration_ms: float,
        corrected: bool = False,
    ) -> None:
        """Record one OCR page attempt."""
        key = f"{source}:{status}"
        with self._lock:
            self.ocr_pages_total += 1
            self.ocr_page_duration_ms_total += duration_ms
            if status == "failed":
                self.ocr_failures_total += 1
            if corrected:
                self.ocr_corrected_pages_total += 1
            self.ocr_pages_by_status[key] = self.ocr_pages_by_status.get(key, 0) + 1

    def record_llm_usage(
        self,
        *,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        """Record token usage reported by an LLM provider."""
        key = f"{provider}:{model}"
        with self._lock:
            self.llm_prompt_tokens_total += prompt_tokens
            self.llm_completion_tokens_total += completion_tokens
            self.llm_tokens_total += total_tokens
            self.llm_estimated_cost_usd_total += estimated_cost_usd
            self.llm_tokens_by_model[key] = self.llm_tokens_by_model.get(key, 0) + total_tokens
            self.llm_estimated_cost_usd_by_model[key] = (
                self.llm_estimated_cost_usd_by_model.get(key, 0.0) + estimated_cost_usd
            )

    def snapshot(self) -> dict[str, object]:
        """Return current metric values."""
        with self._lock:
            average_duration = (
                self.request_duration_ms_total / self.requests_total
                if self.requests_total
                else 0.0
            )
            average_queue_wait_ms = (
                self.queue_wait_ms_total / self.queue_claims_total
                if self.queue_claims_total
                else 0.0
            )
            ocr_pages_per_second = (
                self.ocr_pages_total / (self.ocr_page_duration_ms_total / 1000)
                if self.ocr_page_duration_ms_total > 0
                else 0.0
            )
            return {
                "uptime_seconds": max(time.time() - self.started_at, 0.0),
                "requests_total": self.requests_total,
                "errors_total": self.errors_total,
                "average_request_duration_ms": average_duration,
                "status_counts": dict(self.status_counts),
                "run_stage_duration_ms_total": dict(self.run_stage_duration_ms_total),
                "run_stage_counts": dict(self.run_stage_counts),
                "runs_completed_total": self.runs_completed_total,
                "runs_failed_total": self.runs_failed_total,
                "runs_canceled_total": self.runs_canceled_total,
                "queue_claims_total": self.queue_claims_total,
                "queue_failures_total": self.queue_failures_total,
                "average_queue_wait_ms": average_queue_wait_ms,
                "conversion_failures_total": self.conversion_failures_total,
                "conversion_failures_by_type": dict(self.conversion_failures_by_type),
                "ocr_pages_total": self.ocr_pages_total,
                "ocr_failures_total": self.ocr_failures_total,
                "ocr_corrected_pages_total": self.ocr_corrected_pages_total,
                "ocr_page_duration_ms_total": self.ocr_page_duration_ms_total,
                "ocr_pages_per_second": ocr_pages_per_second,
                "ocr_pages_by_status": dict(self.ocr_pages_by_status),
                "llm_prompt_tokens_total": self.llm_prompt_tokens_total,
                "llm_completion_tokens_total": self.llm_completion_tokens_total,
                "llm_tokens_total": self.llm_tokens_total,
                "llm_estimated_cost_usd_total": self.llm_estimated_cost_usd_total,
                "llm_tokens_by_model": dict(self.llm_tokens_by_model),
                "llm_estimated_cost_usd_by_model": dict(self.llm_estimated_cost_usd_by_model),
            }

    def prometheus_text(self) -> str:
        """Render current metrics in Prometheus text exposition format."""
        with self._lock:
            uptime_seconds = max(time.time() - self.started_at, 0.0)
            requests_total = self.requests_total
            errors_total = self.errors_total
            request_duration_ms_total = self.request_duration_ms_total
            average_request_duration_ms = (
                self.request_duration_ms_total / self.requests_total
                if self.requests_total
                else 0.0
            )
            runs_completed_total = self.runs_completed_total
            runs_failed_total = self.runs_failed_total
            runs_canceled_total = self.runs_canceled_total
            queue_claims_total = self.queue_claims_total
            queue_failures_total = self.queue_failures_total
            average_queue_wait_ms = (
                self.queue_wait_ms_total / self.queue_claims_total
                if self.queue_claims_total
                else 0.0
            )
            ocr_pages_per_second = (
                self.ocr_pages_total / (self.ocr_page_duration_ms_total / 1000)
                if self.ocr_page_duration_ms_total > 0
                else 0.0
            )
            status_counts = dict(self.status_counts)
            run_stage_duration_ms_total = dict(self.run_stage_duration_ms_total)
            run_stage_counts = dict(self.run_stage_counts)
            conversion_failures_total = self.conversion_failures_total
            conversion_failures_by_type = dict(self.conversion_failures_by_type)
            ocr_pages_total = self.ocr_pages_total
            ocr_failures_total = self.ocr_failures_total
            ocr_corrected_pages_total = self.ocr_corrected_pages_total
            ocr_page_duration_ms_total = self.ocr_page_duration_ms_total
            ocr_pages_by_status = dict(self.ocr_pages_by_status)
            llm_prompt_tokens_total = self.llm_prompt_tokens_total
            llm_completion_tokens_total = self.llm_completion_tokens_total
            llm_tokens_total = self.llm_tokens_total
            llm_estimated_cost_usd_total = self.llm_estimated_cost_usd_total
            llm_tokens_by_model = dict(self.llm_tokens_by_model)
            llm_estimated_cost_usd_by_model = dict(self.llm_estimated_cost_usd_by_model)
        lines = [
            "# HELP librarian_uptime_seconds Process uptime in seconds.",
            "# TYPE librarian_uptime_seconds gauge",
            _prometheus_sample("librarian_uptime_seconds", uptime_seconds),
            "# HELP librarian_requests_total Total HTTP requests observed by this process.",
            "# TYPE librarian_requests_total counter",
            _prometheus_sample("librarian_requests_total", requests_total),
            "# HELP librarian_errors_total Total HTTP 5xx responses observed by this process.",
            "# TYPE librarian_errors_total counter",
            _prometheus_sample("librarian_errors_total", errors_total),
            (
                "# HELP librarian_request_duration_ms_total "
                "Total HTTP request duration in milliseconds."
            ),
            "# TYPE librarian_request_duration_ms_total counter",
            _prometheus_sample(
                "librarian_request_duration_ms_total",
                request_duration_ms_total,
            ),
            (
                "# HELP librarian_request_duration_ms_average "
                "Average HTTP request duration in milliseconds."
            ),
            "# TYPE librarian_request_duration_ms_average gauge",
            _prometheus_sample(
                "librarian_request_duration_ms_average",
                average_request_duration_ms,
            ),
            "# HELP librarian_runs_total Terminal processing run outcomes.",
            "# TYPE librarian_runs_total counter",
            _prometheus_sample(
                "librarian_runs_total",
                runs_completed_total,
                {"status": "succeeded"},
            ),
            _prometheus_sample(
                "librarian_runs_total",
                runs_failed_total,
                {"status": "failed"},
            ),
            _prometheus_sample(
                "librarian_runs_total",
                runs_canceled_total,
                {"status": "canceled"},
            ),
            "# HELP librarian_queue_claims_total Durable queue claims.",
            "# TYPE librarian_queue_claims_total counter",
            _prometheus_sample("librarian_queue_claims_total", queue_claims_total),
            "# HELP librarian_queue_failures_total Durable queue processing failures.",
            "# TYPE librarian_queue_failures_total counter",
            _prometheus_sample("librarian_queue_failures_total", queue_failures_total),
            "# HELP librarian_queue_wait_ms_average Average durable queue wait in milliseconds.",
            "# TYPE librarian_queue_wait_ms_average gauge",
            _prometheus_sample(
                "librarian_queue_wait_ms_average",
                average_queue_wait_ms,
            ),
            "# HELP librarian_conversion_failures_total Classified conversion failures.",
            "# TYPE librarian_conversion_failures_total counter",
            _prometheus_sample(
                "librarian_conversion_failures_total",
                conversion_failures_total,
            ),
            "# HELP librarian_ocr_pages_total OCR page attempts.",
            "# TYPE librarian_ocr_pages_total counter",
            _prometheus_sample("librarian_ocr_pages_total", ocr_pages_total),
            "# HELP librarian_ocr_failures_total OCR page failures.",
            "# TYPE librarian_ocr_failures_total counter",
            _prometheus_sample("librarian_ocr_failures_total", ocr_failures_total),
            "# HELP librarian_ocr_corrected_pages_total OCR pages corrected by LLM.",
            "# TYPE librarian_ocr_corrected_pages_total counter",
            _prometheus_sample(
                "librarian_ocr_corrected_pages_total",
                ocr_corrected_pages_total,
            ),
            "# HELP librarian_ocr_page_duration_ms_total Total OCR page duration.",
            "# TYPE librarian_ocr_page_duration_ms_total counter",
            _prometheus_sample(
                "librarian_ocr_page_duration_ms_total",
                ocr_page_duration_ms_total,
            ),
            "# HELP librarian_ocr_pages_per_second OCR page throughput.",
            "# TYPE librarian_ocr_pages_per_second gauge",
            _prometheus_sample("librarian_ocr_pages_per_second", ocr_pages_per_second),
            "# HELP librarian_llm_prompt_tokens_total LLM prompt tokens reported by providers.",
            "# TYPE librarian_llm_prompt_tokens_total counter",
            _prometheus_sample(
                "librarian_llm_prompt_tokens_total",
                llm_prompt_tokens_total,
            ),
            (
                "# HELP librarian_llm_completion_tokens_total "
                "LLM completion tokens reported by providers."
            ),
            "# TYPE librarian_llm_completion_tokens_total counter",
            _prometheus_sample(
                "librarian_llm_completion_tokens_total",
                llm_completion_tokens_total,
            ),
            "# HELP librarian_llm_tokens_total LLM total tokens reported by providers.",
            "# TYPE librarian_llm_tokens_total counter",
            _prometheus_sample("librarian_llm_tokens_total", llm_tokens_total),
            "# HELP librarian_llm_estimated_cost_usd_total Estimated LLM cost in USD.",
            "# TYPE librarian_llm_estimated_cost_usd_total counter",
            _prometheus_sample(
                "librarian_llm_estimated_cost_usd_total",
                llm_estimated_cost_usd_total,
            ),
        ]
        lines.extend(
            [
                "# HELP librarian_http_responses_total HTTP responses by status code.",
                "# TYPE librarian_http_responses_total counter",
            ]
        )
        for status_code, count in sorted(status_counts.items()):
            lines.append(
                _prometheus_sample(
                    "librarian_http_responses_total",
                    count,
                    {"status_code": status_code},
                )
            )
        lines.extend(
            [
                (
                    "# HELP librarian_run_stage_duration_ms_total "
                    "Processing duration by stage in milliseconds."
                ),
                "# TYPE librarian_run_stage_duration_ms_total counter",
            ]
        )
        for stage, duration in sorted(run_stage_duration_ms_total.items()):
            lines.append(
                _prometheus_sample(
                    "librarian_run_stage_duration_ms_total",
                    duration,
                    {"stage": stage},
                )
            )
        lines.extend(
            [
                "# HELP librarian_run_stage_total Processing stage executions.",
                "# TYPE librarian_run_stage_total counter",
            ]
        )
        for stage, count in sorted(run_stage_counts.items()):
            lines.append(
                _prometheus_sample(
                    "librarian_run_stage_total",
                    count,
                    {"stage": stage},
                )
            )
        lines.extend(
            [
                "# HELP librarian_conversion_failures_by_type_total Conversion failures by type.",
                "# TYPE librarian_conversion_failures_by_type_total counter",
            ]
        )
        for key, count in sorted(conversion_failures_by_type.items()):
            failure_type, _, source_extension = key.partition(":")
            lines.append(
                _prometheus_sample(
                    "librarian_conversion_failures_by_type_total",
                    count,
                    {"failure_type": failure_type, "source_extension": source_extension},
                )
            )
        lines.extend(
            [
                "# HELP librarian_ocr_pages_by_status_total OCR pages by source/status.",
                "# TYPE librarian_ocr_pages_by_status_total counter",
            ]
        )
        for key, count in sorted(ocr_pages_by_status.items()):
            source, _, status = key.partition(":")
            lines.append(
                _prometheus_sample(
                    "librarian_ocr_pages_by_status_total",
                    count,
                    {"source": source, "status": status},
                )
            )
        lines.extend(
            [
                "# HELP librarian_llm_tokens_by_model_total LLM total tokens by provider/model.",
                "# TYPE librarian_llm_tokens_by_model_total counter",
            ]
        )
        for key, count in sorted(llm_tokens_by_model.items()):
            provider, _, model = key.partition(":")
            lines.append(
                _prometheus_sample(
                    "librarian_llm_tokens_by_model_total",
                    count,
                    {"provider": provider, "model": model},
                )
            )
        lines.extend(
            [
                (
                    "# HELP librarian_llm_estimated_cost_by_model_usd_total "
                    "Estimated LLM cost by model in USD."
                ),
                "# TYPE librarian_llm_estimated_cost_by_model_usd_total counter",
            ]
        )
        for key, cost in sorted(llm_estimated_cost_usd_by_model.items()):
            provider, _, model = key.partition(":")
            lines.append(
                _prometheus_sample(
                    "librarian_llm_estimated_cost_by_model_usd_total",
                    cost,
                    {"provider": provider, "model": model},
                )
            )
        return "\n".join(lines) + "\n"


class NoOpMetricsRecorder:
    """No-op metrics adapter for application services."""

    def record_run_stage(self, *, stage: str, duration_ms: float) -> None:
        """Ignore processing stage metrics."""

    def record_run_finished(self, *, status: str) -> None:
        """Ignore terminal run metrics."""

    def record_queue_claim(self, *, wait_ms: float) -> None:
        """Ignore queue claim metrics."""

    def record_queue_failure(self) -> None:
        """Ignore queue failure metrics."""

    def record_conversion_failure(
        self,
        *,
        failure_type: str,
        source_extension: str,
    ) -> None:
        """Ignore conversion failure metrics."""

    def record_ocr_page(
        self,
        *,
        source: str,
        status: str,
        duration_ms: float,
        corrected: bool = False,
    ) -> None:
        """Ignore OCR page metrics."""

    def record_llm_usage(
        self,
        *,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        estimated_cost_usd: float = 0.0,
    ) -> None:
        """Ignore LLM usage metrics."""


def _prometheus_sample(
    metric: str,
    value: int | float,
    labels: dict[str, str] | None = None,
) -> str:
    label_text = ""
    if labels:
        rendered = ",".join(
            f'{key}="{_prometheus_escape_label(label_value)}"'
            for key, label_value in sorted(labels.items())
        )
        label_text = f"{{{rendered}}}"
    return f"{metric}{label_text} {value}"


def _prometheus_escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
