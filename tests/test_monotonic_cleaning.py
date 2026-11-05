import json
from collections import Counter
from typing import Literal

import pytest

from librarian.application.clean_chunks import CleanChunks
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk
from librarian.prompts import PromptCatalog


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["advisory", "strict"])
async def test_passed_units_are_never_edited_or_reviewed_again(
    mode: Literal["advisory", "strict"],
) -> None:
    class Editor:
        name = "editor"
        calls: Counter[str] = Counter()

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            source = payload["source"]
            name = next(label for label in ("first", "second", "third") if label in source)
            self.calls[name] += 1
            if name == "second" and self.calls[name] < 3:
                return "The second result was unknown."
            return source.replace("teh", "The").replace("the second", "The second")

    class Reviewer:
        name = "reviewer"
        calls: Counter[str] = Counter()

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            pair = json.loads(user_prompt)["pairs"][0]
            name = next(label for label in ("first", "second", "third") if label in pair["source"])
            self.calls[name] += 1
            return json.dumps({"issues": []})

    source = (
        "teh first result stands.\n\nthe second result was 318 units.\n\nteh third result stands."
    )
    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    editor, reviewer = Editor(), Reviewer()
    cleaner = CleanChunks(
        editor,
        PromptCatalog(),
        "cmos_v8",
        "test",
        review_mode=mode,
        verification_provider=reviewer,
        unit_target_chars=45,
        unit_max_retries=2,
    )
    result = (await cleaner.execute([chunk]))[0]
    assert editor.calls == {"first": 1, "second": 3, "third": 1}
    assert reviewer.calls == {"first": 1, "second": 1, "third": 1}
    assert result.text == (
        "The first result stands.\n\nThe second result was 318 units.\n\nThe third result stands."
    )


@pytest.mark.asyncio
async def test_passed_review_checkpoints_before_other_reviewer_fails() -> None:
    import asyncio
    from dataclasses import replace

    from librarian.application.errors import LLMAccessError
    from librarian.application.review_items import UnitCheckpoint

    checkpoint_written = asyncio.Event()
    snapshots: list[tuple[UnitCheckpoint, ...]] = []

    class Editor:
        name = "editor"
        sources: list[str] = []

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            source = json.loads(user_prompt)["source"]
            self.sources.append(source)
            return source.replace("teh", "The")

    class Reviewer:
        name = "reviewer"
        recover = False

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            source = json.loads(user_prompt)["pairs"][0]["source"]
            if "second" in source and not self.recover:
                await checkpoint_written.wait()
                raise LLMAccessError("provider unavailable", reason="authentication")
            return json.dumps({"issues": []})

    async def persist(_chunk: Chunk, units: tuple[UnitCheckpoint, ...]) -> None:
        snapshots.append(units)
        checkpoint_written.set()

    source = "teh first result stands.\n\nteh second result stands."
    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    editor, reviewer = Editor(), Reviewer()
    cleaner = CleanChunks(
        editor,
        PromptCatalog(),
        "cmos_v8",
        "test",
        unit_target_chars=30,
        review_mode="strict",
        verification_provider=reviewer,
        on_unit_checkpoint=persist,
    )
    with pytest.raises(LLMAccessError):
        await cleaner.execute([chunk])
    saved = snapshots[-1]
    assert saved[0].accepted_text == "The first result stands."
    assert saved[1].accepted_text is None
    reviewer.recover = True
    editor.sources.clear()
    resumed = replace(cleaner, resume_units={chunk.id: saved})
    result = (await resumed.execute([chunk]))[0]
    assert editor.sources == ["teh second result stands."]
    assert result.text == "The first result stands.\n\nThe second result stands."


@pytest.mark.asyncio
async def test_parallel_approvals_cannot_overwrite_a_newer_checkpoint() -> None:
    import asyncio

    from librarian.application.review_items import UnitCheckpoint

    completions: list[int] = []

    class Editor:
        name = "editor"

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            return json.loads(user_prompt)["source"].replace("teh", "The")

    class Reviewer:
        name = "reviewer"

        async def complete(self, **_: object) -> str:
            return json.dumps({"issues": []})

    async def persist(_chunk: Chunk, units: tuple[UnitCheckpoint, ...]) -> None:
        accepted = sum(u.accepted_text is not None for u in units)
        if accepted == 1:
            await asyncio.sleep(0.02)
        completions.append(accepted)

    source = "teh first result stands.\n\nteh second result stands."
    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    cleaner = CleanChunks(
        Editor(),
        PromptCatalog(),
        "cmos_v8",
        "test",
        unit_target_chars=30,
        review_mode="strict",
        verification_provider=Reviewer(),
        on_unit_checkpoint=persist,
    )
    await cleaner.execute([chunk])
    assert completions == [1, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["strict", "advisory"])
