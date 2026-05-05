import pytest

from librarian.application.benchmark import run_benchmark, synthetic_text
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

    result = await run_benchmark(
        cleaner=cleaner,
        document_id=DocumentId("doc_benchmark"),
        text=synthetic_text(paragraphs=5, paragraph_chars=500),
        policy=ChunkingPolicy(target_chars=1_000, overlap_chars=50, min_chunk_chars=100),
    )

    assert result.input_chars > 0
    assert result.chunks > 0
    assert result.chars_per_second > 0
