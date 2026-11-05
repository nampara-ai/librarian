import json

import pytest

from librarian.application.clean_chunks import CleanChunks, FidelityValidationError
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk
from librarian.pipeline.editing import EditUnit, decode_edits, editing_units, verification_pairs
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
    assert text == "First complete thought. And the follow-up matters."
    pairs = verification_pairs(raw, units, include_unchanged_prose=True)
    assert pairs[1]["edited"] == "And the follow-up matters."
    assert pairs[0]["edited_context_after"] == " And the follow-up matters."
    assert pairs[1]["edited_context_before"] == "First complete thought. "
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


@pytest.mark.asyncio
async def test_dependent_repairs_receive_preceding_committed_draft() -> None:
    class Provider:
        name = "dependent-repairs"

        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "pairs" in payload or "pair" in payload:
                return json.dumps(
                    {"issues": [{"id": "u0001", "problem": "Second source still needs repair."}]}
                )
            self.payloads.append(payload)
            phrase = "First" if len(self.payloads) == 1 else "Second"
            return json.dumps(
                {"patches": [{"id": "p0", "find": phrase, "replace": phrase + " repaired"}]}
            )

    units = [
        EditUnit("u0000", "First source.", "prose", ""),
        EditUnit("u0001", "Second source.", "prose", " "),
    ]
    provider = Provider()
    cleaner = CleanChunks(provider, PromptCatalog(), "cmos_v8", "test")
    edits = {unit.id: unit.text for unit in units}
    await cleaner._edit_units(  # pyright: ignore[reportPrivateUsage]
        units,
        "Raw previous.",
        "",
        edits,
        {unit.id: ["editorial-quality-failed"] for unit in units},
        {},
        patch_semantics=True,
        previous_edited_context="Edited previous.",
    )
    assert provider.payloads[0]["edited_context_before"] == "Edited previous.\n\n"
    assert provider.payloads[1]["edited_context_before"] == "First repaired source. "
    assert provider.payloads[1]["context_before"] == "First source."
    assert edits["u0001"] == "Second repaired source."


@pytest.mark.asyncio
async def test_parallel_review_does_not_label_raw_predecessor_as_an_edited_draft() -> None:
    class Provider:
        name = "parallel-context"

        def __init__(self) -> None:
            self.pairs: list[dict[str, str]] = []

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "pairs" in payload:
                self.pairs.extend(payload["pairs"])
                return '{"issues": []}'
            return payload["source"]

    sources = ["A first observation.", "A second observation."]
    chunks = [
        Chunk(ChunkId(str(i)), DocumentId("doc"), i, source, 0, len(source), str(i))
        for i, source in enumerate(sources)
    ]
    provider = Provider()
    cleaner = CleanChunks(provider, PromptCatalog(), "cmos_v8", "test", coherence_mode="fast")
    await cleaner.execute(chunks)
    pair = next(pair for pair in provider.pairs if pair["source"] == sources[1])
    assert pair["context_before"] == sources[0]
    assert pair["edited_context_before"] == ""


@pytest.mark.asyncio
async def test_obsolete_neighbor_check_is_reviewed_before_applying_a_conflicting_patch() -> None:
    class Provider:
        name = "stale-checks"
        patch_calls = 0

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "pairs" in payload:
                pair = payload["pairs"][0]
                assert pair["edited_context_before"] == "“A greeting "
                assert pair["edited"] == "and goodbye.”"
                return '{"issues": []}'
            self.patch_calls += 1
            return json.dumps({"patches": [{"id": "p0", "find": ".”", "replace": ""}]})

    units = [
        EditUnit("u0000", "a greeting", "prose", ""),
        EditUnit("u0001", "and goodbye", "prose", " "),
    ]
    provider = Provider()
    cleaner = CleanChunks(provider, PromptCatalog(), "cmos_v8", "test")
    edits = {"u0000": "“A greeting.”", "u0001": "and goodbye.”"}
    failures = {
        "u0000": ["The quotation continues into the next unit; remove its premature ending."],
        "u0001": ["The closing quotation mark lacks an opening mark in this unit."],
    }
    traces: list[str] = []
    raw = await cleaner._edit_units(  # pyright: ignore[reportPrivateUsage]
        units, "", "", edits, failures, {}, patch_semantics=True, repair_review_traces=traces
    )
    assert provider.patch_calls == 1
    assert "u0001" not in failures
    assert len(traces) == 1
    assert decode_edits(raw, units)[0] == "“A greeting and goodbye.”"


def test_adjacent_repair_windows_preserve_source_candidates_and_limits() -> None:
    from librarian.pipeline.editing import coalesce_adjacent_repairs

    units = [
        EditUnit("u0000", "First source", "prose", ""),
        EditUnit("u0001", "second source", "prose", " "),
        EditUnit("u0002", "third source", "prose", "\n\n"),
    ]
    edits = {unit.id: unit.text.title() for unit in units}
    failures = {unit.id: ["check " + unit.id] for unit in units}
    merged, candidates, checks = coalesce_adjacent_repairs(units, edits, failures, max_chars=30)
    assert len(merged) == 2
    assert merged[0].text == "First source second source"
    assert candidates["u0000"] == "First Source Second Source"
    assert checks["u0000"] == ["check u0000", "check u0001"]
    assert "u0001" not in candidates and "u0001" not in checks
    assert merged[1] == units[2]
    assert len(edits) == len(failures) == 3


@pytest.mark.asyncio
async def test_pipeline_repairs_coupled_unit_boundaries_in_one_atomic_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    units = [
        EditUnit("u0000", "she said a greeting", "prose", ""),
        EditUnit("u0001", "and goodbye", "prose", " "),
    ]

    def split(_: str, **_kwargs: object) -> list[EditUnit]:
        return units

    monkeypatch.setattr("librarian.application.clean_chunks.editing_units", split)

    class Provider:
        name = "atomic-window"
        patch_calls = 0

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            payload = json.loads(user_prompt)
            if "pairs" in payload or "pair" in payload:
                pair = payload.get("pair") or payload["pairs"][0]
                if pair["edited"] == "She said, “A greeting and goodbye.”":
                    return '{"issues": []}'
                return json.dumps(
                    {
                        "issues": [
                            {
                                "id": pair["id"],
                                "problem": "Repair the quotation boundary with its neighbor.",
                            }
                        ]
                    }
                )
            if "candidate_spans" in payload:
                self.patch_calls += 1
                assert payload["source"] == "she said a greeting and goodbye"
                return json.dumps(
                    {"patches": [{"id": "p0", "find": "greeting.” And", "replace": "greeting and"}]}
                )
            return (
                "She said, “A greeting.”" if payload["source"] == units[0].text else "and goodbye.”"
            )

    source = "she said a greeting and goodbye"
    chunk = Chunk(ChunkId("atomic"), DocumentId("doc"), 0, source, 0, len(source), "source")
    provider = Provider()
    cleaner = CleanChunks(provider, PromptCatalog(), "cmos_v8", "test", max_fidelity_retries=1)
    result = (await cleaner.execute([chunk]))[0]
    assert result.text == "She said, “A greeting and goodbye.”"
    assert provider.patch_calls == 1