async def test_review_can_approve_filler_reduction_without_reopening_it(
    mode: Literal["strict", "advisory"],
) -> None:
    body = " ".join(
        f"The archive entry {letter} remains unchanged." for letter in "abcdefghijklmnopqrstuvwxyz"
    )
    first = "um " * 80 + body
    source = first + "\n\nThe second result was 318 units."

    class Editor:
        name = "editor"
        first_calls = 0
        second_calls = 0

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            text = json.loads(user_prompt)["source"]
            if "second" in text:
                self.second_calls += 1
                return "The second result is unknown." if self.second_calls < 3 else text
            self.first_calls += 1
            return body

    class Reviewer:
        name = "reviewer"
        first_calls = 0

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            if "archive" in json.loads(user_prompt)["pairs"][0]["source"]:
                self.first_calls += 1
            return json.dumps({"issues": []})

    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    editor, reviewer = Editor(), Reviewer()
    cleaner = CleanChunks(
        editor,
        PromptCatalog(),
        "cmos_v8",
        "test",
        review_mode=mode,
        verification_provider=reviewer,
        unit_target_chars=len(first),
        unit_max_retries=2,
    )
    result = (await cleaner.execute([chunk]))[0]
    assert editor.first_calls == reviewer.first_calls == 1
    assert editor.second_calls == 3
    assert result.text == body + "\n\nThe second result was 318 units."
    assert not result.fidelity_failed


@pytest.mark.asyncio
@pytest.mark.parametrize("stubborn", [False, True])
async def test_merged_failures_preserve_but_cannot_inflate_unspent_unit_budgets(
    stubborn: bool,
) -> None:
    from librarian.application.review_items import review_items

    class Editor:
        name = "editor"
        calls = 0

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            self.calls += 1
            payload = json.loads(user_prompt)
            if "candidate_spans" not in payload:
                return (
                    payload["source"].replace("teh", "The").replace("result stood", "item stayed")
                )
            if stubborn:
                span = payload["candidate_spans"][0]
                old = "item stayed" if "item stayed" in span["text"] else "item remained"
                new = "item remained" if old == "item stayed" else "item stayed"
                return json.dumps({"patches": [{"id": span["id"], "find": old, "replace": new}]})
            span = next(s for s in payload["candidate_spans"] if "item stayed" in s["text"])
            return json.dumps(
                {"patches": [{"id": span["id"], "find": "item stayed", "replace": "result stood"}]}
            )

    class Reviewer:
        name = "reviewer"

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "checks" in payload:
                return json.dumps(
                    {
                        "checks": [
                            {"id": c["id"], "confirmed": True, "reason": c["problem"]}
                            for c in payload["checks"]
                        ]
                    }
                )
            pair = payload["pairs"][0]
            return json.dumps(
                {
                    "issues": [
                        {
                            "id": pair["id"],
                            "problem": "Restore result stood in the altered clauses.",
                        }
                    ]
                    if "item stayed" in pair["edited"] or "item remained" in pair["edited"]
                    else []
                }
            )

    source = "teh first result stood.\n\nteh second result stood."
    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    editor = Editor()
    cleaner = CleanChunks(
        editor,
        PromptCatalog(),
        "cmos_v8",
        "test",
        verification_provider=Reviewer(),
        review_mode="strict",
        unit_target_chars=30,
        unit_max_retries=1,
        publish_partial=True,
    )
    result = (await cleaner.execute([chunk]))[0]
    # Two original units * (one generation + one retry) = four total calls.
    assert editor.calls == 4
    if stubborn:
        assert review_items(result.text)[0]["source"] == source
    else:
        assert result.text == "The first result stood.\n\nThe second result stood."
        assert not review_items(result.text)
