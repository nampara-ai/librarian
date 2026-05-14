"""OpenAI-compatible async LLM adapter."""

from __future__ import annotations

import asyncio
import os
import random
from typing import Protocol

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

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds)
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_retries = max_retries
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._retry_max_delay_seconds = retry_max_delay_seconds
        self._metrics = metrics
        self._prompt_cost_per_1k_tokens_usd = prompt_cost_per_1k_tokens_usd
        self._completion_cost_per_1k_tokens_usd = completion_cost_per_1k_tokens_usd

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        async with self._semaphore:
            response = await self._complete_with_retries(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        self._record_usage(response, model=model)
        return response.choices[0].message.content or ""

    async def _complete_with_retries(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> ChatCompletion:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await self._client.chat.completions.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                )
            except Exception as exc:
                if not is_retriable_openai_error(exc):
                    raise RuntimeError(
                        f"LLM provider request failed: {sanitize_error_message(exc)}"
                    ) from None
                last_error = exc
                if attempt >= self._max_retries:
                    raise RuntimeError(
                        "LLM provider request failed after retries: "
                        f"{sanitize_error_message(exc)}"
                    ) from None
                delay = min(
                    self._retry_base_delay_seconds * (2**attempt),
                    self._retry_max_delay_seconds,
                )
                jitter = random.uniform(0, delay * 0.1)  # noqa: S311
                await asyncio.sleep(delay + jitter)

        if last_error is not None:
            raise RuntimeError(
                "LLM provider request failed after retries: "
                f"{sanitize_error_message(last_error)}"
            ) from None
        raise RuntimeError("LLM completion failed without an exception")

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
