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
    ],
)
def test_settings_reject_invalid_runtime_controls(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Settings(**kwargs)
