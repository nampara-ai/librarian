import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from librarian.application.clean_chunks import CleanChunks, FidelityValidationError
from librarian.application.errors import LLMResponseTruncatedError
from librarian.application.factory import build_container
from librarian.application.quality_report import run_quality_key
from librarian.config import Settings
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk
from librarian.pipeline.editing import editing_units
from librarian.prompts import PromptCatalog


@pytest.mark.asyncio
async def test_truncation_retries_smaller_spans_and_retains_successful_units() -> None:
    class Provider:
        name = "size-limited"
        calls: Counter[str] = Counter()

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            unit = json.loads(user_prompt)["units"][0]
            self.calls[unit["text"]] += 1
            if len(unit["text"]) > 2700:
                raise LLMResponseTruncatedError("Token limit", partial_text='{"edits": [')
            return json.dumps({"edits": [{"id": unit["id"], "text": unit["text"]}]})

    source = " ".join(f"Measurement {i} remained within the stated tolerance." for i in range(110))
    units = editing_units(source)
    assert any(len(unit.text) > 2700 for unit in units)
    short = next(unit for unit in units if len(unit.text) <= 2700)
    chunk = Chunk(ChunkId("truncated"), DocumentId("doc"), 0, source, 0, len(source), "source")
    provider = Provider()
    cleaner = CleanChunks(provider, PromptCatalog(), "cmos_v6", "test", verify_semantics=False)
    results = await cleaner.execute([chunk])
    assert results[0].text.split() == source.split()
    assert provider.calls[short.text] == 1
    assert not results[0].fidelity_failed


@pytest.mark.asyncio
async def test_repeated_truncation_exhausts_budget_without_publishing_partial_text() -> None:
    class Provider:
        name = "truncated"
        calls = 0

        async def complete(self, **_: object) -> str:
            self.calls += 1
            raise LLMResponseTruncatedError("Token limit", partial_text="Incomplete candidate")

    source = "The test did not establish that the treatment was effective."
    chunk = Chunk(ChunkId("truncated"), DocumentId("doc"), 0, source, 0, len(source), "source")
    provider = Provider()
    cleaner = CleanChunks(provider, PromptCatalog(), "cmos_v6", "test", max_fidelity_retries=1)
    with pytest.raises(FidelityValidationError, match="truncated-model-output"):
        await cleaner.execute([chunk])
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_preparation_failure_keeps_chunk_total_and_failure_report(tmp_path: Path) -> None:
    class Preparation:
        name = "preparation"

        async def complete(self, **_: object) -> str:
            raise RuntimeError("Preparation failed")

    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "test.sqlite",
        cleaning_prompt_version="cmos_v6",
        chunk_target_chars=180,
        chunk_overlap_chars=20,
    )
    source = tmp_path / "source.txt"
    source.write_text("An independent observation needs careful explanation. " * 20)
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    process = replace(
        container.process_document,
        cleaner=replace(container.process_document.cleaner, preparation_provider=Preparation()),
    )
    queued = await process.start(ingested.document.id)
    with pytest.raises(RuntimeError, match="Preparation failed"):
        await process.execute_existing(queued.id)
    run = await container.repository.get_run(queued.id)
    assert run is not None and run.total_chunks > 1
    report = json.loads(await container.repository.get_text(run_quality_key(queued.id)))
    assert report["chunks"] == run.total_chunks
    assert report["fatal"] == ["cleaning-failed"]
