"""Application service for chunk cleaning."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from librarian.application.ports import LLMProvider
from librarian.domain.models import Chunk
from librarian.pipeline.validation import validate_cleaned_text
from librarian.prompts.loader import PromptCatalog

CoherenceMode = Literal["fast", "balanced", "max-coherence"]


@dataclass(frozen=True, slots=True)
class CleanedChunk:
    """Cleaned chunk result."""

    chunk: Chunk
    text: str
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CleanChunks:
    """Clean chunks through an LLM provider."""

    provider: LLMProvider
    prompt_catalog: PromptCatalog
    prompt_version: str
    model: str
    max_tokens: int = 8192
    temperature: float = 0.0
    coherence_mode: CoherenceMode = "balanced"

    async def execute(self, chunks: list[Chunk]) -> list[CleanedChunk]:
        if self.coherence_mode == "max-coherence":
            return await self._clean_sequential(chunks)
        return await self._clean_parallel(chunks)

    async def _clean_parallel(self, chunks: list[Chunk]) -> list[CleanedChunk]:
        tasks = [self._clean_one(chunk, previous_context="") for chunk in chunks]
        return list(await asyncio.gather(*tasks))

    async def _clean_sequential(self, chunks: list[Chunk]) -> list[CleanedChunk]:
        results: list[CleanedChunk] = []
        previous_context = ""
        for chunk in chunks:
            result = await self._clean_one(chunk, previous_context=previous_context)
            results.append(result)
            previous_context = result.text[-500:]
        return results

    async def _clean_one(self, chunk: Chunk, *, previous_context: str) -> CleanedChunk:
        system_prompt = self.prompt_catalog.get("cleaning", self.prompt_version)
        user_prompt = chunk.text
        if previous_context:
            user_prompt = f"[CONTEXT: This continues from: {previous_context}]\n\n{chunk.text}"

        raw = await self.provider.complete(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        validated = validate_cleaned_text(raw, input_size=len(chunk.text))
        return CleanedChunk(chunk=chunk, text=validated.text, warnings=validated.warnings)
