"""Benchmark harness for deterministic pipeline components."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from librarian.application.clean_chunks import CleanChunks
from librarian.domain.ids import DocumentId
from librarian.pipeline.chunking import ChunkingPolicy, chunk_text


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """Benchmark measurements."""

    provider: str
    model: str
    input_chars: int
    chunks: int
    chunking_seconds: float
    cleaning_seconds: float
    total_seconds: float
    chars_per_second: float
    chunk_target_chars: int
    chunk_overlap_chars: int


@dataclass(frozen=True, slots=True)
class BenchmarkSuiteResult:
    """Repeated benchmark measurements."""

    runs: tuple[BenchmarkResult, ...]

    @property
    def average_chars_per_second(self) -> float:
        """Average throughput across runs."""
        if not self.runs:
            return 0.0
        return sum(item.chars_per_second for item in self.runs) / len(self.runs)

    @property
    def fastest_chars_per_second(self) -> float:
        """Fastest throughput across runs."""
        if not self.runs:
            return 0.0
        return max(item.chars_per_second for item in self.runs)


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
        provider=cleaner.provider.name,
        model=cleaner.model,
        input_chars=len(text),
        chunks=len(chunks),
        chunking_seconds=after_chunking - start,
        cleaning_seconds=after_cleaning - after_chunking,
        total_seconds=total_seconds,
        chars_per_second=len(text) / total_seconds,
        chunk_target_chars=policy.target_chars,
        chunk_overlap_chars=policy.overlap_chars,
    )


async def run_benchmark_suite(
    *,
    cleaner: CleanChunks,
    document_id: DocumentId,
    text: str,
    policy: ChunkingPolicy,
    repeats: int,
) -> BenchmarkSuiteResult:
    """Run repeated benchmark measurements."""
    runs: list[BenchmarkResult] = []
    for index in range(repeats):
        runs.append(
            await run_benchmark(
                cleaner=cleaner,
                document_id=DocumentId(f"{document_id}_{index}"),
                text=text,
                policy=policy,
            )
        )
    return BenchmarkSuiteResult(runs=tuple(runs))


def benchmark_result_json(result: BenchmarkSuiteResult) -> str:
    """Render benchmark results as JSON."""
    return json.dumps(
        {
            "average_chars_per_second": result.average_chars_per_second,
            "fastest_chars_per_second": result.fastest_chars_per_second,
            "runs": [
                {
                    "provider": item.provider,
                    "model": item.model,
                    "input_chars": item.input_chars,
                    "chunks": item.chunks,
                    "chunking_seconds": item.chunking_seconds,
                    "cleaning_seconds": item.cleaning_seconds,
                    "total_seconds": item.total_seconds,
                    "chars_per_second": item.chars_per_second,
                    "chunk_target_chars": item.chunk_target_chars,
                    "chunk_overlap_chars": item.chunk_overlap_chars,
                }
                for item in result.runs
            ],
        },
        indent=2,
    )


def load_benchmark_text(path: Path | None, *, paragraphs: int, paragraph_chars: int) -> str:
    """Load benchmark text from disk or generate deterministic synthetic text."""
    if path is not None:
        return path.read_text(encoding="utf-8")
    return synthetic_text(paragraphs=paragraphs, paragraph_chars=paragraph_chars)


def synthetic_text(*, paragraphs: int, paragraph_chars: int) -> str:
    """Generate deterministic benchmark text."""
    seed = (
        "This is a rough transcript paragraph about library processing, "
        "classification, chunking, and cleaning. "
    )
    paragraph = (seed * ((paragraph_chars // len(seed)) + 1))[:paragraph_chars]
    return "\n\n".join(f"{index}. {paragraph}" for index in range(paragraphs))
