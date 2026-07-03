from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from librarian.config import Settings


@pytest.mark.parametrize(
    "kwargs",
    [
        {"job_backend": "bogus"},
        {"llm_provider": "bogus"},
        {"cleaning_prompt_version": "cmos_v9"},
        {"classification_prompt_version": "dewey_v9"},
        {"cleaning_mode": "experimental"},
        {"job_max_concurrency": 0},
        {"chunk_target_chars": 0},
        {"chunk_overlap_chars": -1},
        {"chunk_target_chars": 100, "chunk_overlap_chars": 100},
        {"api_max_request_bytes": 0},
        {"api_max_upload_bytes": 0},
        {"api_max_batch_files": 0},
        {"api_max_batch_bytes": 0},
        {"api_max_import_files": 0},
        {"api_max_import_bytes": 0},
        {"api_max_import_manifest_bytes": 0},
        {"api_max_content_chars": 0},
        {"api_rate_limit_per_minute": -1},
        {"api_trusted_proxy_cidrs": "not-a-network"},
        {"api_audit_retention_days": -1},
        {"log_level": "VERBOSE"},
        {"log_format": "xml"},
        {"max_source_bytes": 0},
        {"text_max_input_bytes": 0},
        {"docx_max_input_bytes": 0},
        {"pdf_max_input_bytes": 0},
        {"pdf_max_pages": 0},
        {"ocr_pdf_max_pages": 0},
        {"ocr_preprocess_mode": "posterize"},
        {"ocr_threshold": -1},
        {"ocr_threshold": 256},
        {"ocr_llm_correction": "bogus"},
        {"ocr_low_confidence_threshold": -1},
        {"ocr_low_confidence_threshold": 101},
        {"ocr_page_concurrency": 0},
        {"llm_prompt_cost_per_1k_tokens_usd": -0.01},
        {"llm_completion_cost_per_1k_tokens_usd": -0.01},
        {"llm_max_prompt_chars": 0},
        {"llm_max_response_chars": 0},
        {"figure_vision_min_bytes": 9000, "figure_vision_max_bytes": 8000},
    ],
)
def test_settings_reject_invalid_runtime_controls(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_redacted_config_masks_secrets_and_exposes_settings() -> None:
    settings = Settings(
        api_key="raw-secret",
        api_key_hashes="hash-secret",
        otel_headers="authorization=Bearer tok",
    )

    payload = settings.redacted_config()

    assert payload["api_key"] == "***redacted***"
    assert payload["api_key_hashes"] == "***redacted***"
    assert payload["otel_headers"] == "***redacted***"
    # Non-secret operational settings remain visible.
    assert payload["pdf_engine"] == "auto"
    assert payload["figure_vision_enabled"] is False
    # A secret left unset stays null rather than being masked.
    assert payload["api_keys"] is None


def test_settings_default_prompt_stack() -> None:
    settings = Settings()

    assert settings.cleaning_prompt_version == "cmos_v3"
    assert settings.classification_prompt_version == "dewey_v5"


def test_database_path_defaults_inside_data_dir() -> None:
    settings = Settings(data_dir=Path("/var/lib/librarian"))

    assert settings.database_path == Path("/var/lib/librarian/librarian.sqlite")


def test_database_path_default_matches_default_data_dir() -> None:
    settings = Settings()

    assert settings.database_path == settings.data_dir / "librarian.sqlite"


def test_explicit_database_path_is_preserved() -> None:
    settings = Settings(
        data_dir=Path("/var/lib/librarian"),
        database_path=Path("/elsewhere/custom.sqlite"),
    )

    assert settings.database_path == Path("/elsewhere/custom.sqlite")


def test_database_path_env_override_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIBRARIAN_DATA_DIR", "/var/lib/librarian")
    monkeypatch.setenv("LIBRARIAN_DATABASE_PATH", "/elsewhere/custom.sqlite")

    settings = Settings()

    assert settings.database_path == Path("/elsewhere/custom.sqlite")


def test_data_dir_env_moves_database_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIBRARIAN_DATA_DIR", "/var/lib/librarian")

    settings = Settings()

    assert settings.database_path == Path("/var/lib/librarian/librarian.sqlite")
