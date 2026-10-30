from dataclasses import replace

import pytest

from librarian.application.clean_chunks import CleanChunks, CleanedChunk, CoherenceMode
from librarian.domain.ids import DocumentId
from librarian.llm.mock import MockLLMProvider
from librarian.pipeline.chunking import ChunkingPolicy, chunk_text
from librarian.prompts import PromptCatalog


def test_cache_fingerprint_tracks_context_and_editorial_policy() -> None:
    cleaner = CleanChunks(MockLLMProvider(), PromptCatalog(), "cmos_v6", "test")
    key = cleaner.cache_version("Original document")
    assert key == cleaner.cache_version("Original document")
    assert key != cleaner.cache_version("Different neighboring passages")
    assert key != replace(cleaner, verify_semantics=False).cache_version("Original document")
    assert key != replace(cleaner, provider_endpoint="https://example.test/v1").cache_version(
        "Original document"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fast", "balanced", "max-coherence"])
async def test_resume_keeps_original_neighbors_and_reports_only_new_work(
    mode: CoherenceMode,
) -> None:
    class Provider(MockLLMProvider):
        def __init__(self) -> None:
            self.requests: list[str] = []

        async def complete(self, **kwargs: object) -> str:
            prompt = str(kwargs["user_prompt"])
            self.requests.append(prompt)
            return prompt.split("]\n\n", 1)[-1] if prompt.startswith("[CONTEXT:") else prompt

    source = "First source passage.\n\nMiddle source passage.\n\nLast source passage."
    chunks = chunk_text(
        DocumentId("cache-test"),
        source,
        ChunkingPolicy(target_chars=25, overlap_chars=0, min_chunk_chars=0),
    )
    assert len(chunks) == 3
    provider = Provider()
    cleaner = CleanChunks(provider, PromptCatalog(), "cmos_v5", "test", coherence_mode=mode)
    first = CleanedChunk(chunks[0], chunks[0].text, ())
    last = CleanedChunk(chunks[2], chunks[2].text, ())
    completed: list[CleanedChunk] = []

    async def on_cleaned(result: CleanedChunk) -> None:
        completed.append(result)

    results = await cleaner.execute(
        chunks, cached_chunks=[first, last], on_chunk_cleaned=on_cleaned
    )
    assert [result.chunk.id for result in results] == [chunk.id for chunk in chunks]
    assert [item.chunk.id for item in completed] == [chunks[1].id]
    assert len(provider.requests) == 1
    assert "First source passage." in provider.requests[0]
    assert "Last source passage." not in provider.requests[0]
