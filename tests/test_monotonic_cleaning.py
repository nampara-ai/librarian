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

    from dataclasses import replace

    from librarian.application.review_items import UnitCheckpoint

    restarted_editor, restarted_reviewer = Editor(), Reviewer()
    restarted = replace(
        cleaner,
        provider=restarted_editor,
        verification_provider=restarted_reviewer,
        resume_units={
            chunk.id: (
                UnitCheckpoint("u0000", first, "prose", "", body),
                UnitCheckpoint("u0001", "The second result was 318 units.", "prose", "\n\n", None),
            )
        },
    )
    resumed = (await restarted.execute([chunk]))[0]
    assert resumed.text == result.text
    assert restarted_editor.first_calls == restarted_reviewer.first_calls == 0
    assert restarted_editor.second_calls == 3


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
        from dataclasses import replace

        stubborn = False
        editor.calls = 0
        resumed = replace(cleaner, resume_units={chunk.id: result.checkpoint})
        repaired = (await resumed.execute([chunk]))[0]
        assert editor.calls == 3
        assert repaired.text == "The first result stood.\n\nThe second result stood."
        assert not review_items(repaired.text)
    else:
        assert result.text == "The first result stood.\n\nThe second result stood."
        assert not review_items(result.text)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_checkpoint", [False, True])
async def test_small_unit_profile_bounds_failed_windows_and_preserves_accepted_neighbor(
    legacy_checkpoint: bool,
) -> None:
    from dataclasses import replace

    from librarian.application.review_items import UnitCheckpoint, review_items
    from librarian.pipeline.editing import editing_units

    class Editor:
        name = "editor"
        sources: list[str] = []

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "candidate_spans" not in payload:
                self.sources.append(payload["source"])
                return payload["source"].replace("teh", "The").replace("stood", "stayed")
            span = payload["candidate_spans"][0]
            old = "stayed" if "stayed" in span["text"] else "remained"
            return json.dumps(
                {
                    "patches": [
                        {
                            "id": span["id"],
                            "find": old,
                            "replace": "remained" if old == "stayed" else "stayed",
                        }
                    ]
                }
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
                        {"id": pair["id"], "problem": "Restore stood, not stayed or remained."}
                    ]
                    if "stayed" in pair["edited"] or "remained" in pair["edited"]
                    else []
                }
            )

    source = (
        "teh first result stood.\n\nteh second result stood.\n\n"
        "teh third result stood.\n\nThe final note is sound."
    )
    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    editor = Editor()
    cleaner = CleanChunks(
        editor,
        PromptCatalog(),
        "cmos_v8",
        "test",
        verification_provider=Reviewer(),
        review_mode="strict",
        unit_target_chars=26,
        unit_max_retries=1,
        publish_partial=True,
    )
    units = editing_units(source, target_chars=26, pack_prose_blocks=True)
    assert len(units) == 4
    if legacy_checkpoint:
        merged = "".join((u.separator if i else "") + u.text for i, u in enumerate(units[:3]))
        saved = (
            UnitCheckpoint(units[0].id, merged, "prose", units[0].separator, None),
            UnitCheckpoint(units[3].id, units[3].text, "prose", units[3].separator, units[3].text),
        )
        cleaner = replace(cleaner, resume_units={chunk.id: saved})
    result = (await cleaner.execute([chunk]))[0]
    flagged = review_items(result.text)
    assert flagged
    for item in flagged:
        size = item["source_chars"]
        assert isinstance(size, int) and size <= 52
    assert "The final note is sound." in result.text
    assert editor.sources.count("The final note is sound.") == (0 if legacy_checkpoint else 1)
    assert all(len(s) <= 52 for s in editor.sources)


@pytest.mark.asyncio
async def test_numeric_checkpoint_violation_is_rechecked_and_explicitly_audited() -> None:
    from librarian.application.clean_chunks import CleaningAttempt
    from librarian.application.review_items import UnitCheckpoint

    events: list[CleaningAttempt] = []

    async def record(attempt: CleaningAttempt) -> None:
        events.append(attempt)

    class Provider:
        name = "test"
        drafts = 0

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "pairs" in payload:
                return json.dumps({"issues": []})
            self.drafts += 1
            return payload["source"]

    source = "The shipment had 318 units."
    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    provider = Provider()
    cleaner = CleanChunks(
        provider,
        PromptCatalog(),
        "cmos_v8",
        "test",
        review_mode="strict",
        on_attempt=record,
        resume_units={
            chunk.id: (UnitCheckpoint("u0000", source, "prose", "", "The shipment had 319 units."),)
        },
    )
    result = (await cleaner.execute([chunk]))[0]
    assert result.text == source
    assert provider.drafts == 1
    deferred = next(e for e in events if e.status == "context-approval-deferred")
    assert deferred.text is not None
    item = json.loads(deferred.text)["units"][0]
    assert item["id"] == "u0000"
    assert item["reason"].startswith("checkpoint-validation-requires-review:")
