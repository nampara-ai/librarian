"""The optional final pass only publishes source-backed, local repairs."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from librarian.application import final_refinement
from librarian.application.final_refinement import (
    FinalRefiner,
    _adds_ungrounded_heading_number,  # pyright: ignore[reportPrivateUsage]
    _checked_page_replacement,  # pyright: ignore[reportPrivateUsage]
    _strip_margin_number_suffixes,  # pyright: ignore[reportPrivateUsage]
)


class ScriptedProvider:
    name = "scripted"

    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        del system_prompt, model, max_tokens, temperature
        self.calls.append(user_prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def describe_image(
        self,
        *,
        image_base64: str,
        media_type: str,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        raise NotImplementedError


def make_refiner(provider: ScriptedProvider) -> FinalRefiner:
    return FinalRefiner(provider, "test-model", 4000, 20_000)


def flagged_page(number: int = 2) -> dict[str, object]:
    return {"pages": [{"page_number": number, "warnings": ["missing-numbers"]}]}


def native_page_loader(
    texts: dict[int, str],
) -> Callable[[Path | None, list[int]], dict[int, str]]:
    def load(path: Path | None, numbers: list[int]) -> dict[int, str]:
        del path
        return {number: texts[number] for number in numbers if number in texts}

    return load


def test_pdf_margin_numerals_are_not_heading_text() -> None:
    words = [
        ("Window", 204.0, 234.0, 380.5),
        ("Control", 237.0, 256.0, 380.5),
        ("Behavior", 259.0, 297.0, 380.5),
        ("6", 545.0, 552.0, 380.5),
        ("Required", 204.0, 260.0, 420.0),
        ("6", 272.0, 279.0, 420.0),
    ]
    native = "Window Control Behavior 6\nRequired 6"
    cleaned = _strip_margin_number_suffixes(native, words, 612.0)

    assert cleaned == "Window Control Behavior\nRequired 6"
    assert _adds_ungrounded_heading_number(
        "Previous paragraph.",
        "Previous paragraph.\n\n#### Window Control Behavior 6",
        cleaned,
    )
    assert not _adds_ungrounded_heading_number(
        "Previous paragraph.",
        "Previous paragraph.\n\n#### Window Control Behavior",
        cleaned,
    )
    original = (
        "#### Window Control Behavior 6\n\nKeep the controls visible while the window stays open."
    )
    replacement = _checked_page_replacement(
        original,
        original,
        cleaned + "\nKeep the controls visible while the window stays open.",
        ("missing-numbers",),
    )
    assert replacement is not None
    assert "#### Window Control Behavior\n" in replacement
    assert "Behavior 6" not in replacement


@pytest.mark.asyncio
async def test_repairs_one_flagged_page_from_pdf_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = "# Guide\n\nIntroduction.\n\n<!-- page-break -->\n\n"
    page = (
        "Choose the blue shape and keep the original label. "
        "Make sure the window stays in front of the other windows. "
        "The size should be consistent with the surrounding controls."
    )
    extra = " The required clearance is 12 pixels."
    source = first + page
    monkeypatch.setattr(
        final_refinement,
        "_read_native_pdf_pages",
        native_page_loader({2: page + extra}),
    )
    provider = ScriptedProvider([json.dumps({"replacement": page + extra})])

    result = await make_refiner(provider).execute(
        source=source,
        output=source,
        extraction_report=flagged_page(),
        source_path=Path("guide.pdf"),
    )

    assert len(provider.calls) == 1
    assert result.text.endswith(page + extra)
    assert result.report["applied"] == 1
    assert result.report["rejected"] == 0


@pytest.mark.asyncio
async def test_repairs_one_truncated_figure_caption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = "Figure 2-3 Dialog boxes with display rectangles of two sizes"
    source = (
        "# Contents\n"
        f"- {entry} 10\n\n"
        "<!-- page-break -->\n\n"
        "**Figure 2-3** Dialog boxes with display rectangles"
    )
    monkeypatch.setattr(
        final_refinement,
        "_read_native_pdf_pages",
        native_page_loader({2: entry}),
    )
    provider = ScriptedProvider(
        [json.dumps({"replacement": f"**Figure 2-3** {entry.removeprefix('Figure 2-3 ')}"})]
    )

    result = await make_refiner(provider).execute(
        source=source,
        output=source,
        extraction_report=None,
        source_path=Path("guide.pdf"),
    )

    assert len(provider.calls) == 1
    assert result.report["applied"] == 1
    assert result.text.endswith("**Figure 2-3** Dialog boxes with display rectangles of two sizes")

    fenced = ScriptedProvider(
        [
            "```json\n"
            + json.dumps({"replacement": f"**Figure 2-3** {entry.removeprefix('Figure 2-3 ')}"})
            + "\n```"
        ]
    )
    fenced_result = await make_refiner(fenced).execute(
        source=source,
        output=source,
        extraction_report=None,
        source_path=Path("guide.pdf"),
    )
    assert fenced_result.report["applied"] == 1


@pytest.mark.asyncio
async def test_does_not_rewrite_caption_when_pdf_disagrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        "# Contents\n"
        "- Figure 7-6 A confirmation alert box with appropriately named buttons 8\n\n"
        "<!-- page-break -->\n\n"
        "**Figure 7-6** A confirmation alert box with appropriately named button"
    )
    monkeypatch.setattr(
        final_refinement,
        "_read_native_pdf_pages",
        native_page_loader(
            {2: "Figure 7-6 A confirmation alert box with appropriately named button"}
        ),
    )
    provider = ScriptedProvider([])

    result = await make_refiner(provider).execute(
        source=source,
        output=source,
        extraction_report=None,
        source_path=Path("guide.pdf"),
    )

    assert result.text == source
    assert result.report["deferred"] == 1
    assert result.actions[0].reason == "not-in-source-page"
    assert not provider.calls


@pytest.mark.asyncio
async def test_rejects_unsafe_page_rewrite_and_stops_after_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = "# Guide\n\nKeep this figure and all explanatory text. ![](image_p1_0.png)"
    source = page + "\n\n<!-- page-break -->\n\n" + page
    monkeypatch.setattr(
        final_refinement,
        "_read_native_pdf_pages",
        native_page_loader({1: page, 2: page}),
    )
    provider = ScriptedProvider([RuntimeError("rate limited")])
    result = await make_refiner(provider).execute(
        source=source,
        output=source,
        extraction_report={
            "pages": [
                {"page_number": 1, "warnings": ["missing-numbers"]},
                {"page_number": 2, "warnings": ["missing-numbers"]},
            ]
        },
        source_path=Path("guide.pdf"),
    )
    assert result.text == source
    assert len(provider.calls) == 1
    assert [action.status for action in result.actions] == ["rejected", "deferred"]
    assert result.actions[1].reason == "provider-unavailable"

    unsafe = ScriptedProvider([json.dumps({"replacement": "All figures have been removed."})])
    result = await make_refiner(unsafe).execute(
        source=page,
        output=page,
        extraction_report=flagged_page(1),
        source_path=Path("guide.pdf"),
    )
    assert result.text == page
    assert result.actions[0].reason == "fidelity-check"
