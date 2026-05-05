"""Stable identifier helpers."""

from __future__ import annotations

import hashlib
from typing import NewType

DocumentId = NewType("DocumentId", str)
RunId = NewType("RunId", str)
ChunkId = NewType("ChunkId", str)


def digest_text(prefix: str, text: str, *, length: int = 16) -> str:
    """Return a stable prefixed SHA-256 digest for text content."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def digest_bytes(prefix: str, payload: bytes, *, length: int = 16) -> str:
    """Return a stable prefixed SHA-256 digest for bytes."""
    digest = hashlib.sha256(payload).hexdigest()[:length]
    return f"{prefix}_{digest}"
