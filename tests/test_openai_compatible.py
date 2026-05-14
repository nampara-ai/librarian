from typing import cast

import httpx
import openai
import pytest

from librarian.config import Settings
from librarian.llm import build_provider
from librarian.llm.openai_compatible import OpenAICompatibleProvider, is_retriable_openai_error
from librarian.observability import MetricsRecorder


def test_openai_retry_classification() -> None:
    request = httpx.Request("POST", "https://api.example.test")
    assert is_retriable_openai_error(openai.APITimeoutError(request))
    assert is_retriable_openai_error(openai.APIConnectionError(request=request))
    assert is_retriable_openai_error(
        openai.RateLimitError(
            "rate limited",
            response=httpx.Response(429, request=request),
            body=None,
        )
    )
    assert is_retriable_openai_error(
        openai.APIStatusError(
            "server failed",
            response=httpx.Response(503, request=request),
            body=None,
        )
    )

    assert not is_retriable_openai_error(
        openai.BadRequestError(
            "bad request",
            response=httpx.Response(400, request=request),
            body=None,
        )
    )
    assert not is_retriable_openai_error(
        openai.AuthenticationError(
            "bad auth",
            response=httpx.Response(401, request=request),
            body=None,
        )
    )


def test_openai_provider_fast_fails_when_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIBRARIAN_TEST_MISSING_API_KEY", raising=False)

    with pytest.raises(ValueError, match="Missing API key environment variable"):
        OpenAICompatibleProvider(
            api_key_env="LIBRARIAN_TEST_MISSING_API_KEY",
            base_url=None,
            timeout_seconds=1,
            max_concurrency=1,
        )


@pytest.mark.asyncio
async def test_openai_provider_retries_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("POST", "https://api.example.test")
    calls = 0

    class FakeChoice:
        class Message:
            content = "ok"

        message = Message()

    class FakeCompletion:
        choices = [FakeChoice()]
        usage = None

    class FakeCompletions:
        async def create(self, **kwargs: object) -> FakeCompletion:
            del kwargs
            nonlocal calls
            calls += 1
            if calls == 1:
                raise openai.RateLimitError(
                    "rate limited",
                    response=httpx.Response(429, request=request),
                    body=None,
                )
            return FakeCompletion()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    def fake_async_openai(**kwargs: object) -> FakeClient:
        del kwargs
        return FakeClient()

    monkeypatch.setenv("LIBRARIAN_TEST_API_KEY", "test")
    monkeypatch.setattr("librarian.llm.openai_compatible.AsyncOpenAI", fake_async_openai)
    monkeypatch.setattr("librarian.llm.openai_compatible.asyncio.sleep", _no_sleep)

    provider = OpenAICompatibleProvider(
        api_key_env="LIBRARIAN_TEST_API_KEY",
        base_url=None,
        timeout_seconds=1,
        max_concurrency=1,
        max_retries=1,
    )

    result = await provider.complete(
        system_prompt="system",
        user_prompt="user",
        model="model",
        max_tokens=8,
        temperature=0,
    )

    assert result == "ok"
    assert calls == 2


