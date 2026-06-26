"""Lazy LLM provider construction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import cast

from librarian.application.ports import ApplicationMetrics, LLMProvider
from librarian.config import Settings
from librarian.llm.mock import MockLLMProvider
from librarian.llm.openai_compatible import OpenAICompatibleProvider


@dataclass(slots=True)
class LazyLLMProvider:
    """Build the configured LLM provider only when a completion is requested."""

    settings: Settings
    metrics: ApplicationMetrics | None = None
    _provider: LLMProvider | None = None

    @property
    def name(self) -> str:
        return self.settings.llm_provider

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        provider = self._get_provider()
        return await provider.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

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
        provider = self._get_provider()
        describe = getattr(provider, "describe_image", None)
        if not callable(describe):
            raise RuntimeError(f"LLM provider {provider.name!r} does not support vision input")
        describe = cast("Callable[..., Awaitable[str]]", describe)
        return await describe(
            image_base64=image_base64,
            media_type=media_type,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def _get_provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = build_provider(self.settings, metrics=self.metrics)
        return self._provider


@dataclass(slots=True)
class PromptSizeGuardProvider:
    """Reject oversized prompts before provider calls leave the process."""

    provider: LLMProvider
    max_prompt_chars: int
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = self.provider.name

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        prompt_chars = len(system_prompt) + len(user_prompt)
        if prompt_chars > self.max_prompt_chars:
            raise ValueError(
                "LLM prompt exceeded configured character limit "
                f"({prompt_chars} > {self.max_prompt_chars})"
            )
        return await self.provider.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )

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
        prompt_chars = len(system_prompt) + len(user_prompt)
        if prompt_chars > self.max_prompt_chars:
            raise ValueError(
                "LLM prompt exceeded configured character limit "
                f"({prompt_chars} > {self.max_prompt_chars})"
            )
        describe = getattr(self.provider, "describe_image", None)
        if not callable(describe):
            raise RuntimeError(f"LLM provider {self.provider.name!r} does not support vision input")
        describe = cast("Callable[..., Awaitable[str]]", describe)
        return await describe(
            image_base64=image_base64,
            media_type=media_type,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )


def build_provider(settings: Settings, *, metrics: ApplicationMetrics | None = None) -> LLMProvider:
    """Build the configured concrete LLM provider."""
    provider: LLMProvider
    if settings.llm_provider == "mock":
        provider = MockLLMProvider()
    elif settings.llm_provider == "openai-compatible":
        provider = OpenAICompatibleProvider(
            api_key_env=settings.llm_api_key_env,
            base_url=settings.llm_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
            max_concurrency=settings.llm_max_concurrency,
            max_retries=settings.llm_max_retries,
            retry_base_delay_seconds=settings.llm_retry_base_delay_seconds,
            retry_max_delay_seconds=settings.llm_retry_max_delay_seconds,
            metrics=metrics,
            prompt_cost_per_1k_tokens_usd=settings.llm_prompt_cost_per_1k_tokens_usd,
            completion_cost_per_1k_tokens_usd=settings.llm_completion_cost_per_1k_tokens_usd,
        )
    else:
        raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")
    return PromptSizeGuardProvider(
        provider=provider,
        max_prompt_chars=settings.llm_max_prompt_chars,
    )
