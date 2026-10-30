import json

import pytest

from librarian.application.clean_chunks import CleanChunks, FidelityValidationError
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk
from librarian.pipeline.editing import EditUnit, decode_edits, editing_units
from librarian.prompts import PromptCatalog


def test_edit_units_preserve_words_and_atomic_code() -> None:
    prose = " ".join("Presupposes amazing things happen" for _ in range(30))
    code = "```python\nif ready:\n    run()\n```"
    units = editing_units(prose + "\n\n" + code, target_chars=200)
    assert len(units) > 2
    assert [unit.id for unit in units] == [f"u{i:04d}" for i in range(len(units))]
    assert " ".join(unit.text for unit in units if unit.kind == "prose") == prose
    assert [unit.text for unit in units if unit.kind == "code"] == [code]


@pytest.mark.parametrize("mode", ["missing", "duplicate", "reordered", "unknown", "empty"])
def test_edit_decoder_rejects_incomplete_or_reassigned_source_units(mode: str) -> None:
    units = editing_units("First source.\n\nSecond source.")
    edits = [{"id": unit.id, "text": unit.text} for unit in units]
    if mode == "missing":
        edits.pop()
    elif mode == "duplicate":
        edits[1]["id"] = edits[0]["id"]
    elif mode == "reordered":
        edits.reverse()
    elif mode == "unknown":
        edits[0]["id"] = "unknown"
    else:
        edits[0]["text"] = ""
    _, warnings, _ = decode_edits(json.dumps({"edits": edits}), units)
    assert "invalid-edit-structure" in warnings or "empty-output" in warnings


def test_edit_decoder_detects_numbers_moved_between_source_units() -> None:
    units = editing_units("Alice received 10.\n\nBob received 20.")
    raw = json.dumps(
        {
            "edits": [
                {"id": units[0].id, "text": "Alice received 20."},
                {"id": units[1].id, "text": "Bob received 10."},
            ]
        }
    )
    _, warnings, failures = decode_edits(raw, units)
    assert "missing-verbatim-number" in warnings
    assert set(failures) == {unit.id for unit in units}


def test_edit_decoder_keeps_code_verbatim() -> None:
    units = editing_units("```python\nif ready:\n    run()\n```")
    _, warnings, _ = decode_edits(
        json.dumps(
            {
                "edits": [
                    {"id": units[0].id, "text": "```python\nif ready:\nrun()\n```"},
                ]
            }
        ),
        units,
    )
    assert "changed-code-block" in warnings


def test_edit_decoder_rejects_untouched_unpunctuated_transcript() -> None:
    source = " ".join(["uh i think we should explain this clearly"] * 20)
    units = editing_units(source)
    raw = json.dumps({"edits": [{"id": unit.id, "text": unit.text} for unit in units]})
    _, warnings, failures = decode_edits(raw, units)
    assert "unchanged-unclean-prose" in warnings
    assert failures


@pytest.mark.asyncio
async def test_rejected_large_units_retry_in_smaller_source_spans() -> None:
    class Provider:
        name = "size-sensitive"

        def __init__(self) -> None:
            self.sizes: list[int] = []

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            unit = json.loads(user_prompt)["units"][0]
            self.sizes.append(len(unit["text"]))
            text = "Incomplete output." if len(unit["text"]) > 2700 else unit["text"]
            return json.dumps({"edits": [{"id": unit["id"], "text": text}]})

    source = " ".join(
        f"Observation {index} retains its individual measurement and attribution."
        for index in range(100)
    )
    chunk = Chunk(
        id=ChunkId("adaptive"),
        document_id=DocumentId("doc"),
        ordinal=0,
        text=source,
        start_char=0,
        end_char=len(source),
        sha256="adaptive-source",
    )
    provider = Provider()
    cleaner = CleanChunks(provider, PromptCatalog(), "cmos_v6", "test", verify_semantics=False)
    results = await cleaner.execute([chunk])
    assert results[0].text.split() == source.split()
    assert max(provider.sizes) > 2700
    assert min(provider.sizes) < 2000
    assert not results[0].fidelity_failed


def test_reconstructed_sentence_seams_keep_readable_case_and_spacing() -> None:
    units = [
        EditUnit("u0000", "First complete thought.", "prose", "\n\n"),
        EditUnit("u0001", "and the follow-up matters.", "prose", " "),
    ]
    raw = json.dumps(
        {
            "edits": [
                {"id": "u0000", "text": "First complete thought."},
                {"id": "u0001", "text": "and the follow‑up\u202fmatters."},
            ]
        }
    )
    text, warnings, _ = decode_edits(raw, units)
    assert text == "First complete thought.\n\nAnd the follow-up matters."
    assert not warnings


@pytest.mark.asyncio
async def test_source_aligned_cleaner_repairs_a_semantically_reversed_claim() -> None:
    class EditingProvider:
        name = "editing-test"

        def __init__(self) -> None:
            self.edits = 0
            self.reviews = 0

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            data = json.loads(user_prompt)
            if "pairs" in data or "pair" in data:
                self.reviews += 1
                pair = data["pair"] if "pair" in data else data["pairs"][0]
                issues = (
                    []
                    if "not improve" in pair["edited"]
                    else [
                        {
                            "id": pair["id"],
                            "problem": "The original negation was reversed.",
                        }
                    ]
                )
                return json.dumps({"issues": issues})
            self.edits += 1
            unit = data["units"][0]
            text = (
                "The treatment did improve survival in the trial."
                if self.edits == 1
                else ("The treatment did not improve survival in the trial.")
            )
            return json.dumps({"edits": [{"id": unit["id"], "text": text}]})

    source = "The treatment did not improve survival in trial."
    chunk = Chunk(ChunkId("claim"), DocumentId("claim-doc"), 0, source, 0, len(source), "a" * 64)
    provider = EditingProvider()
    cleaner = CleanChunks(provider, PromptCatalog(), "cmos_v6", "test")  # type: ignore[arg-type]
    output = (await cleaner.execute([chunk]))[0]
    assert output.text == "The treatment did not improve survival in the trial."
    assert output.warnings == ()
    assert provider.edits == 2
    assert provider.reviews == 3


@pytest.mark.asyncio
async def test_source_aligned_cleaner_refuses_unparseable_model_output() -> None:
    class InvalidProvider:
        name = "invalid-test"

        async def complete(self, **_: object) -> str:
            return "Here is a summary instead of the required edits."

    source = "A substantive source sentence."
    chunk = Chunk(
        ChunkId("invalid"), DocumentId("invalid-doc"), 0, source, 0, len(source), "b" * 64
    )
    cleaner = CleanChunks(
        InvalidProvider(),
        PromptCatalog(),
        "cmos_v6",
        "test",  # type: ignore[arg-type]
        max_fidelity_retries=0,
    )
    with pytest.raises(FidelityValidationError, match="invalid-edit-structure"):
        await cleaner.execute([chunk])
