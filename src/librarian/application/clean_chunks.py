"""Application service for chunk cleaning."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal

from librarian.application.ports import LLMProvider
from librarian.domain.models import Chunk
from librarian.pipeline.validation import validate_cleaned_text
from librarian.prompts.loader import PromptCatalog

CoherenceMode = Literal["fast", "balanced", "max-coherence"]


async def _run_workers(
    worker: Callable[[], Coroutine[Any, Any, None]],
    worker_count: int,
) -> None:
    """Run N identical workers, cancelling the rest on the first failure.

    ``asyncio.gather`` propagates the first exception but leaves sibling
    workers running, orphaning their in-flight LLM calls (wasted cost and
    late progress callbacks). This cancels the remaining workers as soon as
    one fails and re-raises the original exception unwrapped, so upstream
    ``except ValueError`` / ``except ProcessingCanceled`` handlers still match
    (unlike ``asyncio.TaskGroup``, which wraps failures in an ExceptionGroup).
    """
    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(worker()) for _ in range(worker_count)
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    first_error: BaseException | None = None
    for task in tasks:
        if task.cancelled() or not task.done():
            continue
        exc = task.exception()
        if exc is not None:
            first_error = exc
            break
    if first_error is not None:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise first_error


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
    max_parallel_chunks: int = 8
    balanced_group_size: int = 4
    max_response_chars: int = 2 * 1024 * 1024
    # Trailing characters of the preceding chunk handed to the cleaner as
    # read-only continuity context. Chunks no longer overlap in the source, so
    # this replaces the (previously duplicated) overlap for boundary coherence.
    context_chars: int = 800

    async def execute(
        self,
        chunks: list[Chunk],
        *,
        on_chunk_cleaned: Callable[[], Awaitable[None]] | None = None,
    ) -> list[CleanedChunk]:
        """Clean chunks, invoking on_chunk_cleaned after each completion."""
        if self.coherence_mode == "fast":
            return await self._clean_parallel(chunks, on_chunk_cleaned)
        if self.coherence_mode == "balanced":
            return await self._clean_balanced(chunks, on_chunk_cleaned)
        if self.coherence_mode == "max-coherence":
            return await self._clean_sequential(chunks, on_chunk_cleaned)
        raise ValueError(f"Unsupported coherence mode: {self.coherence_mode}")

    async def _clean_parallel(
        self,
        chunks: list[Chunk],
        on_chunk_cleaned: Callable[[], Awaitable[None]] | None,
    ) -> list[CleanedChunk]:
        if not chunks:
            return []
        worker_count = max(1, min(self.max_parallel_chunks, len(chunks)))
        queue: asyncio.Queue[Chunk] = asyncio.Queue()
        for chunk in chunks:
            queue.put_nowait(chunk)
        results: list[CleanedChunk | None] = [None] * len(chunks)
        positions = {chunk.id: index for index, chunk in enumerate(chunks)}

        async def worker() -> None:
            while True:
                try:
                    chunk = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    # Unordered mode: give each chunk the raw tail of its
                    # predecessor as context so a boundary-split fragment still
                    # cleans coherently (context is read-only, never re-emitted).
                    index = positions[chunk.id]
                    previous_context = (
                        chunks[index - 1].text[-self.context_chars :] if index > 0 else ""
                    )
                    results[index] = await self._clean_one(
                        chunk,
                        previous_context=previous_context,
                    )
                    if on_chunk_cleaned is not None:
                        await on_chunk_cleaned()
                finally:
                    queue.task_done()

        await _run_workers(worker, worker_count)
        return [item for item in results if item is not None]

    async def _clean_balanced(
        self,
        chunks: list[Chunk],
        on_chunk_cleaned: Callable[[], Awaitable[None]] | None,
    ) -> list[CleanedChunk]:
        if not chunks:
            return []
        group_size = max(1, self.balanced_group_size)
        groups = [chunks[index : index + group_size] for index in range(0, len(chunks), group_size)]
        worker_count = max(1, min(self.max_parallel_chunks, len(groups)))
        queue: asyncio.Queue[tuple[int, list[Chunk]]] = asyncio.Queue()
        for index, group in enumerate(groups):
            queue.put_nowait((index, group))
        group_results: list[list[CleanedChunk] | None] = [None] * len(groups)

        async def worker() -> None:
            while True:
                try:
                    group_index, group = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    group_results[group_index] = await self._clean_sequential(
                        group, on_chunk_cleaned
                    )
                finally:
                    queue.task_done()

        await _run_workers(worker, worker_count)
        results: list[CleanedChunk] = []
        for group in group_results:
            if group is not None:
                results.extend(group)
        return results

    async def _clean_sequential(
        self,
        chunks: list[Chunk],
        on_chunk_cleaned: Callable[[], Awaitable[None]] | None = None,
    ) -> list[CleanedChunk]:
        results: list[CleanedChunk] = []
        previous_context = ""
        for chunk in chunks:
            result = await self._clean_one(chunk, previous_context=previous_context)
            results.append(result)
            previous_context = result.text[-self.context_chars :]
            if on_chunk_cleaned is not None:
                await on_chunk_cleaned()
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
        if len(raw) > self.max_response_chars:
            raise ValueError(
                "cleaning provider response exceeded configured character limit "
                f"({len(raw)} > {self.max_response_chars})"
            )
        validated = validate_cleaned_text(
            raw,
            input_size=len(chunk.text),
            source_text=chunk.text,
        )
        return CleanedChunk(chunk=chunk, text=validated.text, warnings=validated.warnings)
