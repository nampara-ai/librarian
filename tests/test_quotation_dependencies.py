import json

import pytest

from librarian.application.clean_chunks import CleanChunks, CleaningAttempt
from librarian.application.review_items import UnitCheckpoint
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk
from librarian.pipeline.editing import EditUnit
from librarian.prompts import PromptCatalog


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_checkpoint", [False, True])
async def test_quoted_continuation_waits_for_its_preceding_boundary(
    legacy_checkpoint: bool,
) -> None:
    left = "The professor said all citizens have"
    right = "equal standing and nobody is exempt."
    units = (EditUnit("a", left, "prose", ""), EditUnit("b", right, "prose", " "))

    class FixedCleaner(CleanChunks):
        def _source_units(self, text: str) -> tuple[EditUnit, ...]:
            return units

    class Editor:
        name = "editor"
        drafts: list[str] = []
        patches: list[str] = []

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "candidate_spans" not in payload:
                self.drafts.append(payload["source"])
                return (
                    "The professor said, “all citizens must have"
                    if payload["source"] == left
                    else "“equal standing, and nobody is exempt.”"
                )
            span = payload["candidate_spans"][0]
            if "must" in span["text"]:
                find, replacement = "citizens must have", "citizens have"
            else:
                find, replacement = "“equal", "equal"
            self.patches.append(find)
            return json.dumps(
                {"patches": [{"id": span["id"], "find": find, "replace": replacement}]}
            )

    class Reviewer:
        name = "reviewer"
        continuation_reviews = 0

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
            problem = None
            if pair["id"] == "a" and "must" in pair["edited"]:
                problem = "Remove the added obligation must."
            if pair["id"] == "b":
                self.continuation_reviews += 1
                # The first review misses a boundary that a later changed context exposes.
                if self.continuation_reviews > 1 and pair["edited"].startswith("“"):
                    problem = "Remove the duplicated opening quotation at this continuation."
            return json.dumps(
                {"issues": [{"id": pair["id"], "problem": problem}] if problem else []}
            )

    source = left + " " + right
    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    events: list[CleaningAttempt] = []

    async def observe(attempt: CleaningAttempt) -> None:
        events.append(attempt)

    checkpoint = (
        UnitCheckpoint("a", left, "prose", "", None),
        UnitCheckpoint("b", right, "prose", " ", "“equal standing, and nobody is exempt.”"),
    )
    editor, reviewer = Editor(), Reviewer()
    cleaner = FixedCleaner(
        editor,
        PromptCatalog(),
        "cmos_v8",
        "test",
        verification_provider=reviewer,
        review_mode="strict",
        on_attempt=observe,
        resume_units={chunk.id: checkpoint} if legacy_checkpoint else {},
    )
    result = (await cleaner.execute([chunk]))[0]
    assert (
        result.text
        == "The professor said, “all citizens have equal standing, and nobody is exempt.”"
    )
    assert editor.patches == ["citizens must have", "“equal"]
    accepted = [
        u["id"]
        for event in events
        if event.status == "units-accepted"
        for u in json.loads(event.text or "{}")["units"]
    ]
    assert accepted == ["a", "b"]
    assert reviewer.continuation_reviews == 3
    assert (right in editor.drafts) is not legacy_checkpoint
    deferred = [event for event in events if event.status == "context-approval-deferred"]
    assert bool(deferred) is legacy_checkpoint
