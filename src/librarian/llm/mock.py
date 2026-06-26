"""Deterministic LLM provider for tests and local dry runs."""

from __future__ import annotations

import json
import re


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
            summary_text = " ".join(user_prompt.split("Text to analyze:", 1)[-1].split())[:300]
            return json.dumps(
                {
                    "summary": summary_text,
                    "description": f"A document about {label}.",
                    "dewey_code": code,
                    "category_name": label,
                    "title": f"{label} Notes",
                    "tags": [part.strip().lower() for part in label.split("&")],
                    "confidence": 1.0,
                }
            )
        return _normalize_cleaned_text(user_prompt)

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
        del system_prompt, user_prompt, model, max_tokens, temperature
        # Deterministic, no-network stand-in: report the image size so tests and
        # dry runs get stable, inspectable output without a vision model.
        return f"Figure ({media_type}, {len(image_base64)} b64 chars): mock description."


def _normalize_cleaned_text(value: str) -> str:
    """Normalize intra-line whitespace while preserving document structure."""
    lines = [
        _normalize_common_ocr_noise(" ".join(line.split()))
        for line in value.splitlines()
    ]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _normalize_common_ocr_noise(value: str) -> str:
    substitutions = (
        (r"\bSadd1e\b", "Saddle"),
        (r"\bcanter transit10ns\b", "canter transitions"),
        (r"\bchain-of-cust0dy\b", "chain-of-custody"),
    )
    for pattern, replacement in substitutions:
        value = re.sub(pattern, replacement, value, flags=re.IGNORECASE)
    return value
