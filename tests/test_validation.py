import pytest
from pydantic import ValidationError

from librarian.application.clean_chunks import (
    CleanChunks,
    CleanedChunk,
    FidelityValidationError,
    strip_repeated_context,
)
from librarian.config import Settings
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk, RunStage
from librarian.pipeline.validation import FIDELITY_WARNINGS, validate_cleaned_text
from librarian.prompts import PromptCatalog


def test_validation_filters_artifact_lines() -> None:
    result = validate_cleaned_text(
        "Here is the cleaned transcript:\n"
        "Actual cleaned content remains.\n"
        "I have cleaned the transcript.",
        input_size=100,
    )

    assert result.text == "Actual cleaned content remains."
    assert "artifact-filtered" in result.warnings


def test_validation_reports_empty_output() -> None:
    result = validate_cleaned_text("   ", input_size=100)

    assert not result.ok
    assert result.warnings == ("empty-output",)


@pytest.mark.parametrize(
    "source",
    [
        'She said, "I cannot read the inscription."\nThe meeting ended.',
        "I cannot read the inscription.\nThe meeting ended.",
        '```python\nif ready:\n    print("I cannot read")\n\n\n\n    run()\n```',
        "- Main item\n    - Nested item\n\nA hard line break.  \nNext line.",
    ],
)
def test_validation_preserves_source_quotes_code_and_markdown_whitespace(source: str) -> None:
    result = validate_cleaned_text(source, input_size=len(source), source_text=source)
    assert result.text == source
    assert "artifact-filtered" not in result.warnings


def test_validation_warns_when_markdown_structure_is_lost() -> None:
    source = """# Visit Notes

Opening paragraph.

Second paragraph with [1].

- First item
- Second item

| A | B |
|---|---|
| 1 | 2 |
"""
    output = (
        "Visit Notes Opening paragraph. Second paragraph with citation. "
        "First item Second item A B 1 2"
    )

    result = validate_cleaned_text(output, input_size=len(source), source_text=source)

    assert "missing-markdown-heading" in result.warnings
    assert "missing-markdown-list" in result.warnings
    assert "missing-markdown-table" in result.warnings
    assert "missing-citation-marker" in result.warnings


def test_validation_warns_for_context_leaks_or_malformed_markdown() -> None:
    result = validate_cleaned_text(
        "[CONTEXT: This continues from previous chunk]\n\n|\n| Name | Value |\n| Alice | 1 |\n-",
        input_size=200,
    )

    assert "context-marker-leak" in result.warnings
    assert "malformed-markdown-table" in result.warnings
    assert "orphan-list-marker" in result.warnings


def test_validation_warns_for_collapsed_paragraphs() -> None:
    source = "\n\n".join(f"Paragraph {index} with useful source detail." for index in range(5))
    output = " ".join(f"Paragraph {index} with useful source detail." for index in range(5)) * 5

    result = validate_cleaned_text(output, input_size=len(source), source_text=source)

    assert "collapsed-paragraphs" in result.warnings


def test_validation_warns_for_repeated_tail() -> None:
    repeated_tail = " The closing sentence repeats without adding evidence." * 8
    result = validate_cleaned_text(
        f"Useful OCR text before a degenerate tail.{repeated_tail}",
        input_size=200,
    )

    assert "repeated-tail" in result.warnings


def test_validation_repeated_tail_ignores_normal_short_repetition() -> None:
    result = validate_cleaned_text(
        "Stable appendix text. Figure 1 references note A. Figure 2 references note A.",
        input_size=80,
    )

    assert "repeated-tail" not in result.warnings


def test_validation_detects_lost_images_numbers_and_substantial_content() -> None:
    source = "![](image_p1_0.png)\n\n" + " ".join(f"Detail {number}" for number in range(250))
    output = " ".join(f"Detail {number}" for number in range(100))

    result = validate_cleaned_text(output, input_size=len(source), source_text=source)

    assert "changed-markdown-images" in result.warnings
    assert "missing-verbatim-number" in result.warnings
    assert "substantial-content-loss" in result.warnings


def test_validation_detects_added_numbers() -> None:
    source = "Keep the timeout at 120 seconds."

    result = validate_cleaned_text(
        "Keep the timeout at 120 seconds and retry 7 times.",
        input_size=len(source),
        source_text=source,
    )

    assert "added-verbatim-number" in result.warnings


def test_validation_preserves_invisible_pdf_page_boundaries() -> None:
    source = "First page.\n\n<!-- page-break -->\n\nSecond page."
    result = validate_cleaned_text(
        "First page. Second page.", input_size=len(source), source_text=source
    )

    assert "changed-page-breaks" in result.warnings


