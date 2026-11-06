import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from openai.types.chat import ChatCompletion

from librarian.llm.openai_compatible import OpenAICompatibleProvider


@pytest.mark.asyncio
async def test_unfinished_sdk_request_is_cancelled_and_retries_remain_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    cancellations = 0

    async def create(**_: object) -> ChatCompletion:
        nonlocal calls, cancellations
        calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellations += 1
            raise
        raise AssertionError("A hanging request must never report success")

    def client(**_: object) -> Any:
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setenv("LIBRARIAN_TEST_DEADLINE_KEY", "test")
    monkeypatch.setattr("librarian.llm.openai_compatible.AsyncOpenAI", client)
    provider = OpenAICompatibleProvider(
        api_key_env="LIBRARIAN_TEST_DEADLINE_KEY",
        base_url=None,
        timeout_seconds=0.01,
        max_concurrency=1,
        max_retries=1,
        retry_base_delay_seconds=0,
        retry_max_delay_seconds=0,
    )
    with pytest.raises(RuntimeError, match="request deadline exceeded"):
        await provider.complete(
            system_prompt="Edit.",
            user_prompt="Source.",
            model="test",
            max_tokens=100,
            temperature=0.0,
        )
    assert calls == 2
    assert cancellations == 2
