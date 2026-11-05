import json
from dataclasses import replace
from pathlib import Path

import pytest

from librarian.application.clean_chunks import CleanChunks
from librarian.application.factory import build_container
from librarian.application.quality_report import run_quality_key
from librarian.application.review_items import mark_source, review_items
from librarian.config import Settings
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk, DocumentStatus, RunStatus
from librarian.prompts import PromptCatalog


def test_marker_roundtrip_preserves_source_and_ignores_embedded_markers() -> None:
    inner = mark_source("nested source", unit_id="nested", chunk=0, reasons=["nested"], attempts=1)
    source = "\n  source --> with Unicode café\n" + inner + "\n"
    marked = mark_source(source, unit_id="u0", chunk=4, reasons=["bad --> header"], attempts=3)
    items = review_items(marked)
    assert len(items) == 1
    assert items[0]["source"] == source
    assert items[0]["reasons"] == ["bad --> header"]


class NumericEditor:
    name = "numeric-editor"

    def __init__(self) -> None:
        self.sources: list[str] = []

    async def complete(self, *, user_prompt: str, **_: object) -> str:
        if not user_prompt.lstrip().startswith("{"):
            return json.dumps({"corrections": []})
        payload = json.loads(user_prompt)
        if "source" not in payload:
            return json.dumps({"corrections": []})
        source = payload["source"]
        self.sources.append(source)
        # Deliberately lose protected numeric data only in the second unit.
        if "second" in source:
            return "The second result is unknown."
        return source.replace("teh", "the")


@pytest.mark.asyncio
async def test_partial_unit_checkpoint_retries_only_failed_unit() -> None:
    source = "The first result was teh expected result.\n\nThe second result was 318 units."
    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    editor = NumericEditor()
    cleaner = CleanChunks(
        editor,
        PromptCatalog(),
        "cmos_v8",
        "test",
        verify_semantics=False,
        publish_partial=True,
        unit_target_chars=45,
        max_fidelity_retries=0,
    )
    result = (await cleaner.execute([chunk]))[0]
    items = review_items(result.text)
    assert len(items) == 1
    assert items[0]["source"] == "The second result was 318 units."
    assert "The first result was the expected result." in result.text
    assert not result.fidelity_failed
    assert result.checkpoint[0].accepted_text == "The first result was the expected result."
    editor.sources.clear()
    resumed = replace(cleaner, resume_units={chunk.id: result.checkpoint})
    second = (await resumed.execute([chunk]))[0]
    assert len(editor.sources) == 1
    assert "second" in editor.sources[0]
    assert "The first result was the expected result." in second.text


@pytest.mark.asyncio
async def test_document_publishes_explicit_partial_output_and_review_report(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text(
        "The first result was teh expected result.\n\nThe second result was 318 units."
    )
    container = await build_container(
        Settings(data_dir=tmp_path / "data", cleaning_fidelity_max_retries=0)
    )
    ingested = await container.ingest_document.execute(source)
    editor = NumericEditor()
    cleaner = replace(
        container.process_document.cleaner,
        provider=editor,
        prompt_version="cmos_v8",
        verify_semantics=False,
        publish_partial=True,
        preparation_provider=editor,
        unit_target_chars=45,
    )
    process = replace(container.process_document, cleaner=cleaner, refiner=None)
    run = await process.execute(ingested.document.id)
    assert run.status == RunStatus.SUCCEEDED
    document = await container.repository.get_document(ingested.document.id)
    assert document is not None and document.status == DocumentStatus.READY
    output = await container.repository.get_cleaned_output(ingested.document.id)
    assert output is not None
    assert review_items(output.text)[0]["source"] == "The second result was 318 units."
    assert "The first result was the expected result." in output.text
    quality = json.loads(await container.repository.get_text(run_quality_key(run.id)))
    assert quality["output_available"] is True
    assert quality["cleaning_status"] == "needs-review"
    assert len(quality["needs_review"]) == 1
    assert quality["needs_review"][0]["attempts"] == 1

    editor.sources.clear()
    repeated = await process.execute(ingested.document.id)
    assert repeated.status == RunStatus.SUCCEEDED
    assert len(editor.sources) == 1
    assert "second" in editor.sources[0]
    repeated_quality = json.loads(await container.repository.get_text(run_quality_key(repeated.id)))
    assert repeated_quality["cached_cleaned_chunks"] == 0
    assert repeated_quality["review_units"] == 1
