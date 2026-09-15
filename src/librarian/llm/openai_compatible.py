"""OpenAI-compatible async LLM adapter."""

from __future__ import annotations

import asyncio
import math
import os
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, cast

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from openai.types.chat import ChatCompletion

from librarian.observability import sanitize_error_message


class LLMResponseTruncatedError(RuntimeError):
    """Raised when the model stopped because it hit the output token cap.

    Silently returning a truncated completion is a correctness hazard — the
    caller (chunk cleaner, classifier) cannot tell a mid-sentence cutoff from a
    complete answer — so we fail loudly and let it surface as a run error.
    """


def _content_or_raise(response: ChatCompletion) -> str:
    """Return the completion text, raising if the model output was truncated."""
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise LLMResponseTruncatedError(
            "LLM response was truncated at the output token limit; "
            "increase max_tokens (LIBRARIAN_LLM_MAX_OUTPUT_TOKENS)"
        )
    return choice.message.content or ""


@dataclass(slots=True)
class ModelParamPrefs:
    """Request-parameter dialect learned from the server, per model.

    Newer OpenAI models reject ``max_tokens`` and require
    ``max_completion_tokens``; reasoning models (o-series, GPT-5+) additionally
    reject any non-default ``temperature``. Other OpenAI-compatible servers
    (Ollama, LM Studio, DeepSeek, older gateways) accept only the classic
    parameters. Rather than hardcode either dialect, start with the classic
    form and adapt when a 400 names the offending parameter, remembering the
    result per model so a run of many chunks pays at most one extra round-trip
    per parameter instead of one per chunk.
    """

    use_max_completion_tokens: bool = False
    omit_temperature: bool = False


_UNSUPPORTED_HINT = re.compile(r"unsupported|not supported|does not support", re.I)
_MAX_TOKENS_HINT = re.compile(r"max_completion_tokens|max_tokens", re.I)
_TEMPERATURE_HINT = re.compile(r"\btemperature\b", re.I)
_MAX_SERVER_RETRY_AFTER_SECONDS = 120.0


def unsupported_parameter(exc: Exception) -> str | None:
    """Return ``"max_tokens"`` or ``"temperature"`` when a 400 rejects that parameter.

    Prefers the structured ``param`` field the OpenAI API sets; falls back to
    the error message for compatible servers that omit it, but only when the
    message actually says the parameter is unsupported, so an unrelated 400
    that merely mentions the word is never mistaken for a dialect mismatch.
    """
    if not isinstance(exc, APIStatusError) or int(exc.status_code) != 400:
        return None
    param = getattr(exc, "param", None)
    if param == "max_tokens":
        return "max_tokens"
    if param == "temperature":
        return "temperature"
    if param is not None:
        return None
    body = getattr(exc, "body", None)
    message = str(exc)
    if isinstance(body, dict):
        message = f"{message} {body.get('message', '')}"
    if not _UNSUPPORTED_HINT.search(message):
        return None
    if _MAX_TOKENS_HINT.search(message):
        return "max_tokens"
    if _TEMPERATURE_HINT.search(message):
        return "temperature"
    return None


def retry_after_seconds(exc: Exception) -> float | None:
    """Read a server's retry delay from an HTTP error, bounded for an interactive run."""
    if not isinstance(exc, APIStatusError):
        return None
    headers = exc.response.headers
    for name, divisor in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            seconds = float(raw) / divisor
        except ValueError:
            continue
        if math.isfinite(seconds) and seconds >= 0:
            return min(seconds, _MAX_SERVER_RETRY_AFTER_SECONDS)
    raw_date = headers.get("retry-after")
    if raw_date is None:
        return None
    try:
        retry_at = parsedate_to_datetime(raw_date)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return min(
        max(0.0, (retry_at - datetime.now(UTC)).total_seconds()),
        _MAX_SERVER_RETRY_AFTER_SECONDS,
    )


class LLMUsageMetrics(Protocol):
    """Metrics sink for provider token usage."""

    def record_llm_usage(
        self,
        *,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        estimated_cost_usd: float = 0.0,
    ) -> None: ...


