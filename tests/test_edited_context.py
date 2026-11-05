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
            if "pair" in payload and "checks" in payload:
                return json.dumps(
                    {
                        "checks": [
                            {"id": check["id"], "confirmed": True, "reason": check["problem"]}
                            for check in payload["checks"]
                        ]
                    }
                )
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
                assert pair["edited_context_before"] == "The lead ended.\n\n"
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


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fast", "balanced", "max-coherence"])
async def test_every_coherence_mode_receives_forward_source_context(mode: str) -> None:
    from typing import cast

    from librarian.application.clean_chunks import CoherenceMode

    class Provider:
        name = "forward-context"

        def __init__(self) -> None:
            self.edits: list[dict[str, object]] = []
            self.pairs: list[dict[str, str]] = []

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "pairs" in payload:
                self.pairs.extend(payload["pairs"])
                return '{"issues": []}'
            self.edits.append(payload)
            return payload["source"]

    texts = ["The first clause", "continues through this clause", "and ends here."]
    source = " ".join(texts)
    chunks: list[Chunk] = []
    offset = 0
    for index, text in enumerate(texts):
        chunks.append(
            Chunk(
                ChunkId(str(index)),
                DocumentId("forward"),
                index,
                text,
                offset,
                offset + len(text),
                "source",
            )
        )
        offset += len(text) + 1
    provider = Provider()
    cleaner = CleanChunks(
        provider,
        PromptCatalog(),
        "cmos_v8",
        "test",
        document_source=source,
        coherence_mode=cast(CoherenceMode, mode),
        balanced_group_size=1,
    )
    await cleaner.execute(chunks)
    first = next(edit for edit in provider.edits if edit["source"] == texts[0])
    assert first["context_after"] == " " + texts[1]
    assert first["continuation_after"] is True
    first_pair = next(pair for pair in provider.pairs if pair["source"] == texts[0])
    assert first_pair["context_after"] == " " + texts[1]
    assert first_pair["edited_context_after"] == ""


@pytest.mark.asyncio
async def test_cached_predecessor_is_available_as_actual_draft_context_in_fast_mode() -> None:
    from librarian.application.clean_chunks import CleanedChunk

    class Provider:
        name = "cached-context"

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "pairs" in payload:
                pair = payload["pairs"][0]
                assert pair["context_before"] == "Teh first clause"
                assert pair["edited_context_before"] == "The first clause "
                return '{"issues": []}'
            assert payload["edited_context_before"] == "The first clause "
            return payload["source"]

    source = "Teh first clause continues here."
    first = Chunk(ChunkId("c0"), DocumentId("doc"), 0, "Teh first clause", 0, 16, "first")
    second = Chunk(ChunkId("c1"), DocumentId("doc"), 1, "continues here.", 17, len(source), "last")
    cleaner = CleanChunks(
        Provider(),
        PromptCatalog(),
        "cmos_v8",
        "test",
        document_source=source,
        coherence_mode="fast",
    )
    results = await cleaner.execute(
        [first, second], cached_chunks=[CleanedChunk(first, "The first clause", ())]
    )
    assert [result.text for result in results] == ["The first clause", "continues here."]


def test_cached_numeric_reading_uses_the_same_document_context_as_generation() -> None:
    from librarian.application.clean_chunks import CleanedChunk

    source = "We lived during the '7s, a decade of change."
    start = source.index("'7s")
    chunk = Chunk(ChunkId("decade"), DocumentId("doc"), 1, "'7s", start, start + 3, "source")
    cleaner = CleanChunks(object(), PromptCatalog(), "cmos_v8", "test", document_source=source)  # type: ignore[arg-type]
    checked = cleaner.revalidate(CleanedChunk(chunk, "’70s", ()))
    assert not checked.fidelity_failed
    assert "repaired-decade-shorthand" in checked.warnings
