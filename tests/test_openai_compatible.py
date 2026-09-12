from collections.abc import Callable
from typing import Any, cast

import httpx
import openai
import pytest

from librarian.config import Settings
from librarian.llm import build_provider
from librarian.llm.openai_compatible import (
    OpenAICompatibleProvider,
    is_retriable_openai_error,
    unsupported_parameter,
)
from librarian.observability import MetricsRecorder


# openai's exception constructors are type-stubbed against httpx2, while the
# tests build real httpx objects; the httpx/httpx2 split otherwise trips
# pyright on every dependency bump (CI resolves latest). Typing these as Any
# keeps pyright stable while runtime still uses genuine httpx objects.
def _fake_request() -> Any:
    return httpx.Request("POST", "https://api.example.test")


def _fake_response(status_code: int, request: Any) -> Any:
    return httpx.Response(status_code, request=request)


def test_openai_retry_classification() -> None:
    request = _fake_request()
    assert is_retriable_openai_error(openai.APITimeoutError(request))
    assert is_retriable_openai_error(openai.APIConnectionError(request=request))
    assert is_retriable_openai_error(
        openai.RateLimitError(
            "rate limited",
            response=_fake_response(429, request),
            body=None,
        )
    )
    assert is_retriable_openai_error(
        openai.APIStatusError(
            "server failed",
            response=_fake_response(503, request),
            body=None,
        )
    )

    assert not is_retriable_openai_error(
        openai.BadRequestError(
            "bad request",
            response=_fake_response(400, request),
            body=None,
        )
    )
    assert not is_retriable_openai_error(
        openai.AuthenticationError(
            "bad auth",
            response=_fake_response(401, request),
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
    request = _fake_request()
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
                    response=_fake_response(429, request),
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
    request = _fake_request()

    class FakeCompletions:
        async def create(self, **kwargs: object) -> object:
            del kwargs
            raise openai.AuthenticationError(
                "bad auth api_key=abc123 sk-testSECRET123",
                response=_fake_response(401, request),
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
    request = _fake_request()

    class FakeCompletions:
        async def create(self, **kwargs: object) -> object:
            del kwargs
            raise openai.RateLimitError(
                "rate limited token=abc123 sk-testSECRET123",
                response=_fake_response(429, request),
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


# --- request-dialect negotiation ---------------------------------------------
#
# Newer OpenAI models reject `max_tokens` (requiring `max_completion_tokens`)
# and reasoning models reject any non-default `temperature`; other
# OpenAI-compatible servers accept only the classic form. The provider must
# adapt on a 400 that names the parameter and remember the answer per model.

_MAX_TOKENS_MSG = (
    "Unsupported parameter: 'max_tokens' is not supported with this model. "
    "Use 'max_completion_tokens' instead."
)
_TEMPERATURE_MSG = (
    "Unsupported value: 'temperature' does not support 0 with this model. "
    "Only the default (1) value is supported."
)


def _bad_request(*, param: str | None, code: str, message: str) -> Any:
    body = {"message": message, "type": "invalid_request_error", "param": param, "code": code}
    return openai.BadRequestError(message, response=_fake_response(400, _fake_request()), body=body)


def _install_scripted_client(
    monkeypatch: pytest.MonkeyPatch,
    script: Callable[[dict[str, Any]], None],
) -> list[dict[str, Any]]:
    """Fake AsyncOpenAI whose create() records its kwargs, then runs `script`.

    `script(kwargs)` raises to fail that call or returns to succeed. The
    returned list holds every kwargs dict sent, so tests can assert exactly
    which parameters went over the wire on each attempt.
    """
    sent: list[dict[str, Any]] = []

    class FakeChoice:
        class Message:
            content = "ok"

        message = Message()

    class FakeCompletion:
        choices = [FakeChoice()]
        usage = None

    class FakeCompletions:
        async def create(self, **kwargs: Any) -> Any:
            sent.append(dict(kwargs))
            script(kwargs)
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
    return sent


def _scripted_provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        api_key_env="LIBRARIAN_TEST_API_KEY",
        base_url=None,
        timeout_seconds=1,
        max_concurrency=1,
        max_retries=0,
    )


async def _complete(provider: OpenAICompatibleProvider, model: str) -> str:
    return await provider.complete(
        system_prompt="system",
        user_prompt="user",
        model=model,
        max_tokens=8,
        temperature=0,
    )


def test_unsupported_parameter_classification() -> None:
    # The structured `param` field the OpenAI API sets is authoritative.
    assert (
        unsupported_parameter(
            _bad_request(param="max_tokens", code="unsupported_parameter", message=_MAX_TOKENS_MSG)
        )
        == "max_tokens"
    )
    assert (
        unsupported_parameter(
            _bad_request(param="temperature", code="unsupported_value", message=_TEMPERATURE_MSG)
        )
        == "temperature"
    )
    # Compatible servers that omit `param` are classified from the message...
    assert (
        unsupported_parameter(_bad_request(param=None, code="invalid", message=_MAX_TOKENS_MSG))
        == "max_tokens"
    )
    assert (
        unsupported_parameter(_bad_request(param=None, code="invalid", message=_TEMPERATURE_MSG))
        == "temperature"
    )
    # ...but only when it actually says the parameter is unsupported: a 400
    # that merely mentions max_tokens (a range error) must not trigger a
    # dialect switch.
    assert (
        unsupported_parameter(
            _bad_request(param=None, code="invalid", message="max_tokens must be at least 1")
        )
        is None
    )
    # A 400 naming an unrelated parameter is a genuine request error.
    assert (
        unsupported_parameter(
            _bad_request(
                param="model", code="model_not_found", message="The model `nope` does not exist"
            )
        )
        is None
    )
    # Non-400 failures are never dialect mismatches.
    assert (
        unsupported_parameter(
            openai.RateLimitError(
                "rate limited", response=_fake_response(429, _fake_request()), body=None
            )
        )
        is None
    )


@pytest.mark.asyncio
async def test_openai_provider_adapts_max_tokens_to_max_completion_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def script(kwargs: dict[str, Any]) -> None:
        if "max_tokens" in kwargs:
            raise _bad_request(
                param="max_tokens", code="unsupported_parameter", message=_MAX_TOKENS_MSG
            )

    sent = _install_scripted_client(monkeypatch, script)
    provider = _scripted_provider()

    assert await _complete(provider, "gpt-5") == "ok"

    # First send used the classic parameter; the immediate resend switched.
    assert "max_tokens" in sent[0] and "max_completion_tokens" not in sent[0]
    assert sent[1]["max_completion_tokens"] == 8 and "max_tokens" not in sent[1]
    assert len(sent) == 2

    # The dialect is remembered: the next call goes straight to the accepted form.
    assert await _complete(provider, "gpt-5") == "ok"
    assert len(sent) == 3
    assert sent[2]["max_completion_tokens"] == 8 and "max_tokens" not in sent[2]


@pytest.mark.asyncio
async def test_openai_provider_omits_temperature_when_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def script(kwargs: dict[str, Any]) -> None:
        if "temperature" in kwargs:
            raise _bad_request(
                param="temperature", code="unsupported_value", message=_TEMPERATURE_MSG
            )

    sent = _install_scripted_client(monkeypatch, script)
    provider = _scripted_provider()

    assert await _complete(provider, "o4-mini") == "ok"
    assert "temperature" in sent[0]
    assert "temperature" not in sent[1]
    assert len(sent) == 2

    assert await _complete(provider, "o4-mini") == "ok"
    assert len(sent) == 3 and "temperature" not in sent[2]


@pytest.mark.asyncio
async def test_openai_provider_adapts_both_params_for_reasoning_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A reasoning model rejects max_tokens first and, once that is fixed,
    # temperature — the provider converges in two adaptations, no backoff.
    def script(kwargs: dict[str, Any]) -> None:
        if "max_tokens" in kwargs:
            raise _bad_request(
                param="max_tokens", code="unsupported_parameter", message=_MAX_TOKENS_MSG
            )
        if "temperature" in kwargs:
            raise _bad_request(
                param="temperature", code="unsupported_value", message=_TEMPERATURE_MSG
            )

    sent = _install_scripted_client(monkeypatch, script)
    provider = _scripted_provider()

    assert await _complete(provider, "gpt-5") == "ok"
    assert len(sent) == 3
    final = sent[2]
    assert final["max_completion_tokens"] == 8
    assert "max_tokens" not in final and "temperature" not in final

    # Both learned: a follow-up is a single, correctly-shaped request.
    assert await _complete(provider, "gpt-5") == "ok"
    assert len(sent) == 4


@pytest.mark.asyncio
async def test_openai_provider_does_not_adapt_unrelated_bad_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def script(kwargs: dict[str, Any]) -> None:
        del kwargs
        raise _bad_request(
            param="model", code="model_not_found", message="The model `nope` does not exist"
        )

    sent = _install_scripted_client(monkeypatch, script)
    provider = _scripted_provider()

    with pytest.raises(RuntimeError, match="LLM provider request failed"):
        await _complete(provider, "nope")
    # No adaptation loop for a genuine request error: it fails fast on the
    # first attempt exactly like any other non-retriable 400.
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_openai_provider_param_adaptation_is_per_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def script(kwargs: dict[str, Any]) -> None:
        if kwargs["model"] == "gpt-5" and "max_tokens" in kwargs:
            raise _bad_request(
                param="max_tokens", code="unsupported_parameter", message=_MAX_TOKENS_MSG
            )

    sent = _install_scripted_client(monkeypatch, script)
    provider = _scripted_provider()

    await _complete(provider, "gpt-5")
    # gpt-5 learned the new dialect; an older model on the same provider must
    # still receive the classic parameter it expects.
    await _complete(provider, "gpt-4.1-mini")
    last = sent[-1]
    assert last["model"] == "gpt-4.1-mini"
    assert "max_tokens" in last and "max_completion_tokens" not in last
