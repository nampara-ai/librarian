"""Versioned prompt catalog."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from typing import Any


class PromptCatalog:
    """Load bundled prompt text by family and version."""

    def get(self, family: str, version: str) -> str:
        return _load_prompt(family, version)

    def clear_cache(self) -> None:
        """Clear cached prompt resources."""
        _load_prompt.cache_clear()

    def cache_info(self) -> Any:
        """Return prompt cache statistics."""
        return _load_prompt.cache_info()


@lru_cache(maxsize=64)
def _load_prompt(family: str, version: str) -> str:
        resource = files("librarian.prompts").joinpath(family, f"{version}.md")
        return resource.read_text(encoding="utf-8")
