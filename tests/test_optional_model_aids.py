import json
from dataclasses import replace
from pathlib import Path

import pytest

from librarian.application.classify_document import ClassifyDocument
from librarian.application.clean_chunks import CleanChunks
from librarian.application.errors import LLMAccessError, LLMResponseTruncatedError
from librarian.application.factory import build_container
from librarian.application.quality_report import run_quality_key
from librarian.config import Settings
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk, RunStatus
from librarian.prompts import PromptCatalog
from librarian.taxonomy.dewey import DeweyTaxonomy


@pytest.mark.asyncio
async def test_invalid_optional_spelling_sheet_does_not_fail_prose_processing(
    tmp_path: Path,
) -> None:
    class Editor:
        name = "editor"

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            if not user_prompt.lstrip().startswith("{"):
                return "not JSON"
            return json.loads(user_prompt)["source"].replace("teh", "The")

    class Reviewer:
        name = "reviewer"

        async def complete(self, **_: object) -> str:
            return json.dumps({"issues": []})

    source = tmp_path / "notes.txt"
    source.write_text("teh patient rested.")
    container = await build_container(Settings(data_dir=tmp_path / "data"))
    ingested = await container.ingest_document.execute(source)
    editor = Editor()
    process = replace(
        container.process_document,
        refiner=None,
        cleaner=replace(
            container.process_document.cleaner,
            provider=editor,
            preparation_provider=editor,
            verification_provider=Reviewer(),
            prompt_version="cmos_v8",
        ),
    )
    run = await process.execute(ingested.document.id)
    assert run.status == RunStatus.SUCCEEDED
    output = await container.repository.get_cleaned_output(ingested.document.id)
    assert output is not None and output.text == "The patient rested."
    quality = json.loads(await container.repository.get_text(run_quality_key(run.id)))
    assert "spelling-preparation-unavailable" in quality["warnings"]


@pytest.mark.asyncio
@pytest.mark.parametrize("access_failure", [False, True])
async def test_source_reading_is_optional_but_access_failures_remain_terminal(
    access_failure: bool,
) -> None:
    class Editor:
        name = "editor"
        calls = 0

        async def complete(self, **_: object) -> str:
            self.calls += 1
            return "The patient rested."

    class Reviewer:
        name = "reviewer"
        planning_calls = 0

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "pairs" not in payload:
                self.planning_calls += 1
                if access_failure:
                    raise LLMAccessError("credentials unavailable", reason="authentication")
                return "not JSON"
            return json.dumps({"issues": []})

    source = "teh patient rested."
    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    editor, reviewer = Editor(), Reviewer()
    cleaner = CleanChunks(
        editor,
        PromptCatalog(),
        "cmos_v8",
        "test",
        source_reading=True,
        verification_provider=reviewer,
        review_mode="strict",
    )
    if access_failure:
        with pytest.raises(LLMAccessError):
            await cleaner.execute([chunk])
        assert editor.calls == 0
        assert reviewer.planning_calls == 1
    else:
        result = (await cleaner.execute([chunk]))[0]
        assert result.text == "The patient rested."
        assert "source-reading-unavailable" in result.warnings
        assert not result.fidelity_failed
        assert editor.calls == 1
        assert reviewer.planning_calls == 2


@pytest.mark.asyncio
async def test_truncated_classification_uses_existing_metadata_fallback() -> None:
    class Provider:
        name = "truncated"

        async def complete(self, **_: object) -> str:
            raise LLMResponseTruncatedError("length", partial_text='{"title":')

    classifier = ClassifyDocument(Provider(), PromptCatalog(), "dewey_v5", "test", DeweyTaxonomy())
    result = await classifier.execute(DocumentId("d"), "Horse training notes.")
    assert result.code == "636.1"
    assert result.title is None
