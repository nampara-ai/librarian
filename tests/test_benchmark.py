import json
from pathlib import Path

import pytest

from librarian.application import benchmark as benchmark_module
from librarian.application.benchmark import (
    benchmark_result_json,
    load_benchmark_text,
    run_benchmark_suite,
    synthetic_text,
)
from librarian.application.clean_chunks import CleanChunks
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk
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
    rendered = json.loads(benchmark_result_json(result))
    assert rendered["librarian_version"]
    assert rendered["generated_at"].endswith("+00:00")
    assert rendered["cleaning_prompt_version"] == "cmos_v1"
    assert rendered["summary"]["run_count"] == 2
    assert rendered["summary"]["total_input_chars"] == sum(item.input_chars for item in result.runs)
    assert rendered["summary"]["total_chunks"] == sum(item.chunks for item in result.runs)
    assert rendered["runs"][0]["provider"] == "mock"


def test_load_benchmark_text_from_file(tmp_path: Path) -> None:
    path = tmp_path / "benchmark.txt"
    path.write_text("benchmark text", encoding="utf-8")

    assert load_benchmark_text(path, paragraphs=1, paragraph_chars=10) == "benchmark text"


def test_load_benchmark_text_rejects_oversized_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "benchmark.txt"
    path.write_text("too large", encoding="utf-8")
    monkeypatch.setattr(benchmark_module, "_MAX_BENCHMARK_INPUT_BYTES", 4)

    with pytest.raises(ValueError, match="Benchmark input exceeds configured limit"):
        load_benchmark_text(path, paragraphs=1, paragraph_chars=10)


def test_synthetic_text_rejects_invalid_dimensions() -> None:
    with pytest.raises(ValueError, match="paragraphs must be at least 1"):
        synthetic_text(paragraphs=0, paragraph_chars=10)
    with pytest.raises(ValueError, match="paragraph_chars must be at least 1"):
        synthetic_text(paragraphs=1, paragraph_chars=0)


@pytest.mark.asyncio
async def test_benchmark_suite_rejects_invalid_repeats() -> None:
    cleaner = CleanChunks(
        provider=MockLLMProvider(),
        prompt_catalog=PromptCatalog(),
        prompt_version="cmos_v1",
        model="mock-cleaner",
    )

    with pytest.raises(ValueError, match="repeats must be at least 1"):
        await run_benchmark_suite(
            cleaner=cleaner,
            document_id=DocumentId("doc_benchmark"),
            text="benchmark text",
            policy=ChunkingPolicy(target_chars=1_000, overlap_chars=50, min_chunk_chars=100),
            repeats=0,
        )


@pytest.mark.asyncio
async def test_clean_chunks_bounds_parallel_task_fanout() -> None:
    active = 0
    max_active = 0

    class TrackingProvider:
        name = "tracking"

        async def complete(
            self,
            *,
            system_prompt: str,
            user_prompt: str,
            model: str,
            max_tokens: int,
            temperature: float,
        ) -> str:
            import asyncio

            del system_prompt, model, max_tokens, temperature
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            active -= 1
            return user_prompt

    chunks = [
        Chunk(
            id=ChunkId(f"chk_{index}"),
            document_id=DocumentId("doc_test"),
            ordinal=index,
            text=f"chunk {index}",
            start_char=index,
            end_char=index + 1,
            sha256=f"sha-{index}",
        )
        for index in range(10)
    ]
    cleaner = CleanChunks(
        provider=TrackingProvider(),
        prompt_catalog=PromptCatalog(),
        prompt_version="cmos_v1",
        model="mock-cleaner",
        max_parallel_chunks=2,
    )

    result = await cleaner.execute(chunks)

    assert [item.chunk.ordinal for item in result] == list(range(10))
    assert max_active <= 2
