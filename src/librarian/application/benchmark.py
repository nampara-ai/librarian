"""Benchmark harness for deterministic pipeline components."""

from __future__ import annotations

import time
from dataclasses import dataclass

from librarian.application.clean_chunks import CleanChunks
from librarian.domain.ids import DocumentId
from librarian.pipeline.chunking import ChunkingPolicy, chunk_text


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """Benchmark measurements."""

    input_chars: int
    chunks: int
    chunking_seconds: float
    cleaning_seconds: float
    chars_per_second: float


async def run_benchmark(
    *,
    cleaner: CleanChunks,
    document_id: DocumentId,
    text: str,
    policy: ChunkingPolicy,
) -> BenchmarkResult:
    """Run a small benchmark over chunking and cleaning."""
    start = time.perf_counter()
    chunks = chunk_text(document_id, text, policy)
    after_chunking = time.perf_counter()
    await cleaner.execute(chunks)
    after_cleaning = time.perf_counter()
    total_seconds = max(after_cleaning - start, 1e-9)
    return BenchmarkResult(
        input_chars=len(text),
        chunks=len(chunks),
        chunking_seconds=after_chunking - start,
        cleaning_seconds=after_cleaning - after_chunking,
        chars_per_second=len(text) / total_seconds,
    )


def synthetic_text(*, paragraphs: int, paragraph_chars: int) -> str:
    """Generate deterministic benchmark text."""
    seed = (
        "This is a rough transcript paragraph about library processing, "
        "classification, chunking, and cleaning. "
    )
    paragraph = (seed * ((paragraph_chars // len(seed)) + 1))[:paragraph_chars]
    return "\n\n".join(f"{index}. {paragraph}" for index in range(paragraphs))
