import json

import pytest

from librarian.application.clean_chunks import CleanChunks
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk
from librarian.pipeline.validation import validate_cleaned_text
from librarian.prompts import PromptCatalog


class ContextCleaner(CleanChunks):
    async def review(
        self, pairs: list[dict[str, str]], verified: dict[str, str]
    ) -> tuple[str | None, dict[str, list[str]], str]:
        return await self._verify_units(pairs, verified)


@pytest.mark.asyncio
async def test_changed_neighbour_invalidates_an_unchanged_units_review() -> None:
    class Reviewer:
        name = "context-review"
        calls = 0

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            self.calls += 1
            payload = json.loads(user_prompt)
            pair = payload.get("pair") or payload["pairs"][0]
            return json.dumps(
                {
                    "issues": [{"id": pair["id"], "problem": "Repair the broken join."}]
                    if pair.get("edited_context_before") == "And so"
                    else []
                }
            )

    reviewer = Reviewer()
    cleaner = ContextCleaner(reviewer, PromptCatalog(), "cmos_v8", "test")
    pair = {
        "id": "u0001",
        "source": "but here is the result",
        "edited": "But here is the result.",
        "edited_context_before": "The introduction ends.",
    }
    verified: dict[str, str] = {}
    assert (await cleaner.review([pair], verified))[0] is None
    assert (await cleaner.review([pair], verified))[0] is None
    assert reviewer.calls == 1
    changed = {**pair, "edited_context_before": "And so"}
    assert (await cleaner.review([changed], verified))[0] == "editorial-quality-failed"
    assert reviewer.calls == 3


@pytest.mark.asyncio
async def test_source_and_edited_context_are_not_conflated_between_chunks() -> None:
    class Editor:
        name = "editor"

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            return json.loads(user_prompt)["source"].replace("Teh", "The")

    class Reviewer:
        name = "reviewer"
        saw_following = False

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            pair = json.loads(user_prompt)["pairs"][0]
            if pair["source"] == "This follows.":
                self.saw_following = True
                assert pair["context_before"] == "Teh lead ended."
                assert pair["edited_context_before"] == "The lead ended."
            return '{"issues": []}'

    chunks = [
        Chunk(ChunkId(f"c{i}"), DocumentId("doc"), i, text, 0, len(text), "source")
        for i, text in enumerate(["Teh lead ended.", "This follows."])
    ]
    reviewer = Reviewer()
    cleaner = CleanChunks(
        Editor(), PromptCatalog(), "cmos_v8", "test", verification_provider=reviewer
    )
    await cleaner.execute(chunks)
    assert reviewer.saw_following


@pytest.mark.asyncio
async def test_parallel_group_starts_receive_original_source_context() -> None:
    class Editor:
        name = "editor"
        saw_context = False

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if payload["source"] == "The next group begins.":
                self.saw_context = True
                assert payload["context_before"] == "The first group ends."
            return payload["source"]

    chunks = [
        Chunk(ChunkId(f"g{i}"), DocumentId("doc"), i, text, 0, len(text), "source")
        for i, text in enumerate(["The first group ends.", "The next group begins."])
    ]
    editor = Editor()
    cleaner = CleanChunks(
        editor, PromptCatalog(), "cmos_v8", "test", balanced_group_size=1, verify_semantics=False
    )
    await cleaner.execute(chunks)
    assert editor.saw_context


def test_new_lowercase_paragraph_is_rejected_without_changing_source_voice() -> None:
    source = "This explains the rule. But another example follows."
    candidate = "This explains the rule.\n\nbut another example follows."
    assert (
        "new-lowercase-paragraph"
        in validate_cleaned_text(candidate, input_size=len(source), source_text=source).warnings
    )
    for original in [
        candidate,
        "A quoted fragment.\n\n“but another example follows”",
        "```text\nThis explains the rule.\n\nbut another example follows.\n```",
    ]:
        assert (
            "new-lowercase-paragraph"
            not in validate_cleaned_text(
                original, input_size=len(original), source_text=original
            ).warnings
        )