def test_strip_repeated_context_handles_markdown_punctuation_changes() -> None:
    repeated = "A glossary entry with enough meaningful words to detect copied continuity context."
    context = f"Earlier text. {repeated} It ends with fifteen stable tokens for the next section."
    output = (
        f"{repeated} It ends with fifteen stable tokens for the next section!\n\n"
        "Actual new section text."
    )

    cleaned, changed = strip_repeated_context(output, context)

    assert changed is True
    assert cleaned == "Actual new section text."


@pytest.mark.asyncio
async def test_cleaner_fails_after_retries_when_model_loses_a_figure() -> None:
    source = "Figure 1\n\n![](image_p1_0.png)\n\nDetailed caption text."

    class DropsFigureProvider:
        name = "drops-figure"

        async def complete(self, **_: object) -> str:
            return "Figure 1\n\nDetailed caption text."

    chunk = Chunk(
        id=ChunkId("chunk_fidelity"),
        document_id=DocumentId("doc_fidelity"),
        ordinal=0,
        text=source,
        start_char=0,
        end_char=len(source),
        sha256="f" * 64,
    )
    cleaner = CleanChunks(
        provider=DropsFigureProvider(),  # type: ignore[arg-type]
        prompt_catalog=PromptCatalog(),
        prompt_version="cmos_v5",
        model="test",
    )

    with pytest.raises(FidelityValidationError, match="changed-markdown-images") as failure:
        await cleaner.execute([chunk])
    assert failure.value.attempts == 4
    assert failure.value.chunk.text == source


@pytest.mark.asyncio
async def test_cleaner_fails_after_retries_when_model_loses_a_number() -> None:
    source = "Keep the timeout at 120 seconds for all production requests."

    class DropsNumberProvider:
        name = "drops-number"

        async def complete(self, **_: object) -> str:
            return "Keep the timeout for all production requests."

    chunk = Chunk(
        id=ChunkId("chunk_numeric_fidelity"),
        document_id=DocumentId("doc_numeric_fidelity"),
        ordinal=0,
        text=source,
        start_char=0,
        end_char=len(source),
        sha256="a" * 64,
    )
    cleaner = CleanChunks(
        provider=DropsNumberProvider(),  # type: ignore[arg-type]
        prompt_catalog=PromptCatalog(),
        prompt_version="cmos_v5",
        model="test",
    )

    with pytest.raises(FidelityValidationError, match="missing-verbatim-number"):
        await cleaner.execute([chunk])


@pytest.mark.asyncio
async def test_cleaner_fails_after_retries_when_model_adds_a_number() -> None:
    source = "Keep the timeout at 120 seconds for all production requests."

    class AddsNumberProvider:
        name = "adds-number"

        async def complete(self, **_: object) -> str:
            return "Keep the timeout at 120 seconds and retry 7 times."

    chunk = Chunk(
        id=ChunkId("chunk_added_numeric_fidelity"),
        document_id=DocumentId("doc_added_numeric_fidelity"),
        ordinal=0,
        text=source,
        start_char=0,
        end_char=len(source),
        sha256="c" * 64,
    )
    cleaner = CleanChunks(
        provider=AddsNumberProvider(),  # type: ignore[arg-type]
        prompt_catalog=PromptCatalog(),
        prompt_version="cmos_v5",
        model="test",
    )

    with pytest.raises(FidelityValidationError, match="added-verbatim-number"):
        await cleaner.execute([chunk])


@pytest.mark.asyncio
async def test_cleaner_repairs_failed_output_from_original_with_numeric_diagnostics() -> None:
    source = "The timeout is 120 seconds and the retry limit is 3."

    class RepairingProvider:
        name = "repairing"

        def __init__(self) -> None:
            self.prompts: list[tuple[str, str]] = []

        async def complete(self, *, system_prompt: str, user_prompt: str, **_: object) -> str:
            self.prompts.append((system_prompt, user_prompt))
            return "The timeout is 120 seconds." if len(self.prompts) == 1 else source

    provider = RepairingProvider()
    chunk = Chunk(ChunkId("retry"), DocumentId("retry-doc"), 0, source, 0, len(source), "a" * 64)
    cleaner = CleanChunks(provider, PromptCatalog(), "cmos_v5", "test")  # type: ignore[arg-type]
    completed: list[CleanedChunk] = []

    async def note(result: CleanedChunk) -> None:
        completed.append(result)

    result = (await cleaner.execute([chunk], on_chunk_cleaned=note))[0]

    assert result.text == source
    assert result.warnings == ()
    assert completed == [result]
    assert len(provider.prompts) == 2
    assert all(user == source for _, user in provider.prompts)
    assert "missing-verbatim-number" in provider.prompts[1][0]
    assert '"120": 1' in provider.prompts[1][0]
    assert '"3": 1' in provider.prompts[1][0]


