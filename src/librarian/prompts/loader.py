"""Versioned prompt catalog."""

from __future__ import annotations

from importlib.resources import files


class PromptCatalog:
    """Load bundled prompt text by family and version."""

    def get(self, family: str, version: str) -> str:
        resource = files("librarian.prompts").joinpath(family, f"{version}.md")
        return resource.read_text(encoding="utf-8")
