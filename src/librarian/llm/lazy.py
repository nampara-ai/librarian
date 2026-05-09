"""Lazy LLM provider construction."""

from __future__ import annotations

from dataclasses import dataclass

from librarian.application.ports import LLMProvider
from librarian.config import Settings
from librarian.llm.mock import MockLLMProvider
from librarian.llm.openai_compatible import OpenAICompatibleProvider


@dataclass(slots=True)
class LazyLLMProvider:
    """Build the configured LLM provider only when a completion is requested."""

    settings: Settings
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

    def _get_provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = build_provider(self.settings)
        return self._provider


def build_provider(settings: Settings) -> LLMProvider:
    """Build the configured concrete LLM provider."""
    if settings.llm_provider == "mock":
        return MockLLMProvider()
    if settings.llm_provider == "openai-compatible":
        return OpenAICompatibleProvider(
            api_key_env=settings.llm_api_key_env,
            base_url=settings.llm_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
            max_concurrency=settings.llm_max_concurrency,
            max_retries=settings.llm_max_retries,
            retry_base_delay_seconds=settings.llm_retry_base_delay_seconds,
            retry_max_delay_seconds=settings.llm_retry_max_delay_seconds,
        )
    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")