class OpenAICompatibleProvider:
    """LLM provider for OpenAI-compatible chat completion APIs."""

    name = "openai-compatible"

    def __init__(
        self,
        *,
        api_key_env: str,
        base_url: str | None,
        timeout_seconds: float,
        max_concurrency: int,
        max_retries: int = 5,
        retry_base_delay_seconds: float = 0.5,
        retry_max_delay_seconds: float = 10.0,
        metrics: LLMUsageMetrics | None = None,
        prompt_cost_per_1k_tokens_usd: float = 0.0,
        completion_cost_per_1k_tokens_usd: float = 0.0,
    ) -> None:
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ValueError(f"Missing API key environment variable: {api_key_env}")

        # The adapter owns retries. Leaving the SDK's own retry loop enabled
        # multiplies attempts (and rate-limit pressure) behind each chunk.
        self._client = AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout_seconds, max_retries=0
        )
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_retries = max_retries
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._retry_max_delay_seconds = retry_max_delay_seconds
        self._metrics = metrics
        self._prompt_cost_per_1k_tokens_usd = prompt_cost_per_1k_tokens_usd
        self._completion_cost_per_1k_tokens_usd = completion_cost_per_1k_tokens_usd
        # Parameter dialect learned per model (see ModelParamPrefs).
        self._param_prefs: dict[str, ModelParamPrefs] = {}

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        async with self._semaphore:
            response = await self._chat_with_retries(
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        self._record_usage(response, model=model)
        return _content_or_raise(response)

    async def describe_image(
        self,
        *,
        image_base64: str,
        media_type: str,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{image_base64}"},
                    },
                ],
            },
        ]
        async with self._semaphore:
            response = await self._chat_with_retries(
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        self._record_usage(response, model=model)
        return _content_or_raise(response)

    def _request_kwargs(
        self,
        *,
        prefs: ModelParamPrefs,
        messages: list[Any],
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": model, "messages": cast("Any", messages)}
        if prefs.use_max_completion_tokens:
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens
        if not prefs.omit_temperature:
            kwargs["temperature"] = temperature
        return kwargs

    async def _chat_with_retries(
        self,
        *,
        messages: list[Any],
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> ChatCompletion:
        prefs = self._param_prefs.setdefault(model, ModelParamPrefs())
        # One adaptation per parameter at most; a dialect mismatch is resolved
        # by resending immediately (no backoff, no transient-retry budget
        # consumed). Anything the server still rejects afterwards is a genuine
        # request error and surfaces as such.
        max_adaptations = 2
        adaptations = 0
        attempt = 0
        while True:
            kwargs = self._request_kwargs(
                prefs=prefs,
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            try:
                # Kwargs are assembled dynamically (dialect varies per model),
                # which defeats the SDK's typed overloads; the API contract
                # is still a ChatCompletion.
                return cast(
                    "ChatCompletion",
                    await self._client.chat.completions.create(**kwargs),
                )
            except Exception as exc:  # noqa: BLE001 - provider errors feed retry policy
                rejected = unsupported_parameter(exc)
                if adaptations < max_adaptations:
                    # Judge the in-flight request, not just shared preferences.
                    # Another concurrent chunk may have learned the dialect
                    # while this classic request was already on the wire.
                    if rejected == "max_tokens" and "max_tokens" in kwargs:
                        prefs.use_max_completion_tokens = True
                        adaptations += 1
                        continue
                    if rejected == "temperature" and "temperature" in kwargs:
                        prefs.omit_temperature = True
                        adaptations += 1
                        continue
                if not is_retriable_openai_error(exc):
                    raise RuntimeError(
                        f"LLM provider request failed: {sanitize_error_message(exc)}"
                    ) from None
                if attempt >= self._max_retries:
                    raise RuntimeError(
                        f"LLM provider request failed after retries: {sanitize_error_message(exc)}"
                    ) from None
                server_delay = retry_after_seconds(exc)
                if server_delay is None:
                    delay = min(
                        self._retry_base_delay_seconds * (2**attempt),
                        self._retry_max_delay_seconds,
                    )
                    delay += random.uniform(0, delay * 0.1)  # noqa: S311
                else:
                    delay = server_delay
                await asyncio.sleep(delay)
                attempt += 1

    def _record_usage(self, response: ChatCompletion, *, model: str) -> None:
        if self._metrics is None or response.usage is None:
            return
        prompt_tokens = int(response.usage.prompt_tokens or 0)
        completion_tokens = int(response.usage.completion_tokens or 0)
        total_tokens = int(response.usage.total_tokens or (prompt_tokens + completion_tokens))
        estimated_cost_usd = (
            prompt_tokens * self._prompt_cost_per_1k_tokens_usd / 1000
            + completion_tokens * self._completion_cost_per_1k_tokens_usd / 1000
        )
        self._metrics.record_llm_usage(
            provider=self.name,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated_cost_usd,
        )


def is_retriable_openai_error(exc: Exception) -> bool:
    """Return true for transient OpenAI-compatible transport/status failures."""
    if isinstance(exc, (APIConnectionError, APITimeoutError, InternalServerError, RateLimitError)):
        return True
    if isinstance(exc, APIStatusError):
        return int(exc.status_code) >= 500
    return False
