"""Deterministic LLM provider for tests and local dry runs."""

from __future__ import annotations

import json


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
        if user_prompt.startswith("Correct OCR text from PDF page"):
            return " ".join(user_prompt.rsplit("\n\n", 1)[-1].split())
        if "Text to analyze:" in user_prompt and "dewey_code" in user_prompt:
            text = user_prompt.split("Text to analyze:", 1)[-1].lower()
            code = "000"
            label = "Computer Science & Information"
            if any(term in text for term in ("horse", "equine", "colt", "mare", "stallion")):
                code = "636.1"
                label = "Horses & Equines"
            elif any(term in text for term in ("medicine", "health", "doctor", "disease")):
                code = "610"
                label = "Medicine & Health"
            elif any(term in text for term in ("writing", "literature", "novel", "poetry")):
                code = "800"
                label = "Literature"
            elif any(term in text for term in ("library", "catalog", "metadata", "search recall")):
                code = "020"
                label = "Library & Information Sciences"
            return json.dumps(
                {
                    "summary": " ".join(user_prompt.split("Text to analyze:", 1)[-1].split())[:300],
                    "dewey_code": code,
                    "category_name": label,
                    "confidence": 1.0,
                }
            )
        return _normalize_cleaned_text(user_prompt)


def _normalize_cleaned_text(value: str) -> str:
    """Normalize intra-line whitespace while preserving document structure."""
    lines = [" ".join(line.split()) for line in value.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)
