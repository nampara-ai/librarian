import json
from pathlib import Path

import pytest

from librarian.application.clean_chunks import CleanChunks
from librarian.application.factory import build_container
from librarian.application.review_items import mark_source, review_body, review_items
from librarian.config import Settings
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk
from librarian.prompts import PromptCatalog


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_advisory_review_never_drives_a_rewrite(unavailable: bool) -> None:
    class Editor:
        name = "editor"
        calls = 0

        async def complete(self, **_: object) -> str:
            self.calls += 1
            return "The patient did not improve."

    class Reviewer:
        name = "reviewer"
        calls = 0

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            self.calls += 1
            if unavailable:
                return "garbage"
            pair = json.loads(user_prompt)["pairs"][0]
            return json.dumps({"issues": [{"id": pair["id"], "problem": "Remove not."}]})

    source = "the patient did not improve"
    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    editor, reviewer = Editor(), Reviewer()
    cleaner = CleanChunks(
        editor,
        PromptCatalog(),
        "cmos_v8",
        "test",
        verification_provider=reviewer,
        review_mode="advisory",
    )
    result = (await cleaner.execute([chunk]))[0]
    items = review_items(result.text)
    assert editor.calls == 1
    assert reviewer.calls == (2 if unavailable else 1)
    assert len(items) == 1
    assert items[0]["kind"] == "advisory"
    assert items[0]["source"] == source
    assert items[0]["candidate"] == "The patient did not improve."
    assert items[0]["reasons"] == (["review-unavailable"] if unavailable else ["Remove not."])
    assert not result.fidelity_failed


@pytest.mark.asyncio
async def test_unchanged_prose_skips_advisory_reviewer() -> None:
    class Provider:
        name = "unused-reviewer"
        calls = 0

        async def complete(self, **_: object) -> str:
            self.calls += 1
            return "The patient did not improve."

    source = "The patient did not improve."
    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    provider = Provider()
    cleaner = CleanChunks(provider, PromptCatalog(), "cmos_v8", "test", review_mode="advisory")
    assert (await cleaner.execute([chunk]))[0].text == source
    assert provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("alias, mode", [(None, "advisory"), (True, "strict"), (False, "off")])
async def test_review_mode_legacy_alias(tmp_path: Path, alias: bool | None, mode: str) -> None:
    settings = Settings(data_dir=tmp_path, cleaning_semantic_verification=alias)
    container = await build_container(settings)
    assert container.process_document.cleaner.review_mode == mode


def test_review_context_uses_visible_prose_without_metadata() -> None:
    candidate = "The patient did not improve."
    marked = mark_source(
        candidate,
        original_source="the patient did not improve",
        unit_id="u0",
        chunk=0,
        reasons=["Do not drop not."],
        attempts=1,
    )
    assert review_body("Before. " + marked + " After.") == "Before. " + candidate + " After."


@pytest.mark.asyncio
async def test_unavailable_review_can_resume_without_reediting_accepted_text() -> None:
    from dataclasses import replace

    class Editor:
        name = "editor"
        calls = 0

        async def complete(self, **_: object) -> str:
            self.calls += 1
            return "The patient did not improve."

    class Reviewer:
        name = "reviewer"
        unavailable = True
        calls = 0

        async def complete(self, **_: object) -> str:
            self.calls += 1
            return "bad JSON" if self.unavailable else json.dumps({"issues": []})

    source = "the patient did not improve"
    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    editor, reviewer = Editor(), Reviewer()
    cleaner = CleanChunks(
        editor,
        PromptCatalog(),
        "cmos_v8",
        "test",
        review_mode="advisory",
        verification_provider=reviewer,
    )
    incomplete = (await cleaner.execute([chunk]))[0]
    assert review_items(incomplete.text)[0]["reasons"] == ["review-unavailable"]
    reviewer.unavailable = False
    resumed = replace(cleaner, resume_units={chunk.id: incomplete.checkpoint})
    completed = (await resumed.execute([chunk]))[0]
    assert completed.text == "The patient did not improve."
    assert not review_items(completed.text)
    assert editor.calls == 1
    assert reviewer.calls == 3
