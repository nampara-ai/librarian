"""OpenAI-compatible async LLM adapter."""

from __future__ import annotations

import asyncio
import logging
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

from librarian.application.errors import LLMAccessError, LLMResponseTruncatedError
from librarian.observability import sanitize_error_message

_LOGGER = logging.getLogger("librarian.llm.openai_compatible")


def _content_or_raise(response: ChatCompletion) -> str:
    """Return the completion text, raising if the model output was truncated."""
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise LLMResponseTruncatedError(
            "LLM response was truncated at the output token limit; "
            "increase max_tokens (LIBRARIAN_LLM_MAX_OUTPUT_TOKENS)",
            partial_text=choice.message.content or "",
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
# Longest self-chosen pause between throttled attempts when the server sends
# no Retry-After; the total wait is bounded by rate_limit_wait_seconds.
_MAX_RATE_LIMIT_BACKOFF_SECONDS = 60.0


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
        rate_limit_wait_seconds: float = 900.0,
        metrics: LLMUsageMetrics | None = None,
        prompt_cost_per_1k_tokens_usd: float = 0.0,
        completion_cost_per_1k_tokens_usd: float = 0.0,
        reasoning_effort: str | None = None,
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
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._retry_max_delay_seconds = retry_max_delay_seconds
        self._rate_limit_wait_seconds = rate_limit_wait_seconds
        self._metrics = metrics
        self._prompt_cost_per_1k_tokens_usd = prompt_cost_per_1k_tokens_usd
        self._completion_cost_per_1k_tokens_usd = completion_cost_per_1k_tokens_usd
        self._reasoning_effort = reasoning_effort
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
        return await self._complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            json_output=False,
        )

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        return await self._complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            json_output=True,
        )

    async def _complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
        json_output: bool,
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
                json_output=json_output,
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
        json_output: bool = False,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": model, "messages": cast("Any", messages)}
        if prefs.use_max_completion_tokens:
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens
        if not prefs.omit_temperature:
            kwargs["temperature"] = temperature
        if self._reasoning_effort is not None:
            kwargs["reasoning_effort"] = self._reasoning_effort
        if json_output:
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    async def _chat_with_retries(
        self,
        *,
        messages: list[Any],
        model: str,
        max_tokens: int,
        temperature: float,
        json_output: bool = False,
    ) -> ChatCompletion:
        prefs = self._param_prefs.setdefault(model, ModelParamPrefs())
        # One adaptation per parameter at most; a dialect mismatch is resolved
        # by resending immediately (no backoff, no transient-retry budget
        # consumed). Anything the server still rejects afterwards is a genuine
        # request error and surfaces as such.
        max_adaptations = 2
        adaptations = 0
        attempt = 0
        throttled = 0
        throttled_seconds = 0.0
        while True:
            kwargs = self._request_kwargs(
                prefs=prefs,
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                json_output=json_output,
            )
            try:
                # Kwargs are assembled dynamically (dialect varies per model),
                # which defeats the SDK's typed overloads; the API contract
                # is still a ChatCompletion.
                # HTTP read timeouts reset when bytes arrive. Own a total attempt
                # deadline as well, so a slow or unfinished response cannot live forever.
                async with asyncio.timeout(self._timeout_seconds):
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
                    blocked = provider_access_failure(exc)
                    if blocked:
                        raise LLMAccessError(
                            f"LLM provider request failed: {sanitize_error_message(exc)}",
                            reason=blocked,
                        ) from None
                    raise RuntimeError(
                        f"LLM provider request failed: {sanitize_error_message(exc)}"
                    ) from None
                server_delay = retry_after_seconds(exc)
                if is_rate_limit_error(exc):
                    # Throttling is not a failure of the request: wait as long as
                    # the server asks (or back off to a minute) and try again,
                    # for up to rate_limit_wait_seconds in total, without
                    # spending the transient-retry budget.
                    if throttled_seconds >= self._rate_limit_wait_seconds:
                        raise RuntimeError(
                            "LLM provider kept rate-limiting requests for "
                            f"{int(throttled_seconds)}s: {sanitize_error_message(exc)}"
                        ) from None
                    if server_delay is None:
                        delay = min(
                            max(self._retry_base_delay_seconds, 1.0) * (2**throttled),
                            _MAX_RATE_LIMIT_BACKOFF_SECONDS,
                        )
                        delay += random.uniform(0, delay * 0.1)  # noqa: S311
                    else:
                        delay = max(server_delay, 0.5)
                    delay = min(delay, self._rate_limit_wait_seconds - throttled_seconds)
                    _LOGGER.warning(
                        "llm-rate-limited",
                        extra={
                            "model": model,
                            "wait_seconds": round(delay, 1),
                            "waited_seconds": round(throttled_seconds, 1),
                        },
                    )
                    await asyncio.sleep(delay)
                    throttled += 1
                    throttled_seconds += delay
                    continue
                if attempt >= self._max_retries:
                    detail = (
                        "request deadline exceeded"
                        if isinstance(exc, TimeoutError)
                        else sanitize_error_message(exc)
                    )
                    raise RuntimeError(
                        f"LLM provider request failed after retries: {detail}"
                    ) from None
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


def provider_access_failure(exc: Exception) -> str | None:
    if not isinstance(exc, APIStatusError):
        return None
    if int(exc.status_code) in {401, 403}:
        return "credentials-or-permissions"
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        payload = cast("dict[str, object]", body)
        error = payload.get("error", payload)
        if isinstance(error, dict):
            details = cast("dict[str, object]", error)
            for field in ("code", "type"):
                value = details.get(field)
                if isinstance(value, str) and value in {
                    "insufficient_quota",
                    "credit_balance_exhausted",
                }:
                    return "credit-exhausted"
    return None


def is_rate_limit_error(exc: Exception) -> bool:
    """Whether the provider throttled the request (HTTP 429)."""
    if isinstance(exc, RateLimitError):
        return True
    return isinstance(exc, APIStatusError) and int(exc.status_code) == 429


def is_retriable_openai_error(exc: Exception) -> bool:
    """Return true for transient OpenAI-compatible transport/status failures."""
    if provider_access_failure(exc):
        return False
    if isinstance(
        exc,
        (TimeoutError, APIConnectionError, APITimeoutError, InternalServerError, RateLimitError),
    ):
        return True
    if isinstance(exc, APIStatusError):
        return int(exc.status_code) >= 500
    return False