@pytest.mark.asyncio
async def test_openai_provider_redacts_non_retriable_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "https://api.example.test")

    class FakeCompletions:
        async def create(self, **kwargs: object) -> object:
            del kwargs
            raise openai.AuthenticationError(
                "bad auth api_key=abc123 sk-testSECRET123",
                response=httpx.Response(401, request=request),
                body=None,
            )

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    def fake_async_openai(**kwargs: object) -> FakeClient:
        del kwargs
        return FakeClient()

    monkeypatch.setenv("LIBRARIAN_TEST_API_KEY", "test")
    monkeypatch.setattr("librarian.llm.openai_compatible.AsyncOpenAI", fake_async_openai)
    provider = OpenAICompatibleProvider(
        api_key_env="LIBRARIAN_TEST_API_KEY",
        base_url=None,
        timeout_seconds=1,
        max_concurrency=1,
        max_retries=0,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await provider.complete(
            system_prompt="system",
            user_prompt="user",
            model="model",
            max_tokens=8,
            temperature=0,
        )

    message = str(exc_info.value)
    assert message == "LLM provider request failed: bad auth api_key=[REDACTED] [REDACTED]"
    assert "abc123" not in message
    assert "sk-testSECRET123" not in message
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_openai_provider_redacts_retry_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "https://api.example.test")

    class FakeCompletions:
        async def create(self, **kwargs: object) -> object:
            del kwargs
            raise openai.RateLimitError(
                "rate limited token=abc123 sk-testSECRET123",
                response=httpx.Response(429, request=request),
                body=None,
            )

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    def fake_async_openai(**kwargs: object) -> FakeClient:
        del kwargs
        return FakeClient()

    monkeypatch.setenv("LIBRARIAN_TEST_API_KEY", "test")
    monkeypatch.setattr("librarian.llm.openai_compatible.AsyncOpenAI", fake_async_openai)
    monkeypatch.setattr("librarian.llm.openai_compatible.asyncio.sleep", _no_sleep)
    provider = OpenAICompatibleProvider(
        api_key_env="LIBRARIAN_TEST_API_KEY",
        base_url=None,
        timeout_seconds=1,
        max_concurrency=1,
        max_retries=1,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await provider.complete(
            system_prompt="system",
            user_prompt="user",
            model="model",
            max_tokens=8,
            temperature=0,
        )

    message = str(exc_info.value)
    assert (
        message
        == "LLM provider request failed after retries: rate limited token=[REDACTED] [REDACTED]"
    )
    assert "abc123" not in message
    assert "sk-testSECRET123" not in message
    assert exc_info.value.__cause__ is None


@pytest.mark.asyncio
async def test_openai_provider_records_token_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeChoice:
        class Message:
            content = "ok"

        message = Message()

    class FakeUsage:
        prompt_tokens = 11
        completion_tokens = 7
        total_tokens = 18

    class FakeCompletion:
        choices = [FakeChoice()]
        usage = FakeUsage()

    class FakeCompletions:
        async def create(self, **kwargs: object) -> FakeCompletion:
            del kwargs
            return FakeCompletion()

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    def fake_async_openai(**kwargs: object) -> FakeClient:
        del kwargs
        return FakeClient()

    metrics = MetricsRecorder()
    monkeypatch.setenv("LIBRARIAN_TEST_API_KEY", "test")
    monkeypatch.setattr("librarian.llm.openai_compatible.AsyncOpenAI", fake_async_openai)
    provider = OpenAICompatibleProvider(
        api_key_env="LIBRARIAN_TEST_API_KEY",
        base_url=None,
        timeout_seconds=1,
        max_concurrency=1,
        metrics=metrics,
        prompt_cost_per_1k_tokens_usd=0.01,
        completion_cost_per_1k_tokens_usd=0.02,
    )

    result = await provider.complete(
        system_prompt="system",
        user_prompt="user",
        model="test-model",
        max_tokens=8,
        temperature=0,
    )

    snapshot = metrics.snapshot()
    assert result == "ok"
    assert snapshot["llm_prompt_tokens_total"] == 11
    assert snapshot["llm_completion_tokens_total"] == 7
    assert snapshot["llm_tokens_total"] == 18
    total_cost = snapshot["llm_estimated_cost_usd_total"]
    assert isinstance(total_cost, int | float)
    assert abs(total_cost - 0.00025) < 1e-12
    assert snapshot["llm_tokens_by_model"] == {"openai-compatible:test-model": 18}
    costs = cast(dict[str, object], snapshot["llm_estimated_cost_usd_by_model"])
    model_cost = costs["openai-compatible:test-model"]
    assert isinstance(model_cost, int | float)
    assert abs(model_cost - 0.00025) < 1e-12


@pytest.mark.asyncio
async def test_build_provider_rejects_oversized_prompts_before_provider_call() -> None:
    provider = build_provider(Settings(llm_provider="mock", llm_max_prompt_chars=10))

    with pytest.raises(ValueError, match="LLM prompt exceeded configured character limit"):
        await provider.complete(
            system_prompt="system",
            user_prompt="this prompt is too long",
            model="mock-cleaner",
            max_tokens=8,
            temperature=0,
        )


async def _no_sleep(_: float) -> None:
    return None
