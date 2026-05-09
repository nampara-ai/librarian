from typing import Any

import pytest
from pydantic import ValidationError

from librarian.config import Settings


@pytest.mark.parametrize(
    "kwargs",
    [
        {"job_backend": "bogus"},
        {"job_max_concurrency": 0},
        {"chunk_target_chars": 0},
        {"chunk_overlap_chars": -1},
        {"chunk_target_chars": 100, "chunk_overlap_chars": 100},
        {"api_max_upload_bytes": 0},
        {"max_source_bytes": 0},
        {"text_max_input_bytes": 0},
        {"docx_max_input_bytes": 0},
        {"pdf_max_input_bytes": 0},
        {"pdf_max_pages": 0},
        {"ocr_pdf_max_pages": 0},
        {"ocr_llm_correction": "bogus"},
        {"ocr_page_concurrency": 0},
    ],
)
def test_settings_reject_invalid_runtime_controls(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(**kwargs)


def test_settings_default_to_v2_prompt_stack() -> None:
    settings = Settings()

    assert settings.cleaning_prompt_version == "cmos_v2"
    assert settings.classification_prompt_version == "dewey_v2"
