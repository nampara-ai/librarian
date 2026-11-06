from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from librarian.application.clean_chunks import CleanChunks
from librarian.application.errors import LLMAccessError
from librarian.domain.ids import ChunkId, DocumentId
from librarian.domain.models import Chunk
from librarian.llm.openai_compatible import OpenAICompatibleProvider, is_retriable_openai_error
from librarian.prompts import PromptCatalog


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_exhausted_credit_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, nested: bool
) -> None:
    calls = 0
    request: Any = httpx.Request("POST", "https://api.example.test/v1/chat/completions")
    response: Any = httpx.Response(429, request=request)
    details = {"code": "credit_balance_exhausted", "type": "insufficient_quota"}
    error = openai.RateLimitError(
        "Credit exhausted", response=response, body={"error": details} if nested else details
    )

    async def create(**_: object) -> str:
        nonlocal calls
        calls += 1
        raise error

    def client(**_: object) -> Any:
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setenv("LIBRARIAN_TEST_CREDIT_KEY", "test")
    monkeypatch.setattr("librarian.llm.openai_compatible.AsyncOpenAI", client)
    provider = OpenAICompatibleProvider(
        api_key_env="LIBRARIAN_TEST_CREDIT_KEY",
        base_url=None,
        timeout_seconds=1,
        max_concurrency=1,
        max_retries=5,
    )
    with pytest.raises(LLMAccessError) as caught:
        await provider.complete(
            system_prompt="Edit.",
            user_prompt="Source.",
            model="test",
            max_tokens=100,
            temperature=0.0,
        )
    assert caught.value.reason == "credit-exhausted"
    assert calls == 1
    assert not is_retriable_openai_error(error)


def test_transient_rate_limits_remain_retriable_and_malformed_codes_do_not_crash() -> None:
    request: Any = httpx.Request("POST", "https://api.example.test")
    response: Any = httpx.Response(429, request=request)
    for body in ({"code": "rate_limit_exceeded"}, {"code": {"malformed": True}}):
        assert is_retriable_openai_error(
            openai.RateLimitError("Rate limited", response=response, body=body)
        )


@pytest.mark.asyncio
async def test_cleaning_preserves_access_error_type_for_batch_stop() -> None:
    class Provider:
        name = "blocked"
        calls = 0

        async def complete(self, **_: object) -> str:
            self.calls += 1
            raise LLMAccessError("Credit exhausted", reason="credit-exhausted")

    source = "The report is ready."
    chunk = Chunk(ChunkId("credit"), DocumentId("doc"), 0, source, 0, len(source), "source")
    provider = Provider()
    cleaner = CleanChunks(provider, PromptCatalog(), "cmos_v8", "test")
    with pytest.raises(LLMAccessError):
        await cleaner.execute([chunk])
    assert provider.calls == 1
