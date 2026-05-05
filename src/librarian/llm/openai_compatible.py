"""OpenAI-compatible async LLM adapter."""

from __future__ import annotations

import asyncio
import os
import random

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion


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
    ) -> None:
        api_key = os.environ.get(api_key_env)
        if not api_key:
            raise ValueError(f"Missing API key environment variable: {api_key_env}")

        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds)
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_retries = max_retries
        self._retry_base_delay_seconds = retry_base_delay_seconds
        self._retry_max_delay_seconds = retry_max_delay_seconds

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
                last_error = exc
                if attempt >= self._max_retries:
                    raise
                delay = min(
                    self._retry_base_delay_seconds * (2**attempt),
                    self._retry_max_delay_seconds,
                )
                jitter = random.uniform(0, delay * 0.1)  # noqa: S311
                await asyncio.sleep(delay + jitter)

        if last_error is not None:
            raise last_error
        raise RuntimeError("LLM completion failed without an exception")
