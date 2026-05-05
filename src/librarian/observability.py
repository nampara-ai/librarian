"""Logging and metrics helpers for service adapters."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any


class JsonFormatter(logging.Formatter):
    """Small JSON log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "timestamp": self.formatTime(record, self.datefmt),
        }
        for key in ("request_id", "method", "path", "status_code", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(*, level: str, log_format: str) -> None:
    """Configure root logging once for CLI/API processes."""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(level.upper())


@dataclass(slots=True)
class MetricsRecorder:
    """In-memory process metrics for the API adapter."""

    started_at: float = field(default_factory=time.time)
    requests_total: int = 0
    errors_total: int = 0
    request_duration_ms_total: float = 0.0
    status_counts: dict[str, int] = field(default_factory=lambda: dict[str, int]())

    def record(self, *, status_code: int, duration_ms: float) -> None:
        """Record one HTTP request."""
        self.requests_total += 1
        self.request_duration_ms_total += duration_ms
        if status_code >= 500:
            self.errors_total += 1
        status_key = str(status_code)
        self.status_counts[status_key] = self.status_counts.get(status_key, 0) + 1

    def snapshot(self) -> dict[str, object]:
        """Return current metric values."""
        average_duration = (
            self.request_duration_ms_total / self.requests_total
            if self.requests_total
            else 0.0
        )
        return {
            "uptime_seconds": max(time.time() - self.started_at, 0.0),
            "requests_total": self.requests_total,
            "errors_total": self.errors_total,
            "average_request_duration_ms": average_duration,
            "status_counts": dict(self.status_counts),
        }
