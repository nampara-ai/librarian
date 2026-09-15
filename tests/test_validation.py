import pytest
from pydantic import ValidationError

from librarian.application.clean_chunks import CleanChunks, CleanedChunk, strip_repeated_context
from librarian.config import Settings
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk, RunStage
from librarian.pipeline.validation import validate_cleaned_text
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
        "[CONTEXT: This continues from previous chunk]\n\n"
        "|\n"
        "| Name | Value |\n"
        "| Alice | 1 |\n"
        "-",
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
    source = "![](image_p1_0.png)\n\n" + " ".join(
        f"Detail {number}" for number in range(250)
    )
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
async def test_cleaner_preserves_source_when_model_loses_a_figure() -> None:
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

    result = (await cleaner.execute([chunk]))[0]

    assert result.text == source
    assert "changed-markdown-images" in result.warnings
    assert "source-preserved-after-fidelity-check" in result.warnings


@pytest.mark.asyncio
async def test_cleaner_preserves_source_when_model_loses_a_number() -> None:
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

    result = (await cleaner.execute([chunk]))[0]

    assert result.text == source
    assert "missing-verbatim-number" in result.warnings
    assert "source-preserved-after-fidelity-check" in result.warnings


@pytest.mark.asyncio
async def test_cleaner_preserves_source_when_model_adds_a_number() -> None:
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

    result = (await cleaner.execute([chunk]))[0]

    assert result.text == source
    assert "added-verbatim-number" in result.warnings
    assert "source-preserved-after-fidelity-check" in result.warnings


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
