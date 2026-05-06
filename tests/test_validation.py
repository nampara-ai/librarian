import pytest
from pydantic import ValidationError

from librarian.config import Settings
from librarian.domain.models import RunStage
from librarian.pipeline.validation import validate_cleaned_text


def test_validation_filters_artifact_lines() -> None:
    result = validate_cleaned_text(
        "Here is the cleaned transcript:\nActual cleaned content remains.",
        input_size=100,
    )

    assert result.text == "Actual cleaned content remains."
    assert "artifact-filtered" in result.warnings


def test_validation_reports_empty_output() -> None:
    result = validate_cleaned_text("   ", input_size=100)

    assert not result.ok
    assert result.warnings == ("empty-output",)


def test_settings_reject_invalid_coherence_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIBRARIAN_COHERENCE_MODE", "fictional")
    with pytest.raises(ValidationError):
        Settings()


def test_run_stage_order_matches_processing_pipeline() -> None:
    assert list(RunStage).index(RunStage.ASSEMBLE) < list(RunStage).index(RunStage.CLASSIFY)
