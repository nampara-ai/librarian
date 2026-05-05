from pathlib import Path

import pytest

from librarian.application.benchmark import (
    benchmark_result_json,
    load_benchmark_text,
    run_benchmark_suite,
    synthetic_text,
)
from librarian.application.clean_chunks import CleanChunks
from librarian.domain.ids import DocumentId
from librarian.llm.mock import MockLLMProvider
from librarian.pipeline.chunking import ChunkingPolicy
from librarian.prompts import PromptCatalog


@pytest.mark.asyncio
async def test_benchmark_reports_throughput() -> None:
    cleaner = CleanChunks(
        provider=MockLLMProvider(),
        prompt_catalog=PromptCatalog(),
        prompt_version="cmos_v1",
        model="mock-cleaner",
    )

    result = await run_benchmark_suite(
        cleaner=cleaner,
        document_id=DocumentId("doc_benchmark"),
        text=synthetic_text(paragraphs=5, paragraph_chars=500),
        policy=ChunkingPolicy(target_chars=1_000, overlap_chars=50, min_chunk_chars=100),
        repeats=2,
    )

    assert len(result.runs) == 2
    assert result.runs[0].input_chars > 0
    assert result.runs[0].chunks > 0
    assert result.average_chars_per_second > 0
    assert '"provider": "mock"' in benchmark_result_json(result)


def test_load_benchmark_text_from_file(tmp_path: Path) -> None:
    path = tmp_path / "benchmark.txt"
    path.write_text("benchmark text", encoding="utf-8")

    assert load_benchmark_text(path, paragraphs=1, paragraph_chars=10) == "benchmark text"
