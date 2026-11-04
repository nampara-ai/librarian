"""Optional structured-output capability without extending the basic model port."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from librarian.application.ports import LLMProvider


async def complete_json(
    provider: LLMProvider,
    *,
    system_prompt: str,
    user_prompt: str,
    model: str,
    max_tokens: int,
    temperature: float,
    enabled: bool = True,
) -> str:
    method = getattr(provider, "complete_json", None) if enabled else None
    if callable(method):
        structured = cast("Callable[..., Awaitable[str]]", method)
        return await structured(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    # Custom providers retain compatibility; callers must validate the returned JSON.
    return await provider.complete(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
    )
