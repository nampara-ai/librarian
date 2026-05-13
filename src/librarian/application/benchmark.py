"""Benchmark harness for deterministic pipeline components."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from librarian.application.clean_chunks import CleanChunks
from librarian.domain.ids import DocumentId
from librarian.pipeline.chunking import ChunkingPolicy, chunk_text
from librarian.version import __version__

_MAX_BENCHMARK_INPUT_BYTES = 100 * 1024 * 1024


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
    generated_at: datetime
    librarian_version: str
    cleaning_prompt_version: str

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
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
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
    return BenchmarkSuiteResult(
        runs=tuple(runs),
        generated_at=datetime.now(UTC),
        librarian_version=__version__,
        cleaning_prompt_version=cleaner.prompt_version,
    )


def benchmark_result_json(result: BenchmarkSuiteResult) -> str:
    """Render benchmark results as JSON."""
    return json.dumps(
        {
            "generated_at": result.generated_at.isoformat(),
            "librarian_version": result.librarian_version,
            "cleaning_prompt_version": result.cleaning_prompt_version,
            "average_chars_per_second": result.average_chars_per_second,
            "fastest_chars_per_second": result.fastest_chars_per_second,
            "summary": _benchmark_summary(result),
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


def _benchmark_summary(result: BenchmarkSuiteResult) -> dict[str, object]:
    return {
        "run_count": len(result.runs),
        "average_chars_per_second": result.average_chars_per_second,
        "fastest_chars_per_second": result.fastest_chars_per_second,
        "total_input_chars": sum(item.input_chars for item in result.runs),
        "total_chunks": sum(item.chunks for item in result.runs),
        "total_seconds": sum(item.total_seconds for item in result.runs),
    }


def load_benchmark_text(path: Path | None, *, paragraphs: int, paragraph_chars: int) -> str:
    """Load benchmark text from disk or generate deterministic synthetic text."""
    if path is not None:
        return _read_limited_text_file(path, max_bytes=_MAX_BENCHMARK_INPUT_BYTES)
    return synthetic_text(paragraphs=paragraphs, paragraph_chars=paragraph_chars)


def _read_limited_text_file(path: Path, *, max_bytes: int) -> str:
    with path.open("rb") as handle:
        payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"Benchmark input exceeds configured limit of {max_bytes} bytes: {path}")
    return payload.decode("utf-8")


def synthetic_text(*, paragraphs: int, paragraph_chars: int) -> str:
    """Generate deterministic benchmark text."""
    if paragraphs < 1:
        raise ValueError("paragraphs must be at least 1")
    if paragraph_chars < 1:
        raise ValueError("paragraph_chars must be at least 1")
    seed = (
        "This is a rough transcript paragraph about library processing, "
        "classification, chunking, and cleaning. "
    )
    paragraph = (seed * ((paragraph_chars // len(seed)) + 1))[:paragraph_chars]
    return "\n\n".join(f"{index}. {paragraph}" for index in range(paragraphs))
