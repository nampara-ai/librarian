"""Output validation and LLM artifact filtering."""

from __future__ import annotations

import re
from dataclasses import dataclass

ARTIFACT_PATTERNS = [
    r"I am ready to (?:begin|process)",
    r"I'm ready to (?:begin|process|edit)",
    r"Please provide (?:the|your) (?:raw )?transcript",
    r"\[The assistant(?:'s response)? (?:would|will|is)",
    r"\[Omitted long matching line\]",
    r"(?:Here is|I have) (?:the )?cleaned (?:text|transcript)",
    r"As (?:an AI|a language model)",
    r"I don't have access to",
    r"I cannot (?:see|access|read)",
    r"(?:The )?(?:input|text) (?:provided )?(?:is|was) (?:empty|blank)",
    r"there is no (?:text|content|input) to",
]

ARTIFACT_REGEX = re.compile("|".join(ARTIFACT_PATTERNS), re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Validated text and warnings."""

    text: str
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())


def validate_cleaned_text(result: str, *, input_size: int) -> ValidationResult:
    """Remove obvious LLM artifacts and report suspicious output."""
    warnings: list[str] = []
    text = result.strip()

    if not text:
        return ValidationResult(text="", warnings=("empty-output",))

    if ARTIFACT_REGEX.search(text):
        warnings.append("artifact-filtered")
        lines = [line for line in text.splitlines() if not ARTIFACT_REGEX.search(line)]
        text = "\n".join(lines).strip()

    if not text:
        warnings.append("empty-after-artifact-filter")
        return ValidationResult(text="", warnings=tuple(warnings))

    if input_size > 100 and len(text) < input_size * 0.2:
        warnings.append("suspiciously-short-output")

    text = _normalize_output_whitespace(text)
    return ValidationResult(text=text, warnings=tuple(warnings))


def _normalize_output_whitespace(text: str) -> str:
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return text.strip()
