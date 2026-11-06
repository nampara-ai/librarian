"""Deterministic content-loss guards: filler-aware retention and elision detection."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Literal, cast

import pytest

from librarian.application.clean_chunks import CleanChunks, CleanedChunk
from librarian.application.review_items import review_body, review_items
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk
from librarian.pipeline.validation import (
    DETERMINISTIC_WARNINGS,
    content_retention,
    ellipsis_count,
    validate_cleaned_text,
)
from librarian.prompts import PromptCatalog

SOURCE_40 = (
    "I own a list that I have built over many years of writing to people every single "
    "morning, and I am able to build some scale with it because those people trust me to "
    "deliver something useful."
)
ABBREVIATED = "I own a list that I... I am able to build some scale."


def test_abbreviated_unit_fails_both_deterministic_guards() -> None:
    result = validate_cleaned_text(ABBREVIATED, input_size=len(SOURCE_40), source_text=SOURCE_40)

    assert "substantial-content-loss" in result.warnings
    assert "model-elided-text" in result.warnings
    assert {"substantial-content-loss", "model-elided-text"} <= DETERMINISTIC_WARNINGS


def test_content_loss_applies_below_the_old_two_hundred_word_floor() -> None:
    sentences = [
        f"Sentence number {n} explains a distinct point about building an audience."
        for n in range(10)
    ]
    source = " ".join(sentences)  # about 100 words
    output = " ".join(sentences[:6])

    result = validate_cleaned_text(output, input_size=len(source), source_text=source)

    assert "substantial-content-loss" in result.warnings
    assert "model-elided-text" not in result.warnings


def test_filler_removal_is_not_content_loss() -> None:
    body = " ".join(
        f"The archive entry {letter} remains unchanged." for letter in "abcdefghijklmnop"
    )
    source = "um " * 60 + "you know, like, " * 10 + body

    assert content_retention(source, body) == 1.0
    result = validate_cleaned_text(body, input_size=len(source), source_text=source)
    assert "substantial-content-loss" not in result.warnings


def test_spelling_inflection_and_word_substitution_are_repairs() -> None:
    source = (
        "Saddle fit and canter transitions matter for the horse because the colour of the "
        "saddle pad never influences how the animal utilizes its back muscles during work."
    )
    candidate = (
        "Saddle fit and canter transition matter for the horse because the color of the "
        "saddle pad never influences how the animal uses its back muscles during work."
    )

    retention = content_retention(source, candidate)
    assert retention is not None and retention >= 0.9
    assert (
        validate_cleaned_text(candidate, input_size=len(source), source_text=source).warnings == ()
    )


def test_padding_cannot_buy_back_dropped_sentences() -> None:
    source = " ".join(
        f"Important sentence {n} carries unique meaning about topic{n} and its consequences."
        for n in range(12)
    )
    padded = (
        " ".join(
            f"Important sentence {n} carries unique meaning about topic{n} and its consequences."
            for n in range(6)
        )
        + " The author continues with additional reflective commentary regarding broader themes "
        * 6
    )

    retention = content_retention(source, padded)
    assert retention is not None and retention < 0.9


def test_ellipsis_normalization_is_not_elision() -> None:
    source = "Well... I suppose we could try. Or . . . not."
    candidate = "Well… I suppose we could try. Or… not."

    assert ellipsis_count(source) == ellipsis_count(candidate) == 2
    assert (
        "model-elided-text"
        not in validate_cleaned_text(candidate, input_size=len(source), source_text=source).warnings
    )


def test_retention_is_unmeasured_for_very_short_units() -> None:
    assert content_retention("Chapter 3", "") is None
    assert content_retention("teh first result stood.", "The first result stayed.") is None


# --- cleaning loop -----------------------------------------------------------


class _AbbreviatingEditor:
    """Returns the source abbreviated with an ellipsis, like a lazy small model."""

    name = "editor"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, user_prompt: str, **_: object) -> str:
        self.calls += 1
        source = json.loads(user_prompt)["source"]
        if "list" in source:
            return ABBREVIATED
        return source.replace("teh", "The")


class _ShruggingReviewer:
    """Approves everything: a weak reviewer must not be able to clear content loss."""

    name = "reviewer"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *, user_prompt: str, **_: object) -> str:
        self.calls += 1
        payload = json.loads(user_prompt)
        if "checks" in payload:
            return json.dumps(
                {
                    "checks": [
                        {"id": c["id"], "confirmed": False, "reason": ""} for c in payload["checks"]
                    ]
                }
            )
        return json.dumps({"issues": []})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["advisory", "strict"])
async def test_abbreviated_unit_is_published_as_complete_source_for_review(
    mode: Literal["advisory", "strict"],
) -> None:
    source = SOURCE_40 + "\n\nteh closing line stands."
    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    editor, reviewer = _AbbreviatingEditor(), _ShruggingReviewer()
    cleaner = CleanChunks(
        editor,
        PromptCatalog(),
        "cmos_v8",
        "test",
        review_mode=mode,
        verification_provider=reviewer,
        unit_target_chars=len(SOURCE_40),
        unit_max_retries=1,
        publish_partial=True,
    )

    result = (await cleaner.execute([chunk]))[0]

    flagged = review_items(result.text)
    assert len(flagged) == 1
    item = flagged[0]
    assert item["kind"] == "source"
    assert item["source"] == SOURCE_40  # the complete source, never the gutted candidate
    reasons = cast(list[str], item["reasons"])
    assert "substantial-content-loss" in reasons
    assert "model-elided-text" in reasons
    assert ABBREVIATED not in result.text
    assert "The closing line stands." in result.text
    assert "unit-needs-review" in result.warnings
    # What a reader receives retains every content word of the source.
    assert content_retention(source, review_body(result.text)) == 1.0


@pytest.mark.asyncio
async def test_reviewer_approval_never_accepts_an_abbreviated_unit() -> None:
    source = SOURCE_40
    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, source, 0, len(source), "sha")
    reviewer = _ShruggingReviewer()
    cleaner = CleanChunks(
        _AbbreviatingEditor(),
        PromptCatalog(),
        "cmos_v8",
        "test",
        review_mode="strict",
        verification_provider=reviewer,
        unit_target_chars=len(source),
        unit_max_retries=1,
        publish_partial=True,
    )

    result = (await cleaner.execute([chunk]))[0]

    assert reviewer.calls == 0  # a unit failing deterministic guards is never sent to review
    assert review_items(result.text)[0]["source"] == source
    assert not result.checkpoint[0].accepted_text


def test_cached_abbreviated_output_is_not_served_as_clean() -> None:
    chunk = Chunk(ChunkId("c"), DocumentId("d"), 0, SOURCE_40, 0, len(SOURCE_40), "sha")
    cleaner = CleanChunks(
        _AbbreviatingEditor(),
        PromptCatalog(),
        "cmos_v8",
        "test",
        review_mode="advisory",
        verification_provider=_ShruggingReviewer(),
    )
    cached = CleanedChunk(chunk=chunk, text=ABBREVIATED, warnings=())

    revalidated = replace(cleaner, document_source=SOURCE_40).revalidate(cached)

    assert revalidated.fidelity_failed
    assert revalidated.text == SOURCE_40
    assert "substantial-content-loss" in revalidated.warnings
    assert "source-preserved-after-fidelity-check" in revalidated.warnings


# --- page-break restoration ---------------------------------------------------

from librarian.pipeline.validation import PAGE_BREAK_REGEX, restore_page_breaks  # noqa: E402

PB = "\n\n<!-- page-break -->\n\n"
PAGES = [
    "People don't want to read ads in their inbox.\nThat's just the way it is.",
    "Types of Emails\nPeople get two kinds of emails. Some are personal, and some are ads.",
    "For instance, if you sell courses, and you don't have millions of people on your list,\n"
    "then you have to play a bit of a different game than just sending out blatant ads.",
    "First Pillar:\nThree Types of Email Campaigns\nEvery one of these campaigns has an objective.",
]
SOURCE = PB.join(PAGES)


def _cleaned(page: str) -> str:
    return page.replace("\n", " ").replace("don't", "do not")


def test_dropped_page_breaks_are_restored_from_word_anchors() -> None:
    candidate = (
        _cleaned(PAGES[0])
        + PB
        + "## "
        + _cleaned(PAGES[1])
        + "\n\n"  # break dropped here
        + _cleaned(PAGES[2])
        + " "
        + _cleaned(PAGES[3])  # and here
    )

    text, restored = restore_page_breaks(SOURCE, candidate)

    assert restored
    blocks = PAGE_BREAK_REGEX.split(text)
    assert len(blocks) == 4
    assert blocks[2].strip().startswith("For instance, if you sell courses")
    assert blocks[3].strip().startswith("First Pillar")
    assert blocks[1].strip().startswith("## Types of Emails")


def test_restored_pages_carry_no_stray_blank_lines() -> None:
    # Models often leave a two-space line break and blank lines around a marker.
    candidate = (
        _cleaned(PAGES[0])
        + "  \n\n<!-- page-break -->\n\n  \n\n\n"
        + _cleaned(PAGES[1])
        + " "
        + _cleaned(PAGES[2])
        + PB
        + _cleaned(PAGES[3])
    )

    text, restored = restore_page_breaks(SOURCE, candidate)

    assert restored
    assert text == PB.join(block.strip() for block in PAGE_BREAK_REGEX.split(text))
    assert "\n\n\n" not in text and "  \n\n" not in text


def test_invented_page_break_is_removed() -> None:
    candidate = PB.lstrip() + PB.join(_cleaned(page) for page in PAGES)

    text, restored = restore_page_breaks(SOURCE, candidate)

    assert restored
    assert len(PAGE_BREAK_REGEX.findall(text)) == 3
    assert text.startswith("People do not want")


def test_unplaceable_page_break_leaves_candidate_alone() -> None:
    candidate = _cleaned(PAGES[0]) + PB + _cleaned(PAGES[3])  # two pages missing entirely

    text, restored = restore_page_breaks(SOURCE, candidate)

    assert not restored
    assert text == candidate


def test_validation_restores_breaks_instead_of_rejecting() -> None:
    candidate = (
        _cleaned(PAGES[0])
        + PB
        + _cleaned(PAGES[1])
        + " "
        + _cleaned(PAGES[2])
        + PB
        + _cleaned(PAGES[3])
    )

    result = validate_cleaned_text(candidate, input_size=len(SOURCE), source_text=SOURCE)

    assert "changed-page-breaks" not in result.warnings
    assert "page-breaks-restored" in result.warnings
    assert len(PAGE_BREAK_REGEX.findall(result.text)) == 3
    assert not DETERMINISTIC_WARNINGS.intersection(result.warnings)
