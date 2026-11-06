import json
from dataclasses import replace

import pytest

from librarian.application.clean_chunks import CleanChunks, CleanedChunk
from librarian.application.review_items import UnitCheckpoint, load_checkpoint
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk
from librarian.pipeline.editorial_risks import (
    EDITORIAL_AUDIT,
    EDITORIAL_AUDIT_MARKER,
    editorial_checks,
)
from librarian.prompts import PromptCatalog


@pytest.mark.parametrize(
    "source",
    [
        "“Ready or not, here we come.”",
        "The example is `x or not y`.",
        "Use ``a ` b or rather c`` literally.",
        "```python\nready or not_ready\n```",
    ],
)
def test_literal_code_and_unchanged_quotations_are_not_speech_repairs(source: str) -> None:
    assert editorial_checks(source, source) == []


@pytest.mark.asyncio
async def test_genuine_alternative_is_explicitly_rejected_without_rewriting() -> None:
    class Provider:
        name = "test"
        drafts = 0
        reviews = 0

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "pairs" in payload:
                self.reviews += 1
                return json.dumps({"issues": []})
            if "checks" in payload:
                self.reviews += 1
                return json.dumps(
                    {
                        "checks": [
                            {
                                "id": c["id"],
                                "confirmed": False,
                                "reason": "This is a deliberate alternative.",
                            }
                            for c in payload["checks"]
                        ]
                    }
                )
            self.drafts += 1
            return payload["source"]

    source = "Ready or not, here we come."
    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    provider = Provider()
    cleaner = CleanChunks(provider, PromptCatalog(), "cmos_v8", "test", review_mode="strict")
    result = (await cleaner.execute([chunk]))[0]
    assert result.text == source
    assert provider.drafts == 1 and provider.reviews == 2
    assert EDITORIAL_AUDIT_MARKER in result.warnings
    assert not cleaner.revalidate(result).fidelity_failed
    before = (provider.drafts, provider.reviews)
    await cleaner.execute([chunk], cached_chunks=[result])
    assert (provider.drafts, provider.reviews) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_focused_check_catches_a_missed_repair_and_migrates_only_its_approval(
    legacy: bool,
) -> None:
    candidate = "The meeting is for Alex—or not for Alex; for the committee."
    source = "the meeting is for Alex or not for Alex it is for the committee"

    class Editor:
        name = "editor"
        drafts = 0
        patches = 0

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "candidate_spans" in payload:
                self.patches += 1
                span = payload["candidate_spans"][0]
                return json.dumps(
                    {
                        "patches": [
                            {
                                "id": span["id"],
                                "find": "for Alex—or not for Alex; for the committee",
                                "replace": "for the committee",
                            }
                        ]
                    }
                )
            self.drafts += 1
            return candidate

    class Reviewer:
        name = "reviewer"

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "checks" in payload:
                return json.dumps(
                    {
                        "checks": [
                            {
                                "id": c["id"],
                                "confirmed": True,
                                "reason": "The final recipient is the committee.",
                            }
                            for c in payload["checks"]
                        ]
                    }
                )
            # The general reviewer misses this defect; the focused decision must cover it.
            return json.dumps({"issues": []})

    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    snapshots: list[tuple[UnitCheckpoint, ...]] = []

    async def persist(_chunk: Chunk, units: tuple[UnitCheckpoint, ...]) -> None:
        snapshots.append(units)

    editor = Editor()
    cleaner = CleanChunks(
        editor,
        PromptCatalog(),
        "cmos_v8",
        "test",
        review_mode="strict",
        verification_provider=Reviewer(),
        on_unit_checkpoint=persist,
    )
    if legacy:
        old = CleanedChunk(chunk, candidate, ())
        assert cleaner.revalidate(old).fidelity_failed
        cleaner = replace(
            cleaner,
            resume_units={chunk.id: (UnitCheckpoint("u0000", source, "prose", "\n\n", candidate),)},
        )
    result = (await cleaner.execute([chunk]))[0]
    assert result.text == "The meeting is for the committee."
    assert editor.drafts == (0 if legacy else 1)
    assert editor.patches == 1
    assert EDITORIAL_AUDIT_MARKER in result.warnings
    assert not cleaner.revalidate(result).fidelity_failed
    restored = load_checkpoint(json.dumps([u.as_dict() for u in snapshots[-1]]))
    assert restored[0].editorial_audit == EDITORIAL_AUDIT