@pytest.mark.asyncio
async def test_fidelity_retry_budget_can_be_disabled() -> None:
    class EmptyProvider:
        name = "empty"
        calls = 0

        async def complete(self, **_: object) -> str:
            self.calls += 1
            return ""

    source = "A substantive sentence."
    chunk = Chunk(ChunkId("empty"), DocumentId("empty-doc"), 0, source, 0, len(source), "b" * 64)
    provider = EmptyProvider()
    cleaner = CleanChunks(
        provider,
        PromptCatalog(),
        "cmos_v5",
        "test",
        max_fidelity_retries=0,  # type: ignore[arg-type]
    )
    with pytest.raises(FidelityValidationError, match="after 1 attempt"):
        await cleaner.execute([chunk])
    assert provider.calls == 1


def test_cleaner_revalidates_stale_cached_output() -> None:
    source = "The reference contains sections 67, 69, and 99-109."
    chunk = Chunk(
        id=ChunkId("chunk_cached_fidelity"),
        document_id=DocumentId("doc_cached_fidelity"),
        ordinal=0,
        text=source,
        start_char=0,
        end_char=len(source),
        sha256="b" * 64,
    )
    cleaner = CleanChunks(
        provider=object(),  # type: ignore[arg-type]
        prompt_catalog=PromptCatalog(),
        prompt_version="cmos_v5",
        model="test",
    )
    stale = CleanedChunk(
        chunk=chunk,
        text="The reference contains sections 99-109.",
        warnings=(),
    )

    result = cleaner.revalidate(stale)

    assert result.text == source
    assert "missing-verbatim-number" in result.warnings
    assert "source-preserved-after-fidelity-check" in result.warnings


def test_settings_reject_invalid_coherence_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIBRARIAN_COHERENCE_MODE", "fictional")
    with pytest.raises(ValidationError):
        Settings()


def test_run_stage_order_matches_processing_pipeline() -> None:
    assert list(RunStage).index(RunStage.ASSEMBLE) < list(RunStage).index(RunStage.CLASSIFY)


@pytest.mark.parametrize("marker", ["[17]", "[^17]", "<sup>17</sup>"])
def test_citation_identity_survives_equivalent_marker_formatting(marker: str) -> None:
    source = "A supported statement [17], and another [18]."
    result = validate_cleaned_text(
        f"A supported statement {marker}, and another [18].",
        input_size=len(source),
        source_text=source,
    )
    assert "missing-citation-marker" not in result.warnings


@pytest.mark.parametrize(
    "candidate",
    [
        "A supported statement [17], and another.",
        "A supported statement [17], and another [17].",
        "A supported statement <sup>19</sup>, and another [18].",
    ],
)
def test_citation_check_detects_partial_loss_and_changed_identities(candidate: str) -> None:
    source = "A supported statement [17], and another [18]."
    result = validate_cleaned_text(candidate, input_size=len(source), source_text=source)
    assert "missing-citation-marker" in result.warnings


def test_ordinary_link_labels_are_not_citation_identities() -> None:
    source = "Read [the guide](https://example.com/docs) for details."
    output = "Read [the documentation](https://example.com/docs) for details."
    result = validate_cleaned_text(output, input_size=len(source), source_text=source)
    assert "missing-citation-marker" not in result.warnings
    assert "changed-literal-link" not in result.warnings


def test_linked_superscript_citation_preserves_identity() -> None:
    source = "A supported statement [17]."
    output = 'A supported statement <sup id="ref17"><a href="#note17">17</a></sup>.'
    result = validate_cleaned_text(output, input_size=len(source), source_text=source)
    assert "missing-citation-marker" not in result.warnings


def test_author_year_citation_spacing_is_typographical() -> None:
    source = "An observation (Smith,2020)."
    output = "An observation (Smith, 2020)."
    result = validate_cleaned_text(output, input_size=len(source), source_text=source)
    assert "missing-citation-marker" not in result.warnings


@pytest.mark.parametrize("digit", ["2", "7", "9"])
def test_temporal_decade_lexical_repair_keeps_one_canonical_reading(digit: str) -> None:
    source = f"Across decades, that style was popular during the '{digit}s."
    result = validate_cleaned_text(
        f"Across decades, that style was popular during the ’{digit}0s.",
        input_size=len(source),
        source_text=source,
    )
    assert "missing-verbatim-number" not in result.warnings
    assert "added-verbatim-number" not in result.warnings
    assert "repaired-decade-shorthand" in result.warnings


