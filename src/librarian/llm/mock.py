"""Deterministic LLM provider for tests and local dry runs."""

from __future__ import annotations


class MockLLMProvider:
    """A no-network provider that echoes cleaned-looking text."""

    name = "mock"

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        del system_prompt, model, max_tokens, temperature
        return " ".join(user_prompt.split())