@pytest.mark.asyncio
async def test_broken_aside_is_repaired_without_discarding_the_direct_address() -> None:
    candidate = "I notice it in public, or I am sure you have noticed it too, that people hesitate."
    source = "I notice in public or I am sure you have noticed too that people hesitate"
    repaired = "I notice in public—as I am sure you have noticed too—that people hesitate."

    class Editor:
        name = "editor"

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "candidate_spans" in payload:
                span = payload["candidate_spans"][0]
                return json.dumps(
                    {
                        "patches": [
                            {
                                "id": span["id"],
                                "find": candidate,
                                "replace": repaired,
                            }
                        ]
                    }
                )
            return candidate

    class Reviewer:
        name = "reviewer"

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "checks" in payload:
                return json.dumps(
                    {
                        "checks": [
                            {
                                "id": c["id"],
                                "confirmed": True,
                                "reason": "The aside uses a broken alternative join.",
                            }
                            for c in payload["checks"]
                        ]
                    }
                )
            return json.dumps({"issues": []})

    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    cleaner = CleanChunks(
        Editor(),
        PromptCatalog(),
        "cmos_v8",
        "test",
        verification_provider=Reviewer(),
        review_mode="strict",
    )
    result = (await cleaner.execute([chunk]))[0]
    assert (
        result.text == "I notice in public—as I am sure you have noticed too—that people hesitate."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_late_cues_are_reviewed_in_small_batches_before_whole_unit_approval(
    legacy: bool,
) -> None:
    source = (
        " ".join(
            f"I mean what I say about {topic}, including their historical context, "
            "their intended audience, and the records used to explain each finding in our archive."
            for topic in ("maps", "rivers", "mountains", "forests")
        )
        + " The parcel is for Nora—or not for Nora; it is for the library."
    )
    fixed = source.replace("for Nora—or not for Nora; it is for the library", "for the library")

    class Provider:
        name = "test"
        drafts = 0
        sizes: list[int] = []
        late_seen = False

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "pairs" in payload:
                return json.dumps({"issues": []})
            if "checks" in payload and "pair" in payload:
                self.sizes.append(len(payload["checks"]))
                checks = []
                for check in payload["checks"]:
                    repair = "or not for Nora" in check["problem"]
                    self.late_seen |= repair
                    checks.append(
                        {
                            "id": check["id"],
                            "confirmed": repair,
                            "reason": "Repair the abandoned referent."
                            if repair
                            else "A deliberate statement about sincerity.",
                        }
                    )
                return json.dumps({"checks": checks})
            if "candidate_spans" in payload:
                span = next(s for s in payload["candidate_spans"] if "for Nora" in s["text"])
                return json.dumps(
                    {
                        "patches": [
                            {
                                "id": span["id"],
                                "find": "for Nora—or not for Nora; it is for the library",
                                "replace": "for the library",
                            }
                        ]
                    }
                )
            self.drafts += 1
            return source

    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    provider = Provider()
    cleaner = CleanChunks(provider, PromptCatalog(), "cmos_v8", "test", review_mode="strict")
    if legacy:
        cleaner = replace(
            cleaner,
            resume_units={
                chunk.id: (
                    UnitCheckpoint(
                        "u0000", source, "prose", "", source, editorial_audit="editorial-risks-v1"
                    ),
                )
            },
        )
    result = (await cleaner.execute([chunk]))[0]
    assert result.text == fixed
    assert provider.late_seen
    assert provider.sizes[:2] == [4, 1]
    assert max(provider.sizes) <= 4
    assert provider.drafts == (0 if legacy else 1)
    assert EDITORIAL_AUDIT_MARKER in result.warnings


@pytest.mark.asyncio
async def test_excessive_review_work_is_explicit_instead_of_silently_approved() -> None:
    from librarian.application.review_items import review_items

    source = " ".join(f"I mean what I say about sector {i}." for i in range(40))

    class Provider:
        name = "test"
        confirmations = 0

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "pairs" in payload:
                return json.dumps({"issues": []})
            if "checks" in payload and "pair" in payload:
                self.confirmations += 1
                raise AssertionError("Excessive confirmation work must be bounded")
            return payload["source"]

    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    provider = Provider()
    cleaner = CleanChunks(
        provider,
        PromptCatalog(),
        "cmos_v8",
        "test",
        review_mode="strict",
        publish_partial=True,
    )
    result = (await cleaner.execute([chunk]))[0]
    assert provider.confirmations == 0
    items = review_items(result.text)
    assert items[0]["source"] == source
    assert items[0]["reasons"] == ["editorial-review-capacity-exceeded"]
    assert EDITORIAL_AUDIT_MARKER not in result.warnings
