import pytest
from pydantic import ValidationError

from librarian.config import Settings
from librarian.domain.models import RunStage
from librarian.pipeline.validation import validate_cleaned_text


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


def test_settings_reject_invalid_coherence_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIBRARIAN_COHERENCE_MODE", "fictional")
    with pytest.raises(ValidationError):
        Settings()


def test_run_stage_order_matches_processing_pipeline() -> None:
    assert list(RunStage).index(RunStage.ASSEMBLE) < list(RunStage).index(RunStage.CLASSIFY)