@pytest.mark.parametrize(
    "source, output",
    [
        ("The interval was 9s.", "The interval was 90s."),
        ("The model was '9s'.", "The model was '90s'."),
        ("We worked in the '9s' series.", "We worked in the '90s' series."),
        ("The version was v9.0.", "The version was v90.0."),
        ("A literal: `in the '9s`.", "A literal: `in the '90s`."),
        ("This happened in the '9s.", "This happened in the ’80s."),
        ("This happened in the '9s.", "This happened in the 1990s."),
        (
            "In the '9s, a decade of change, the dose was 9 mg.",
            "In the ’90s, a decade of change, the dose was 90 mg.",
        ),
        ("During the '9s interval, we waited.", "During the ’90s interval, we waited."),
        (
            "Decades later, we tested in the '9s interval.",
            "Decades later, we tested in the ’90s interval.",
        ),
        ("It happened during the '9s.", "It happened during the ’90s."),
    ],
)
def test_decade_repair_does_not_relax_numeric_fact_or_identifier_protection(
    source: str,
    output: str,
) -> None:
    result = validate_cleaned_text(output, input_size=len(source), source_text=source)
    assert "missing-verbatim-number" in result.warnings
    assert "added-verbatim-number" in result.warnings


def test_local_query_can_preserve_an_unresolved_decade_spelling() -> None:
    source = "This happened in the '9s, a decade of change."
    candidate = "This happened in the [transcription unclear: '9s], a decade of change."
    result = validate_cleaned_text(candidate, input_size=len(source), source_text=source)
    assert "uncertain-transcription" in result.warnings
    assert "missing-verbatim-number" not in result.warnings
    assert "added-verbatim-number" not in result.warnings


def test_decade_role_uses_read_only_neighbors_without_counting_their_numbers() -> None:
    from librarian.pipeline.validation import SourceContext

    source = "'7s"
    context = SourceContext("The earlier dose was 12.5mg. During the ", ", a decade of change.")
    result = validate_cleaned_text(
        "’70s", input_size=len(source), source_text=source, context=context
    )
    assert "repaired-decade-shorthand" in result.warnings
    assert "missing-verbatim-number" not in result.warnings
    assert "added-verbatim-number" not in result.warnings


def test_duration_context_prevents_decade_repair_at_a_transport_boundary() -> None:
    from librarian.pipeline.validation import SourceContext

    source = "'9s"
    context = SourceContext("Decades later, we tested during the ", " interval.")
    result = validate_cleaned_text(
        "’90s", input_size=len(source), source_text=source, context=context
    )
    assert "missing-verbatim-number" in result.warnings
    assert "added-verbatim-number" in result.warnings


def test_transcription_metadata_can_be_normalized_without_losing_a_citation() -> None:
    source = "The shipment contains 318 units of [unintelligible]."
    result = validate_cleaned_text(
        "The shipment contains 318 units of [transcription unclear: unintelligible].",
        input_size=len(source),
        source_text=source,
    )
    assert "uncertain-transcription" in result.warnings
    assert "missing-citation-marker" not in result.warnings


def test_explicit_footnote_label_stays_protected_even_if_it_names_an_audio_marker() -> None:
    source = "The statement has support [^music]."
    result = validate_cleaned_text(
        "The statement has support.", input_size=len(source), source_text=source
    )
    assert "missing-citation-marker" in result.warnings


@pytest.mark.parametrize("distance", [0, 200, 1000, 4000])
def test_decade_reading_uses_available_paragraph_evidence_not_character_radius(
    distance: int,
) -> None:
    source = (
        "It happened in the '9s. "
        + ("Archive details. " * distance)
        + " Records from the '80s remain."
    )
    candidate = source.replace("'9s", "'90s")
    result = validate_cleaned_text(candidate, input_size=len(source), source_text=source)
    assert not FIDELITY_WARNINGS.intersection(result.warnings)
    assert "repaired-decade-shorthand" in result.warnings


def test_decade_evidence_does_not_cross_a_source_paragraph_boundary() -> None:
    source = "It happened in the '9s.\n\nRecords from the '80s remain."
    candidate = source.replace("'9s", "'90s")
    result = validate_cleaned_text(candidate, input_size=len(source), source_text=source)
    assert "missing-verbatim-number" in result.warnings
    assert "added-verbatim-number" in result.warnings
