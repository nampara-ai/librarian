import json

import pytest

from librarian.application.source_reading import read_source
from librarian.prompts import PromptCatalog


@pytest.mark.asyncio
async def test_reading_retries_schema_errors_without_inventing_an_inventory() -> None:
    class Reader:
        name = "reader"
        calls = 0

        async def complete(self, *, user_prompt: str, **_: object) -> str:
            self.calls += 1
            payload = json.loads(user_prompt)
            assert payload["source"] == "She said it might work."
            if self.calls == 1:
                return '{"claims": "incorrect shape"}'
            assert "validation_error" in payload
            return json.dumps(
                {
                    "claims": ["She expresses a possibility."],
                    "qualifiers": ["might"],
                    "rhetoric": [],
                    "voice": [],
                    "discarded_false_starts": [],
                }
            )

    reader = Reader()
    result = await read_source(
        reader,
        PromptCatalog(),
        "test",
        "She said it might work.",
        "",
        "",
        max_tokens=1000,
        temperature=0.0,
    )
    assert result.qualifiers == ["might"]
    assert reader.calls == 2


@pytest.mark.asyncio
async def test_invalid_reading_cannot_silently_become_an_empty_success() -> None:
    class Reader:
        name = "reader"

        async def complete(self, **_: object) -> str:
            return "{}"

    with pytest.raises(ValueError, match="required schema"):
        await read_source(
            Reader(),
            PromptCatalog(),
            "test",
            "A sentence.",
            "",
            "",
            max_tokens=1000,
            temperature=0.0,
        )
