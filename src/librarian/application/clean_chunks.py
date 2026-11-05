"""Application service for chunk cleaning."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal, cast

from librarian.application.async_progress import await_with_progress
from librarian.application.errors import (
    LLMAccessError,
    LLMResponseTruncatedError,
    ProcessingCanceled,
)
from librarian.application.model_output import complete_json
from librarian.application.ports import LLMProvider
from librarian.application.review_items import UnitCheckpoint, mark_source, review_body
from librarian.application.source_reading import read_source
from librarian.domain.ids import ChunkId
from librarian.domain.models import Chunk
from librarian.observability import sanitize_error_message
from librarian.pipeline.chunking import chunk_separator_before
from librarian.pipeline.editing import (
    EditUnit,
    coalesce_adjacent_repairs,
    decode_edits,
    editing_units,
    unit_source_context,
    verification_pairs,
)
from librarian.pipeline.prose_patches import apply_prose_patches, candidate_spans
from librarian.pipeline.protected_text import protect_prose_data
from librarian.pipeline.validation import (
    DETERMINISTIC_WARNINGS,
    FIDELITY_WARNINGS,
    VERBATIM_NUMBER_REGEX,
    SourceContext,
    validate_cleaned_text,
)
from librarian.prompts.loader import PromptCatalog

CoherenceMode = Literal["fast", "balanced", "max-coherence"]


def _route_document_failures(
    units: Sequence[EditUnit], failures: dict[str, list[str]], checks: frozenset[str]
) -> None:
    if not checks:
        return
    for unit in units:
        if unit.kind == "prose":
            failures.setdefault(unit.id, []).append(
                "Document-level checks failed: "
                + ", ".join(sorted(checks))
                + ". Inspect the draft and joins; do not copy context into source."
            )


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
    tasks: list[asyncio.Task[None]] = [asyncio.create_task(worker()) for _ in range(worker_count)]
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
    checkpoint: tuple[UnitCheckpoint, ...] = ()

    @property
    def fidelity_failed(self) -> bool:
        return bool(FIDELITY_WARNINGS.intersection(self.warnings)) or (
            "source-preserved-after-fidelity-check" in self.warnings
        )


class FidelityValidationError(ValueError):
    """Cleaning exhausted its bounded repair budget without a faithful result."""

    def __init__(self, chunk: Chunk, attempts: int, warnings: tuple[str, ...]) -> None:
        self.chunk = chunk
        self.attempts = attempts
        self.warnings = warnings
        super().__init__(
            f"Cleaning chunk {chunk.ordinal + 1} failed fidelity checks after {attempts} "
            f"attempt(s): {', '.join(warnings)}. Original source remains available; "
            "no uncleaned replacement was published."
        )


ChunkCleanedCallback = Callable[[CleanedChunk], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class CleaningAttempt:
    chunk: Chunk
    number: int
    limit: int
    status: Literal[
        "started",
        "waiting",
        "verifying",
        "verified",
        "verification-rejected",
        "repair-reviewed",
        "accepted",
        "units-accepted",
        "rejected",
        "error",
    ]
    warnings: tuple[str, ...] = ()
    text: str | None = None
    elapsed_seconds: float = 0.0


AttemptCallback = Callable[[CleaningAttempt], Awaitable[None]]
UnitCheckpointCallback = Callable[[Chunk, tuple[UnitCheckpoint, ...]], Awaitable[None]]


_CONTEXT_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_MIN_DUPLICATED_CONTEXT_TOKENS = 15


def strip_repeated_context(text: str, previous_context: str) -> tuple[str, bool]:
    """Remove a context suffix copied by the model onto the next chunk.

    Matching token sequences makes this robust to punctuation and Markdown
    changes while retaining exact character boundaries in the model output.
    """
    if not previous_context or not text:
        return text, False
    context_tokens = [
        match.group(0).casefold() for match in _CONTEXT_TOKEN_RE.finditer(previous_context)
    ]
    output_matches = list(_CONTEXT_TOKEN_RE.finditer(text))
    output_tokens = [match.group(0).casefold() for match in output_matches]
    maximum = min(len(context_tokens), len(output_tokens))
    for count in range(maximum, _MIN_DUPLICATED_CONTEXT_TOKENS - 1, -1):
        if context_tokens[-count:] == output_tokens[:count]:
            remainder = text[output_matches[count - 1].end() :]
            return remainder.lstrip(" \t\r\n.,;:!?—–-*_").lstrip(), True
    return text, False


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
    max_fidelity_retries: int = 3
    unit_max_retries: int | None = None
    on_attempt: AttemptCallback | None = None
    on_unit_checkpoint: UnitCheckpointCallback | None = None
    resume_units: dict[ChunkId, tuple[UnitCheckpoint, ...]] = field(
        default_factory=lambda: dict[ChunkId, tuple[UnitCheckpoint, ...]](), repr=False
    )
    publish_partial: bool = False
    review_mode: Literal["off", "advisory", "strict"] = "strict"
    verify_semantics: bool = True
    editorial_context: str = ""
    provider_endpoint: str = ""
    progress_interval_seconds: float = 10.0
    verification_provider: LLMProvider | None = None
    verification_model: str | None = None
    verification_temperature: float = 0.0
    preparation_provider: LLMProvider | None = None
    generation_signature: str = ""
    source_reading: bool = False
    unit_target_chars: int = 2000
    document_source: str | None = field(default=None, repr=False)
    # Trailing characters of the preceding chunk handed to the cleaner as
    # read-only continuity context. Chunks no longer overlap in the source, so
    # this replaces the (previously duplicated) overlap for boundary coherence.
    context_chars: int = 800

    @property
    def source_aligned(self) -> bool:
        return self.prompt_version in {"cmos_v6", "cmos_v8"}

    @property
    def plain_edits(self) -> bool:
        return self.prompt_version == "cmos_v8"

    def _source_units(self, text: str) -> tuple[EditUnit, ...]:
        if not self.source_aligned:
            return ()
        return tuple(
            editing_units(
                text,
                target_chars=self.unit_target_chars if self.plain_edits else 4000,
                pack_prose_blocks=self.plain_edits,
            )
        )

    def cache_version(self, source: str) -> str:
        """Invalidate paid-work caches when source context or editorial policy changes."""
        prompts = {"cleaning": self.prompt_catalog.get("cleaning", self.prompt_version)}
        for name in (
            "fidelity_v1",
            "unit_fidelity_v1",
            "meaning_v1",
            "confirm_meaning_v1",
            "context_v1",
        ):
            prompts[name] = self.prompt_catalog.get("repair", name)
        if self.plain_edits:
            prompts["prose_fidelity_v1"] = self.prompt_catalog.get("repair", "prose_fidelity_v1")
            prompts["prose_patch_v1"] = self.prompt_catalog.get("repair", "prose_patch_v1")
            for name in ("prose_review_v8", "confirm_prose_v8"):
                prompts[name] = self.prompt_catalog.get("repair", name)
            if self.source_reading:
                prompts["source_reading_v2"] = self.prompt_catalog.get(
                    "repair", "source_reading_v2"
                )
        policy = json.dumps(
            {
                "policy": 14,
                "unit_max_retries": self.unit_max_retries,
                "review_mode": self.review_mode,
                "publish_partial": self.publish_partial,
                "source_reading": self.source_reading,
                "unit_target_chars": self.unit_target_chars,
                "source": hashlib.sha256(source.encode()).hexdigest(),
                "prompts": prompts,
                "semantic_verification": self.verify_semantics,
                "endpoint": self.provider_endpoint,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
                "coherence_mode": self.coherence_mode,
                "balanced_group_size": self.balanced_group_size,
                "context_chars": self.context_chars,
                "generation_signature": self.generation_signature,
                "verification_model": self.verification_model or self.model,
                "verification_temperature": self.verification_temperature,
                "verification_provider": (self.verification_provider or self.provider).name,
            },
            sort_keys=True,
        )
        return self.prompt_version + ":" + hashlib.sha256(policy.encode()).hexdigest()

    async def execute(
        self,
        chunks: list[Chunk],
        *,
        on_chunk_cleaned: ChunkCleanedCallback | None = None,
        cached_chunks: list[CleanedChunk] | None = None,
    ) -> list[CleanedChunk]:
        """Clean chunks, invoking on_chunk_cleaned after each completion."""
        reused = {item.chunk.id: item for item in cached_chunks or []}
        if self.coherence_mode == "fast":
            return await self._clean_parallel(chunks, on_chunk_cleaned, reused)
        if self.coherence_mode == "balanced":
            return await self._clean_balanced(chunks, on_chunk_cleaned, reused)
        if self.coherence_mode == "max-coherence":
            return await self._clean_sequential(chunks, on_chunk_cleaned, reused)
        raise ValueError(f"Unsupported coherence mode: {self.coherence_mode}")

    async def _clean_parallel(
        self,
        chunks: list[Chunk],
        on_chunk_cleaned: ChunkCleanedCallback | None,
        reused: dict[ChunkId, CleanedChunk],
    ) -> list[CleanedChunk]:
        if not chunks:
            return []
        worker_count = max(1, min(self.max_parallel_chunks, len(chunks)))
        queue: asyncio.Queue[Chunk] = asyncio.Queue()
        for chunk in chunks:
            if chunk.id not in reused:
                queue.put_nowait(chunk)
        results: list[CleanedChunk | None] = [reused.get(chunk.id) for chunk in chunks]
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
                    known_previous = results[index - 1] if index else None
                    result = await self._clean_one(
                        chunk,
                        previous_context=(
                            review_body(known_previous.text)[-self.context_chars :]
                            if known_previous
                            else ""
                            if self.plain_edits
                            else previous_context
                        ),
                        previous_source_context=previous_context,
                        following_source_context=(
                            chunk_separator_before(chunks[index + 1], self.document_source)
                            + chunks[index + 1].text[: self.context_chars]
                            if index + 1 < len(chunks)
                            else ""
                        ),
                    )
                    results[index] = result
                    if on_chunk_cleaned is not None:
                        await on_chunk_cleaned(result)
                finally:
                    queue.task_done()

        await _run_workers(worker, worker_count)
        return [item for item in results if item is not None]

    async def _clean_balanced(
        self,
        chunks: list[Chunk],
        on_chunk_cleaned: ChunkCleanedCallback | None,
        reused: dict[ChunkId, CleanedChunk],
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
                    previous_group = group_results[group_index - 1] if group_index else None
                    group_results[group_index] = await self._clean_sequential(
                        group,
                        on_chunk_cleaned,
                        reused,
                        initial_edited_context=(
                            review_body(previous_group[-1].text)[-self.context_chars :]
                            if previous_group
                            else ""
                        ),
                        following_source_context=(
                            chunk_separator_before(
                                chunks[(group_index + 1) * group_size], self.document_source
                            )
                            + chunks[(group_index + 1) * group_size].text[: self.context_chars]
                            if (group_index + 1) * group_size < len(chunks)
                            else ""
                        ),
                        initial_source_context=(
                            chunks[group_index * group_size - 1].text[-self.context_chars :]
                            if group_index
                            else ""
                        ),
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
        on_chunk_cleaned: ChunkCleanedCallback | None = None,
        reused: dict[ChunkId, CleanedChunk] | None = None,
        *,
        initial_source_context: str = "",
        initial_edited_context: str = "",
        following_source_context: str = "",
    ) -> list[CleanedChunk]:
        reused = reused or {}
        results: list[CleanedChunk] = []
        previous_context = initial_edited_context
        previous_source_context = initial_source_context
        for index, chunk in enumerate(chunks):
            cached = reused.get(chunk.id)
            result = cached or await self._clean_one(
                chunk,
                previous_context=previous_context,
                previous_source_context=previous_source_context,
                following_source_context=(
                    chunk_separator_before(chunks[index + 1], self.document_source)
                    + chunks[index + 1].text[: self.context_chars]
                    if index + 1 < len(chunks)
                    else following_source_context
                ),
            )
            results.append(result)
            previous_context = review_body(result.text)[-self.context_chars :]
            previous_source_context = chunk.text[-self.context_chars :]
            if cached is None and on_chunk_cleaned is not None:
                await on_chunk_cleaned(result)
        return results

    def _attempt_prompt(
        self,
        system_prompt: str,
        chunk: Chunk,
        attempt: int,
        warnings: tuple[str, ...],
        unit_failures: dict[str, list[str]],
        *,
        structured: bool,
    ) -> str:
        prompt = system_prompt
        if attempt:
            if self.plain_edits:
                return prompt + "\n\n" + self.prompt_catalog.get("repair", "prose_fidelity_v1")
            diagnostics: dict[str, object] = {
                "failed_checks": list(warnings),
                "unit_failures": unit_failures,
            }
            if structured:
                prompt += "\n\n" + self.prompt_catalog.get("repair", "unit_fidelity_v1")
            else:
                diagnostics["numeric_tokens_and_counts"] = dict(
                    Counter(VERBATIM_NUMBER_REGEX.findall(chunk.text))
                )
                diagnostics["source_word_count"] = len(re.findall(r"\b\w+\b", chunk.text))
                prompt += "\n\n" + self.prompt_catalog.get("repair", "fidelity_v1")
            prompt += "\n\nValidation diagnostics:\n" + json.dumps(diagnostics)
            if structured and attempt == max(0, self.max_fidelity_retries):
                prompt += (
                    "\n\nFinal fidelity repair: use a conservative copyedit of the original "
                    "unit. Keep its substantive wording and order; fix punctuation, grammar, "
                    "spelling, and pure stutters. Do not paraphrase or compress claims. "
                    "A previous candidate may contain invented relationships: the original "
                    "source, not that candidate, is authoritative. Preserve 'supposed to', "
                    "'seems', and other markers of the speaker's interpretation; do not "
                    "convert them into claims that an instruction or event actually occurred."
                )
        return prompt

    async def _report_wait(self, chunk: Chunk, number: int, budget: int, elapsed: float) -> None:
        if self.on_attempt is not None:
            await self.on_attempt(
                CleaningAttempt(chunk, number, budget, "waiting", elapsed_seconds=elapsed)
            )

    async def _clean_one(
        self,
        chunk: Chunk,
        *,
        previous_context: str,
        previous_source_context: str | None = None,
        following_source_context: str = "",
    ) -> CleanedChunk:
        system_prompt = self.prompt_catalog.get("cleaning", self.prompt_version)
        if self.editorial_context:
            system_prompt += (
                "\n\nUnverified spelling suggestions: check each against the source.\n"
                if self.plain_edits
                else "\n\nSource-grounded document spelling sheet:\n"
            ) + self.editorial_context
        units = list(self._source_units(chunk.text))
        saved_units = self.resume_units.get(chunk.id, ())
        # Only source-identical checkpoints can be reused, including the original
        # separators and any failed windows coalesced during the earlier run.
        original = "".join((u.separator if i else "") + u.text for i, u in enumerate(units))
        restored = "".join((u.separator if i else "") + u.source for i, u in enumerate(saved_units))
        if saved_units and original == restored:
            units = [EditUnit(u.unit_id, u.source, u.kind, u.separator) for u in saved_units]
        else:
            saved_units = ()
        preceding_separator = chunk_separator_before(chunk, self.document_source)
        has_units = bool(units)
        user_prompt = chunk.text
        if has_units:
            user_prompt = json.dumps(
                {"context": previous_context, "units": [unit.as_dict() for unit in units]},
                ensure_ascii=False,
            )
        elif previous_context:
            user_prompt = f"[CONTEXT: This continues from: {previous_context}]\n\n{chunk.text}"

        warnings: tuple[str, ...] = ()
        unit_failures: dict[str, list[str]] = {}
        unit_edits: dict[str, str] = {
            u.unit_id: u.accepted_text for u in saved_units if u.accepted_text is not None
        }
        if saved_units:
            cached_response = json.dumps(
                {"edits": [{"id": u.id, "text": unit_edits.get(u.id, u.text)} for u in units]}
            )
            _, _, checkpoint_failures = decode_edits(
                cached_response,
                units,
                context=SourceContext(
                    previous_source_context or previous_context, following_source_context
                ),
            )
            for invalid_id in checkpoint_failures:
                unit_edits.pop(invalid_id, None)
        frozen_edits = dict(unit_edits)
        frozen_ids = set(frozen_edits)
        advisory_issues: dict[str, list[str]] = {
            u.unit_id: list(u.advisory_reasons)
            for u in saved_units
            if u.advisory_reasons and u.unit_id in frozen_ids
        }
        generation_failures: dict[str, list[str]] = {}
        verified_units: dict[str, str] = {}
        attempts_used = 0
        unit_limit = (
            max(
                0,
                self.unit_max_retries
                if self.unit_max_retries is not None
                else self.max_fidelity_retries,
            )
            + 1
        )
        unit_attempts: dict[str, int] = {}
        chunk_limit = 2 * unit_limit if has_units else max(0, self.max_fidelity_retries) + 1

        prepare_attempt_prompt = partial(self._attempt_prompt, system_prompt, chunk)
        verify_units = self._verify_units
        progress_interval = self.progress_interval_seconds
        coalesce_limit = max(6000, min(12_000, self.unit_target_chars * 2))
        source_context = SourceContext(
            (previous_source_context or previous_context) + preceding_separator,
            following_source_context,
        )

        async def generate_attempt(
            prompt: str,
            attempt: int,
            attempt_warnings: tuple[str, ...],
            eligible_ids: set[str],
            repair_review_traces: list[str],
            limit: int,
        ) -> str:
            if has_units:
                operation = self._edit_units(
                    units,
                    (previous_source_context or previous_context)
                    if self.plain_edits
                    else previous_context,
                    prompt,
                    unit_edits,
                    unit_failures,
                    generation_failures,
                    repair_unit_chars=max(800, 4000 // (2**attempt)) if attempt else None,
                    patch_semantics=self.plain_edits
                    and bool(
                        {
                            "semantic-fidelity-failed",
                            "editorial-quality-failed",
                            "new-lowercase-paragraph",
                        }.intersection(attempt_warnings)
                    ),
                    previous_edited_context=previous_context,
                    repair_review_traces=repair_review_traces,
                    following_context=following_source_context,
                    preceding_separator=preceding_separator,
                    eligible_unit_ids=eligible_ids,
                )
            else:
                operation = self.provider.complete(
                    system_prompt=prompt,
                    user_prompt=user_prompt,
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                )

            note_wait = partial(self._report_wait, chunk, attempt + 1, limit)

            return await await_with_progress(
                operation,
                note_wait,
                interval_seconds=self.progress_interval_seconds,
            )

        for attempt in range(chunk_limit):
            attempt_warnings = cast("tuple[str, ...]", warnings)
            limit = chunk_limit
            if attempt and self.plain_edits and "editorial-quality-failed" in warnings:
                expanded, updated_edits, updated_failures = coalesce_adjacent_repairs(
                    units,
                    unit_edits,
                    unit_failures,
                    max_chars=coalesce_limit,
                )
                if len(expanded) != len(units):
                    units[:] = expanded
                    unit_edits.clear()
                    unit_edits.update(updated_edits)
                    unit_failures.clear()
                    unit_failures.update(updated_failures)
                    # Preserve approvals for untouched units. Content signatures still
                    # invalidate a review when its actual context has changed.
                    for key in list(verified_units):
                        if key not in {u.id for u in units}:
                            verified_units.pop(key)
                    retained = {unit.id for unit in units}
                    for key in list(generation_failures):
                        if key not in retained:
                            generation_failures.pop(key)
            eligible_ids = {
                u.id
                for u in units
                if u.id not in frozen_ids and unit_attempts.get(u.id, 0) < unit_limit
            }
            if attempt and has_units and not eligible_ids:
                break
            attempts_used = attempt + 1
            if self.on_attempt is not None:
                await self.on_attempt(CleaningAttempt(chunk, attempt + 1, limit, "started"))
            started = time.monotonic()
            for unit_id in eligible_ids:
                unit_attempts[unit_id] = unit_attempts.get(unit_id, 0) + 1
            prompt = prepare_attempt_prompt(
                attempt,
                cast("tuple[str, ...]", warnings),
                unit_failures,
                structured=has_units,
            )
            repair_review_traces: list[str] = []
            note_wait = partial(self._report_wait, chunk, attempt + 1, limit)
            try:
                raw = await generate_attempt(
                    prompt, attempt, attempt_warnings, eligible_ids, repair_review_traces, limit
                )
            except (ProcessingCanceled, LLMAccessError):
                raise
            except Exception as exc:  # noqa: BLE001 - identify the chunk without its source text
                if self.on_attempt is not None:
                    await self.on_attempt(
                        CleaningAttempt(
                            chunk,
                            attempt + 1,
                            limit,
                            "error",
                            (type(exc).__name__,),
                            elapsed_seconds=time.monotonic() - started,
                        )
                    )
                raise RuntimeError(
                    f"Cleaning chunk {chunk.ordinal + 1} failed: {sanitize_error_message(exc)}"
                ) from None
            if len(raw) > self.max_response_chars:
                raise ValueError(
                    "cleaning provider response exceeded configured character limit "
                    f"({len(raw)} > {self.max_response_chars})"
                )
            if repair_review_traces and self.on_attempt is not None:
                await self.on_attempt(
                    CleaningAttempt(
                        chunk,
                        attempt + 1,
                        limit,
                        "repair-reviewed",
                        text=json.dumps(repair_review_traces, ensure_ascii=False),
                        elapsed_seconds=time.monotonic() - started,
                    )
                )
            provider_text = raw
            unit_warnings: tuple[str, ...] = ()
            stripped_context = False
            if has_units:
                raw, unit_warnings, unit_failures = decode_edits(
                    raw,
                    units,
                    context=source_context,
                    frozen_edits=frozen_edits,
                )
                if "invalid-edit-structure" not in unit_warnings:
                    canonical = verification_pairs(
                        provider_text,
                        units,
                        include_unchanged_prose=True,
                        frozen_edits=frozen_edits,
                    )
                    unit_edits.update({pair["id"]: pair["edited"] for pair in canonical})
                unit_failures.update(generation_failures)
                unit_warnings = tuple(
                    dict.fromkeys(
                        (
                            *unit_warnings,
                            *(
                                warning
                                for values in generation_failures.values()
                                for warning in values
                            ),
                        )
                    )
                )
            else:
                raw, stripped_context = strip_repeated_context(raw, previous_context)
            validated = validate_cleaned_text(
                raw,
                input_size=len(chunk.text),
                source_text=chunk.text,
                context=source_context,
            )
            warnings = tuple(dict.fromkeys((*unit_warnings, *validated.warnings)))
            structural_failures = FIDELITY_WARNINGS.intersection(warnings)
            unassigned = structural_failures.difference(unit_warnings, {"substantial-content-loss"})
            # Global failures can occur at a join even when isolated units passed.
            # Retrying must perform real work rather than reconstruct the same draft.
            _route_document_failures(units, unit_failures, unassigned)
            if (
                has_units
                and self.verify_semantics
                and self.review_mode != "off"
                and "invalid-edit-structure" not in unit_warnings
            ):
                pairs = verification_pairs(
                    provider_text,
                    units,
                    include_unchanged_prose=self.plain_edits and self.review_mode == "strict",
                    frozen_edits=frozen_edits,
                )
                if self.plain_edits:
                    # Repairs must patch the same canonical text the reviewer audited.
                    unit_edits.update({pair["id"]: pair["edited"] for pair in pairs})
                    for pair in pairs:
                        if pair["id"] == units[0].id:
                            pair["context_before"] = (
                                previous_source_context
                                if previous_source_context is not None
                                else previous_context
                            )[-1200:]
                            pair["edited_context_before"] = (
                                (previous_context + preceding_separator)[-1200:]
                                if previous_context
                                else ""
                            )
                pairs = [
                    pair
                    for pair in pairs
                    if pair["id"] not in frozen_ids
                    and pair["id"] not in unit_failures
                    and (
                        self.review_mode == "strict"
                        or " ".join(pair["source"].split()) != " ".join(pair["edited"].split())
                    )
                ]
                for pair in pairs:
                    if pair["id"] == units[-1].id:
                        pair["context_after"] = following_source_context[:1200]
                if self.on_attempt is not None:
                    await self.on_attempt(
                        CleaningAttempt(
                            chunk,
                            attempt + 1,
                            limit,
                            "verifying",
                            text=provider_text,
                            elapsed_seconds=time.monotonic() - started,
                        )
                    )
                semantic_warning: str | None
                issues: dict[str, list[str]]
                verification: str
                semantic_warning, issues, verification = await await_with_progress(
                    verify_units(
                        pairs,
                        verified_units,
                        source_contexts={
                            u.id: unit_source_context(units, i, source_context)
                            for i, u in enumerate(units)
                        },
                    ),
                    note_wait,
                    interval_seconds=progress_interval,
                )
                if semantic_warning:
                    if self.review_mode == "advisory":
                        advisory_issues.update(issues)
                    else:
                        warnings = (*warnings, semantic_warning)
                        unit_failures.update(issues)
                elif not unit_failures:
                    # A word-count reduction alone is not proof of lost meaning. It is
                    # cleared only after complete unit coverage and an independent audit.
                    warnings = tuple(w for w in warnings if w != "substantial-content-loss")
                if self.on_attempt is not None:
                    await self.on_attempt(
                        CleaningAttempt(
                            chunk,
                            attempt + 1,
                            limit,
                            "verification-rejected" if semantic_warning else "verified",
                            (semantic_warning,) if semantic_warning else (),
                            verification,
                            time.monotonic() - started,
                        )
                    )
            if has_units and "invalid-edit-structure" not in warnings:
                newly_accepted: list[str] = []
                for unit in units:
                    if unit.id not in unit_failures and unit.id in unit_edits:
                        if unit.id not in frozen_edits:
                            newly_accepted.append(unit.id)
                        frozen_edits.setdefault(unit.id, unit_edits[unit.id])
                frozen_ids = set(frozen_edits)
                unit_edits.update(frozen_edits)
                if newly_accepted:
                    accepted_checkpoint = tuple(
                        UnitCheckpoint(
                            u.id,
                            u.text,
                            u.kind,
                            u.separator,
                            frozen_edits.get(u.id),
                            tuple(advisory_issues.get(u.id, [])),
                        )
                        for u in units
                    )
                    if self.on_unit_checkpoint is not None:
                        await self.on_unit_checkpoint(chunk, accepted_checkpoint)
                    if self.on_attempt is not None:
                        await self.on_attempt(
                            CleaningAttempt(
                                chunk,
                                attempt + 1,
                                limit,
                                "units-accepted",
                                text=json.dumps(
                                    {
                                        "units": [
                                            {
                                                "id": uid,
                                                "sha256": hashlib.sha256(
                                                    frozen_edits[uid].encode()
                                                ).hexdigest(),
                                                "attempts": unit_attempts.get(uid, 0),
                                            }
                                            for uid in newly_accepted
                                        ]
                                    }
                                ),
                            )
                        )
            blocking = FIDELITY_WARNINGS if self.review_mode == "strict" else DETERMINISTIC_WARNINGS
            accepted = not blocking.intersection(warnings)
            if self.on_attempt is not None:
                await self.on_attempt(
                    CleaningAttempt(
                        chunk,
                        attempt + 1,
                        limit,
                        "accepted" if accepted else "rejected",
                        warnings,
                        provider_text,
                        time.monotonic() - started,
                    )
                )
            if accepted:
                if advisory_issues:
                    parts: list[str] = []
                    checkpoints: list[UnitCheckpoint] = []
                    for unit in units:
                        edited = unit_edits.get(unit.id, unit.text)
                        reasons = advisory_issues.get(unit.id, [])
                        checkpoints.append(
                            UnitCheckpoint(
                                unit.id,
                                unit.text,
                                unit.kind,
                                unit.separator,
                                edited,
                                tuple(reasons),
                            )
                        )
                        text = (
                            mark_source(
                                edited,
                                unit_id=unit.id,
                                chunk=chunk.ordinal,
                                reasons=reasons,
                                attempts=attempts_used,
                                original_source=unit.text,
                            )
                            if reasons
                            else edited
                        )
                        parts.append((unit.separator if parts else "") + text)
                    return CleanedChunk(
                        chunk, "".join(parts), (*warnings, "review-advisory"), tuple(checkpoints)
                    )
                return CleanedChunk(
                    chunk=chunk,
                    text=validated.text,
                    warnings=(("repeated-context-stripped",) if stripped_context else ())
                    + warnings,
                )
            if "invalid-fidelity-verification" in warnings:
                # The reviewer has exhausted its own format budget. A new draft
                # cannot repair the reviewer's transport or response schema.
                break
        if self.publish_partial:
            # Invalid candidates never become fallback prose. Preserve the exact
            # source of unresolved units, while retaining independently passed edits.
            failures = unit_failures or {unit.id: list(warnings) for unit in units}
            if not units:
                return CleanedChunk(
                    chunk,
                    mark_source(
                        chunk.text,
                        unit_id="chunk",
                        chunk=chunk.ordinal,
                        reasons=warnings,
                        attempts=attempts_used,
                    ),
                    ("unit-needs-review",),
                )
            parts: list[str] = []
            checkpoint: list[UnitCheckpoint] = []
            for unit in units:
                reasons = failures.get(unit.id)
                if reasons or unit.id not in unit_edits:
                    text = mark_source(
                        unit.text,
                        unit_id=unit.id,
                        chunk=chunk.ordinal,
                        reasons=reasons or warnings,
                        attempts=unit_attempts.get(unit.id, attempts_used),
                    )
                else:
                    text = unit_edits[unit.id]
                accepted_text = unit_edits.get(unit.id) if not reasons else None
                checkpoint.append(
                    UnitCheckpoint(unit.id, unit.text, unit.kind, unit.separator, accepted_text)
                )
                parts.append((unit.separator if parts else "") + text)
            return CleanedChunk(chunk, "".join(parts), ("unit-needs-review",), tuple(checkpoint))
        raise FidelityValidationError(chunk, attempts_used, warnings)

    async def _edit_units(
        self,
        units: Sequence[EditUnit],
        previous_context: str,
        system_prompt: str,
        edits: dict[str, str],
        failures: dict[str, list[str]],
        generation_failures: dict[str, list[str]],
        *,
        repair_unit_chars: int | None = None,
        following_context: str = "",
        inherited_checks: tuple[str, ...] = (),
        patch_semantics: bool = False,
        previous_edited_context: str = "",
        repair_review_traces: list[str] | None = None,
        preceding_separator: str = "\n\n",
        eligible_unit_ids: set[str] | None = None,
    ) -> str:
        """Edit modest source spans independently and retry only rejected spans."""
        if self.plain_edits:
            for unit in units:
                if unit.kind in {"code", "table"}:
                    edits[unit.id] = unit.text
                    failures.pop(unit.id, None)
                    generation_failures.pop(unit.id, None)
        pending = [
            unit
            for unit in units
            if (unit.id not in edits or unit.id in failures)
            and (eligible_unit_ids is None or unit.id in eligible_unit_ids)
        ]
        queue: asyncio.Queue[EditUnit] = asyncio.Queue()
        for unit in pending:
            queue.put_nowait(unit)
        positions = {unit.id: index for index, unit in enumerate(units)}
        initial_edits = dict(edits)

        async def worker() -> None:
            while not queue.empty():
                unit = queue.get_nowait()
                index = positions[unit.id]
                before = units[index - 1].text[-1200:] if index else previous_context[-1200:]
                after = (
                    units[index + 1].text[:1200] if index + 1 < len(units) else following_context
                )
                if patch_semantics and unit.id in edits and unit.id in failures:
                    edited_before = (
                        (edits.get(units[index - 1].id, "") + unit.separator)[-1200:]
                        if index
                        else (previous_edited_context + preceding_separator)[-1200:]
                        if previous_edited_context
                        else ""
                    )
                    edited_after = (
                        (units[index + 1].separator + edits.get(units[index + 1].id, ""))[:1200]
                        if index + 1 < len(units)
                        else ""
                    )
                    if index and edits.get(units[index - 1].id) != initial_edits.get(
                        units[index - 1].id
                    ):
                        # Rejected checks describe an older neighbor snapshot. Refresh
                        # them before a dependent repair can undo the preceding fix.
                        warning, refreshed, trace = await self._verify_units(
                            [
                                {
                                    "id": unit.id,
                                    "source": unit.text,
                                    "edited": edits[unit.id],
                                    "context_before": before,
                                    "context_after": after,
                                    "edited_context_before": edited_before,
                                    "edited_context_after": edited_after,
                                }
                            ],
                            {},
                            source_contexts={
                                unit.id: unit_source_context(
                                    units,
                                    index,
                                    SourceContext(
                                        previous_context + preceding_separator, following_context
                                    ),
                                )
                            },
                        )
                        if repair_review_traces is not None:
                            repair_review_traces.append(trace)
                        if warning == "invalid-fidelity-verification":
                            generation_failures[unit.id] = [warning]
                            queue.task_done()
                            continue
                        if not warning:
                            failures.pop(unit.id, None)
                            generation_failures.pop(unit.id, None)
                            queue.task_done()
                            continue
                        failures[unit.id] = refreshed[unit.id]
                    repaired, problems = await self._repair_prose(
                        unit,
                        edits[unit.id],
                        failures[unit.id],
                        before,
                        after,
                        edited_before=edited_before,
                        edited_after=edited_after,
                        source_context=unit_source_context(
                            units,
                            index,
                            SourceContext(
                                previous_context + preceding_separator, following_context
                            ),
                        ),
                    )
                    edits[unit.id] = repaired
                    if problems:
                        generation_failures[unit.id] = list(problems)
                    else:
                        generation_failures.pop(unit.id, None)
                    queue.task_done()
                    continue
                if repair_unit_chars is not None and len(unit.text) > repair_unit_chars:
                    smaller = editing_units(
                        unit.text,
                        target_chars=repair_unit_chars,
                        pack_prose_blocks=self.plain_edits,
                    )
                    if len(smaller) > 1:
                        child_failures: dict[str, list[str]] = {}
                        child_response = await self._edit_units(
                            smaller,
                            before,
                            system_prompt,
                            {},
                            {},
                            child_failures,
                            following_context=after,
                            inherited_checks=(*inherited_checks, *failures.get(unit.id, [])),
                            preceding_separator=unit.separator if index else preceding_separator,
                        )
                        child_text, child_warnings, _ = decode_edits(child_response, smaller)
                        edits[unit.id] = child_text
                        if child_failures:
                            generation_failures[unit.id] = list(
                                dict.fromkeys(
                                    w for values in child_failures.values() for w in values
                                )
                            )
                        elif "invalid-edit-structure" in child_warnings:
                            generation_failures[unit.id] = ["invalid-edit-structure"]
                        else:
                            generation_failures.pop(unit.id, None)
                        queue.task_done()
                        continue
                try:
                    protection_warnings: tuple[str, ...] = ()
                    protection_context = unit_source_context(
                        units,
                        index,
                        SourceContext(previous_context + preceding_separator, following_context),
                    )
                    protected = protect_prose_data(unit.text, context=protection_context)
                    payload: dict[str, object] = {
                        "context_before": before,
                        "context_after": after,
                        "continuation_before": unit.separator == " "
                        or (index == 0 and bool(before)),
                        "continuation_after": (
                            (index + 1 < len(units) and units[index + 1].separator == " ")
                            or (index + 1 == len(units) and bool(following_context))
                        ),
                        "previous_candidate": edits.get(unit.id),
                        "repair_checks": [*inherited_checks, *failures.get(unit.id, [])],
                    }
                    if self.plain_edits:
                        payload["source"] = protected.text
                        payload["edited_context_before"] = (
                            (edits[units[index - 1].id] + unit.separator)[-1200:]
                            if index and units[index - 1].id in edits
                            else (previous_edited_context + preceding_separator)[-1200:]
                            if not index and previous_edited_context
                            else ""
                        )
                        payload["protected_values"] = dict(protected.values)
                        if protected.numeric_readings:
                            payload["numeric_readings"] = [
                                reading.as_dict() for reading in protected.numeric_readings
                            ]
                        if self.source_reading:
                            reading = await read_source(
                                self.verification_provider or self.provider,
                                self.prompt_catalog,
                                self.verification_model or self.model,
                                unit.text,
                                before,
                                after,
                                max_tokens=min(self.max_tokens, 8192),
                                temperature=self.verification_temperature,
                                prompt_version="source_reading_v2",
                            )
                            payload["source_reading"] = reading.model_dump()
                    else:
                        payload["units"] = [unit.as_dict()]
                    response = await self.provider.complete(
                        system_prompt=system_prompt,
                        user_prompt=json.dumps(payload, ensure_ascii=False),
                        model=self.model,
                        max_tokens=self.max_tokens,
                        temperature=self.temperature,
                    )
                except LLMResponseTruncatedError as exc:
                    edits[unit.id] = exc.partial_text[: self.max_response_chars]
                    generation_failures[unit.id] = ["truncated-model-output"]
                    queue.task_done()
                    continue
                if len(response) > self.max_response_chars:
                    raise ValueError(
                        "cleaning provider response exceeded configured character limit"
                    )
                if self.plain_edits:
                    response, protection_warnings = protected.restore(response)
                    response = json.dumps({"edits": [{"id": unit.id, "text": response}]})
                decoded, problems, _ = decode_edits(
                    response, [unit], context=SourceContext(before + " ", " " + after)
                )
                if "invalid-edit-structure" in problems:
                    edits[unit.id] = response
                    generation_failures[unit.id] = ["invalid-edit-structure"]
                else:
                    if self.plain_edits:
                        edits[unit.id] = decoded
                    else:
                        pairs = verification_pairs(response, [unit])
                        edits[unit.id] = pairs[0]["edited"] if pairs else unit.text
                    if protection_warnings:
                        generation_failures[unit.id] = list(protection_warnings)
                    else:
                        generation_failures.pop(unit.id, None)
                queue.task_done()

        if pending:
            # Repairs depend on the evolving neighbouring draft. Commit them in
            # source order so adjacent patches do not act on incompatible snapshots.
            workers = 1 if patch_semantics else min(len(pending), max(1, self.max_parallel_chunks))
            await _run_workers(worker, workers)
        return json.dumps(
            {"edits": [{"id": unit.id, "text": edits[unit.id]} for unit in units]},
            ensure_ascii=False,
        )

    async def _repair_prose(
        self,
        unit: EditUnit,
        candidate: str,
        checks: list[str],
        before: str,
        after: str,
        *,
        edited_before: str = "",
        edited_after: str = "",
        source_context: SourceContext | None = None,
    ) -> tuple[str, tuple[str, ...]]:
        protected = protect_prose_data(
            candidate,
            source_context=unit.text,
            context=source_context or SourceContext(before + " ", " " + after),
        )
        payload: dict[str, object] = {
            "source": unit.text,
            "candidate_spans": [
                {"id": span.id, "text": span.text} for span in candidate_spans(protected.text)
            ],
            "context_before": before,
            "context_after": after,
            "edited_context_before": edited_before,
            "edited_context_after": edited_after,
            "checks": checks,
            "protected_values": dict(protected.values),
            "numeric_readings": [reading.as_dict() for reading in protected.numeric_readings],
        }
        for _ in range(2):
            try:
                raw = await complete_json(
                    self.provider,
                    system_prompt=self.prompt_catalog.get("repair", "prose_patch_v1"),
                    user_prompt=json.dumps(payload, ensure_ascii=False),
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                )
            except LLMResponseTruncatedError:
                return candidate, ("truncated-model-output",)
            if len(raw) > self.max_response_chars:
                raise ValueError("prose patch response exceeded configured character limit")
            edited, problems = apply_prose_patches(protected.text, raw)
            restored, protection_warnings = protected.restore(edited)
            if not problems and not protection_warnings:
                return restored, ()
            payload["patch_validation_errors"] = [*problems, *protection_warnings]
            payload["previous_patch"] = raw
        return candidate, ("invalid-prose-patch",)

    async def _request_review(
        self,
        system_prompt: str,
        payload: dict[str, object],
        unit_id: str,
        *,
        confirmation_checks: Sequence[str] | None = None,
    ) -> tuple[str | None, dict[str, list[str]], str]:
        request = dict(payload)
        attempts: list[dict[str, object]] = []
        for _ in range(2):
            try:
                raw = await complete_json(
                    self.verification_provider or self.provider,
                    enabled=self.plain_edits,
                    system_prompt=system_prompt,
                    user_prompt=json.dumps(request, ensure_ascii=False),
                    model=self.verification_model or self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.verification_temperature,
                )
            except LLMResponseTruncatedError as exc:
                raw = exc.partial_text[: self.max_response_chars]
                warning, issues = "invalid-fidelity-verification", {}
                error = "truncated-review-response"
            else:
                if len(raw) > self.max_response_chars:
                    raise ValueError("Review response exceeded configured character limit")
                warning, issues = (
                    _confirmation_issues(raw, unit_id, confirmation_checks)
                    if confirmation_checks is not None
                    else _meaning_issues(raw, {unit_id})
                )
                error = "invalid-review-json"
            attempts.append(
                {
                    "response": raw,
                    "format_error": error if warning == "invalid-fidelity-verification" else None,
                }
            )
            if warning != "invalid-fidelity-verification":
                return warning, issues, json.dumps({"attempts": attempts}, ensure_ascii=False)
            # Schema repair never supplies a desired substantive verdict.
            request["review_format_error"] = error + (
                "; return exactly one checks entry for every original check ID, in order."
                if confirmation_checks is not None
                else "; return valid issues JSON with the original ID."
            )
            request["previous_review"] = raw[:8192]
        return (
            "invalid-fidelity-verification",
            {},
            json.dumps({"attempts": attempts}, ensure_ascii=False),
        )

    async def _verify_units(
        self,
        pairs: list[dict[str, str]],
        verified: dict[str, str],
        *,
        source_contexts: dict[str, SourceContext] | None = None,
    ) -> tuple[str | None, dict[str, list[str]], str]:
        queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        for original_pair in pairs:
            pair = dict(original_pair)
            if self.plain_edits:
                data = protect_prose_data(
                    pair["source"],
                    context=(source_contexts or {}).get(
                        pair["id"],
                        SourceContext(
                            pair.get("context_before", "") + " ",
                            " " + pair.get("context_after", ""),
                        ),
                    ),
                )
                if data.numeric_readings:
                    pair["numeric_readings"] = json.dumps(
                        [reading.as_dict() for reading in data.numeric_readings]
                    )
                if data.values:
                    pair["immutable_source_values"] = json.dumps(
                        dict(Counter(value for _, value in data.values))
                    )
            signature = hashlib.sha256(json.dumps(pair, sort_keys=True).encode()).hexdigest()
            if verified.get(pair["id"]) != signature:
                queue.put_nowait(pair)
        count = queue.qsize()
        failures: dict[str, list[str]] = {}
        responses: list[dict[str, str]] = []
        invalid = False

        async def worker() -> None:
            nonlocal invalid
            while not queue.empty():
                pair = queue.get_nowait()
                warning, issues, response = await self._request_review(
                    system_prompt=(
                        self.prompt_catalog.get(
                            "repair", "prose_review_v8" if self.plain_edits else "meaning_v1"
                        )
                        + (
                            ""
                            if self.plain_edits
                            else (
                                "\n\nApproved spelling corrections; these do not change identity:\n"
                            )
                            + self.editorial_context
                        )
                    ),
                    payload={"pairs": [pair]},
                    unit_id=pair["id"],
                )
                record = {"id": pair["id"], "response": response}
                if (
                    issues
                    and warning != "invalid-fidelity-verification"
                    and self.review_mode == "strict"
                ):
                    confirmed_warning, confirmed, confirmation = await self._request_review(
                        system_prompt=(
                            self.prompt_catalog.get(
                                "repair",
                                "confirm_prose_v8" if self.plain_edits else "confirm_meaning_v1",
                            )
                            + (
                                ""
                                if self.plain_edits
                                else "\n\nApproved spelling sheet:\n" + self.editorial_context
                            )
                        ),
                        payload=(
                            {
                                "pair": pair,
                                "checks": [
                                    {"id": f"c{index}", "problem": problem}
                                    for index, problem in enumerate(issues[pair["id"]])
                                ],
                            }
                            if self.plain_edits
                            else {"pair": pair, "proposed_issues": issues[pair["id"]]}
                        ),
                        unit_id=pair["id"],
                        confirmation_checks=issues[pair["id"]] if self.plain_edits else None,
                    )
                    record["confirmation"] = confirmation
                    if confirmed_warning == "invalid-fidelity-verification":
                        warning = confirmed_warning
                    else:
                        warning, issues = confirmed_warning, confirmed
                responses.append(record)
                if warning == "invalid-fidelity-verification":
                    invalid = True
                    failures[pair["id"]] = (
                        ["review-unavailable"]
                        if self.review_mode == "advisory"
                        else issues.get(
                            pair["id"],
                            ["Return a valid issues JSON object using the unchanged unit ID."],
                        )
                    )
                elif issues:
                    failures.update(issues)
                else:
                    verified[pair["id"]] = hashlib.sha256(
                        json.dumps(pair, sort_keys=True).encode()
                    ).hexdigest()
                queue.task_done()

        if count:
            await _run_workers(worker, min(count, max(1, self.max_parallel_chunks)))
        warning = (
            "invalid-fidelity-verification"
            if invalid
            else (
                ("editorial-quality-failed" if self.plain_edits else "semantic-fidelity-failed")
                if failures
                else None
            )
        )
        return warning, failures, json.dumps({"reviews": responses}, ensure_ascii=False)

    def revalidate(self, cleaned: CleanedChunk) -> CleanedChunk:
        """Apply current output and fidelity rules to a fresh or cached chunk.

        Cached provider output can outlive validation-policy changes. Running
        it through the current guardrail prevents a stale cache entry from
        bypassing a newly added fidelity check.
        """
        validated = validate_cleaned_text(
            cleaned.text,
            input_size=len(cleaned.chunk.text),
            source_text=cleaned.chunk.text,
            context=SourceContext(
                self.document_source[
                    max(0, cleaned.chunk.start_char - self.context_chars) : cleaned.chunk.start_char
                ],
                self.document_source[
                    cleaned.chunk.end_char : cleaned.chunk.end_char + self.context_chars
                ],
            )
            if self.document_source is not None
            else None,
        )
        validation_warnings = validated.warnings
        if self.source_aligned and self.verify_semantics:
            # Only current policy-namespaced, audited edits reach this cache path.
            validation_warnings = tuple(
                w for w in validation_warnings if w != "substantial-content-loss"
            )
        warnings = list(dict.fromkeys((*cleaned.warnings, *validation_warnings)))
        if FIDELITY_WARNINGS.intersection(warnings):
            warnings.append("source-preserved-after-fidelity-check")
            return CleanedChunk(
                chunk=cleaned.chunk,
                text=cleaned.chunk.text.strip(),
                warnings=tuple(dict.fromkeys(warnings)),
            )
        return CleanedChunk(
            chunk=cleaned.chunk,
            text=validated.text,
            warnings=tuple(dict.fromkeys(warnings)),
        )


def _meaning_issues(raw: str, ids: set[str]) -> tuple[str | None, dict[str, list[str]]]:
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]+?)\s*```", raw.strip(), re.I)
    if fenced:
        raw = fenced.group(1)
    try:
        value = json.loads(raw)
    except ValueError:
        return "invalid-fidelity-verification", {}
    if not isinstance(value, dict) or set(cast("dict[str, object]", value)) != {"issues"}:
        return "invalid-fidelity-verification", {}
    issues = cast("dict[str, object]", value)["issues"]
    if not isinstance(issues, list):
        return "invalid-fidelity-verification", {}
    problems: dict[str, list[str]] = {}
    for issue in cast("list[object]", issues):
        if not isinstance(issue, dict):
            return "invalid-fidelity-verification", {}
        entry = cast("dict[str, object]", issue)
        unit_id, problem = entry.get("id"), entry.get("problem")
        if isinstance(unit_id, str) and unit_id not in ids and len(ids) == 1:
            only_id = next(iter(ids))
            if unit_id.startswith(only_id + "-") and unit_id[len(only_id) + 1 :].isdigit():
                unit_id = only_id
        if not isinstance(unit_id, str) or unit_id not in ids or not isinstance(problem, str):
            return "invalid-fidelity-verification", {}
        if not problem.strip():
            return "invalid-fidelity-verification", {}
        problems.setdefault(unit_id, []).append(problem[:1000])
    return ("semantic-fidelity-failed" if problems else None), problems


def _confirmation_issues(
    raw: str, unit_id: str, proposed: Sequence[str]
) -> tuple[str | None, dict[str, list[str]]]:
    """Require an explicit decision for every check, including rejected ones."""
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]+?)\s*```", raw.strip(), re.I)
    if fenced:
        raw = fenced.group(1)
    try:
        value = json.loads(raw)
    except ValueError:
        return "invalid-fidelity-verification", {}
    if not isinstance(value, dict) or set(cast("dict[str, object]", value)) != {"checks"}:
        return "invalid-fidelity-verification", {}
    checks = cast("dict[str, object]", value)["checks"]
    if not isinstance(checks, list) or len(cast("list[object]", checks)) != len(proposed):
        return "invalid-fidelity-verification", {}
    confirmed: list[str] = []
    for index, item in enumerate(cast("list[object]", checks)):
        if not isinstance(item, dict):
            return "invalid-fidelity-verification", {}
        entry = cast("dict[str, object]", item)
        reason = entry.get("reason")
        if (
            set(entry) != {"id", "confirmed", "reason"}
            or entry.get("id") != f"c{index}"
            or not isinstance(entry.get("confirmed"), bool)
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            return "invalid-fidelity-verification", {}
        if entry["confirmed"]:
            confirmed.append(reason[:1000])
    return ("semantic-fidelity-failed", {unit_id: confirmed}) if confirmed else (None, {})
