"""Application service for chunk cleaning."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal, cast

from librarian.application.async_progress import await_with_progress
from librarian.application.errors import ProcessingCanceled
from librarian.application.ports import LLMProvider
from librarian.domain.models import Chunk
from librarian.observability import sanitize_error_message
from librarian.pipeline.editing import EditUnit, decode_edits, editing_units, verification_pairs
from librarian.pipeline.validation import VERBATIM_NUMBER_REGEX, validate_cleaned_text
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

    @property
    def fidelity_failed(self) -> bool:
        return bool(_FIDELITY_WARNINGS.intersection(self.warnings)) or (
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
        "accepted",
        "rejected",
        "error",
    ]
    warnings: tuple[str, ...] = ()
    text: str | None = None
    elapsed_seconds: float = 0.0


AttemptCallback = Callable[[CleaningAttempt], Awaitable[None]]

_FIDELITY_WARNINGS = frozenset(
    {
        "added-verbatim-number",
        "collapsed-paragraphs",
        "context-marker-leak",
        "changed-markdown-images",
        "changed-page-breaks",
        "empty-after-artifact-filter",
        "empty-output",
        "malformed-markdown-table",
        "missing-citation-marker",
        "missing-markdown-heading",
        "missing-markdown-list",
        "missing-markdown-table",
        "missing-verbatim-number",
        "repeated-tail",
        "substantial-content-loss",
        "suspiciously-short-output",
        "invalid-edit-structure",
        "changed-code-block",
        "changed-table-shape",
        "changed-table-values",
        "semantic-fidelity-failed",
        "invalid-fidelity-verification",
    }
)
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
    on_attempt: AttemptCallback | None = None
    verify_semantics: bool = True
    editorial_context: str = ""
    progress_interval_seconds: float = 10.0
    # Trailing characters of the preceding chunk handed to the cleaner as
    # read-only continuity context. Chunks no longer overlap in the source, so
    # this replaces the (previously duplicated) overlap for boundary coherence.
    context_chars: int = 800

    async def execute(
        self,
        chunks: list[Chunk],
        *,
        on_chunk_cleaned: ChunkCleanedCallback | None = None,
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
        on_chunk_cleaned: ChunkCleanedCallback | None,
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
                    result = await self._clean_one(
                        chunk,
                        previous_context=previous_context,
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
        on_chunk_cleaned: ChunkCleanedCallback | None = None,
    ) -> list[CleanedChunk]:
        results: list[CleanedChunk] = []
        previous_context = ""
        for chunk in chunks:
            result = await self._clean_one(chunk, previous_context=previous_context)
            results.append(result)
            previous_context = result.text[-self.context_chars :]
            if on_chunk_cleaned is not None:
                await on_chunk_cleaned(result)
        return results

    async def _clean_one(self, chunk: Chunk, *, previous_context: str) -> CleanedChunk:
        system_prompt = self.prompt_catalog.get("cleaning", self.prompt_version)
        if self.editorial_context:
            system_prompt += (
                "\n\nSource-grounded document spelling sheet:\n" + self.editorial_context
            )
        units: list[EditUnit] = (
            editing_units(chunk.text) if self.prompt_version == "cmos_v6" else []
        )
        user_prompt = chunk.text
        if units:
            user_prompt = json.dumps(
                {"context": previous_context, "units": [unit.as_dict() for unit in units]},
                ensure_ascii=False,
            )
        elif previous_context:
            user_prompt = f"[CONTEXT: This continues from: {previous_context}]\n\n{chunk.text}"

        warnings: tuple[str, ...] = ()
        unit_failures: dict[str, list[str]] = {}
        unit_edits: dict[str, str] = {}
        generation_failures: dict[str, list[str]] = {}
        verified_units: dict[str, str] = {}
        for attempt in range(max(0, self.max_fidelity_retries) + 1):
            limit = max(0, self.max_fidelity_retries) + 1
            if self.on_attempt is not None:
                await self.on_attempt(CleaningAttempt(chunk, attempt + 1, limit, "started"))
            started = time.monotonic()
            prompt = system_prompt
            if attempt:
                diagnostics: dict[str, object] = {
                    "failed_checks": list(warnings),
                    "unit_failures": unit_failures,
                }
                if units:
                    prompt += "\n\n" + self.prompt_catalog.get("repair", "unit_fidelity_v1")
                else:
                    diagnostics["numeric_tokens_and_counts"] = dict(
                        Counter(VERBATIM_NUMBER_REGEX.findall(chunk.text))
                    )
                    diagnostics["source_word_count"] = len(re.findall(r"\b\w+\b", chunk.text))
                    prompt += "\n\n" + self.prompt_catalog.get("repair", "fidelity_v1")
                prompt += "\n\nValidation diagnostics:\n" + json.dumps(diagnostics)
            try:
                if units:
                    operation = self._edit_units(
                        units,
                        previous_context,
                        prompt,
                        unit_edits,
                        unit_failures,
                        generation_failures,
                    )
                else:
                    operation = self.provider.complete(
                        system_prompt=prompt,
                        user_prompt=user_prompt,
                        model=self.model,
                        max_tokens=self.max_tokens,
                        temperature=self.temperature,
                    )

                async def note_wait(
                    elapsed: float,
                    number: int = attempt + 1,
                    budget: int = limit,
                ) -> None:
                    if self.on_attempt is not None:
                        await self.on_attempt(
                            CleaningAttempt(
                                chunk,
                                number,
                                budget,
                                "waiting",
                                elapsed_seconds=elapsed,
                            )
                        )

                raw = await await_with_progress(
                    operation,
                    note_wait,
                    interval_seconds=self.progress_interval_seconds,
                )
            except ProcessingCanceled:
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
            provider_text = raw
            unit_warnings: tuple[str, ...] = ()
            stripped_context = False
            if units:
                raw, unit_warnings, unit_failures = decode_edits(raw, units)
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
                raw, input_size=len(chunk.text), source_text=chunk.text
            )
            warnings = tuple(dict.fromkeys((*unit_warnings, *validated.warnings)))
            structural_failures = _FIDELITY_WARNINGS.intersection(warnings)
            if (
                units
                and self.verify_semantics
                and not (structural_failures - {"substantial-content-loss"})
            ):
                pairs = verification_pairs(provider_text, units)
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
                semantic_warning, issues, verification = await await_with_progress(
                    self._verify_units(
                        pairs,
                        verified_units,
                    ),
                    note_wait,
                    interval_seconds=self.progress_interval_seconds,
                )
                if semantic_warning:
                    warnings = (*warnings, semantic_warning)
                    unit_failures = issues
                else:
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
            accepted = not _FIDELITY_WARNINGS.intersection(warnings)
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
                return CleanedChunk(
                    chunk=chunk,
                    text=validated.text,
                    warnings=(("repeated-context-stripped",) if stripped_context else ())
                    + warnings,
                )
        raise FidelityValidationError(chunk, max(0, self.max_fidelity_retries) + 1, warnings)

    async def _edit_units(
        self,
        units: list[EditUnit],
        previous_context: str,
        system_prompt: str,
        edits: dict[str, str],
        failures: dict[str, list[str]],
        generation_failures: dict[str, list[str]],
    ) -> str:
        """Edit modest source spans independently and retry only rejected spans."""
        pending = [unit for unit in units if unit.id not in edits or unit.id in failures]
        queue: asyncio.Queue[EditUnit] = asyncio.Queue()
        for unit in pending:
            queue.put_nowait(unit)
        positions = {unit.id: index for index, unit in enumerate(units)}

        async def worker() -> None:
            while not queue.empty():
                unit = queue.get_nowait()
                index = positions[unit.id]
                before = units[index - 1].text[-1200:] if index else previous_context[-1200:]
                after = units[index + 1].text[:1200] if index + 1 < len(units) else ""
                response = await self.provider.complete(
                    system_prompt=system_prompt,
                    user_prompt=json.dumps(
                        {
                            "context_before": before,
                            "context_after": after,
                            "continuation_before": unit.separator == " "
                            or (index == 0 and bool(before)),
                            "continuation_after": (
                                index + 1 < len(units) and units[index + 1].separator == " "
                            ),
                            "units": [unit.as_dict()],
                            "previous_candidate": edits.get(unit.id),
                            "repair_checks": failures.get(unit.id, []),
                        },
                        ensure_ascii=False,
                    ),
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                )
                if len(response) > self.max_response_chars:
                    raise ValueError(
                        "cleaning provider response exceeded configured character limit"
                    )
                _, problems, _ = decode_edits(response, [unit])
                if "invalid-edit-structure" in problems:
                    edits[unit.id] = response
                    generation_failures[unit.id] = ["invalid-edit-structure"]
                else:
                    pairs = verification_pairs(response, [unit])
                    edits[unit.id] = pairs[0]["edited"] if pairs else unit.text
                    generation_failures.pop(unit.id, None)
                queue.task_done()

        if pending:
            await _run_workers(worker, min(len(pending), max(1, self.max_parallel_chunks)))
        return json.dumps(
            {"edits": [{"id": unit.id, "text": edits[unit.id]} for unit in units]},
            ensure_ascii=False,
        )

    async def _verify_units(
        self,
        pairs: list[dict[str, str]],
        verified: dict[str, str],
    ) -> tuple[str | None, dict[str, list[str]], str]:
        queue: asyncio.Queue[dict[str, str]] = asyncio.Queue()
        for pair in pairs:
            if verified.get(pair["id"]) != pair["edited"]:
                queue.put_nowait(pair)
        count = queue.qsize()
        failures: dict[str, list[str]] = {}
        responses: list[dict[str, str]] = []
        invalid = False

        async def worker() -> None:
            nonlocal invalid
            while not queue.empty():
                pair = queue.get_nowait()
                response = await self.provider.complete(
                    system_prompt=(
                        self.prompt_catalog.get("repair", "meaning_v1")
                        + "\n\nApproved spelling corrections; these do not change identity:\n"
                        + self.editorial_context
                    ),
                    user_prompt=json.dumps({"pairs": [pair]}, ensure_ascii=False),
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=0.0,
                )
                warning, issues = _meaning_issues(response, {pair["id"]})
                record = {"id": pair["id"], "response": response}
                if issues and warning != "invalid-fidelity-verification":
                    confirmation = await self.provider.complete(
                        system_prompt=(
                            self.prompt_catalog.get("repair", "confirm_meaning_v1")
                            + "\n\nApproved spelling sheet:\n"
                            + self.editorial_context
                        ),
                        user_prompt=json.dumps(
                            {
                                "pair": pair,
                                "proposed_issues": issues[pair["id"]],
                            },
                            ensure_ascii=False,
                        ),
                        model=self.model,
                        max_tokens=self.max_tokens,
                        temperature=0.0,
                    )
                    confirmed_warning, confirmed = _meaning_issues(confirmation, {pair["id"]})
                    record["confirmation"] = confirmation
                    if confirmed_warning == "invalid-fidelity-verification":
                        warning = confirmed_warning
                    else:
                        warning, issues = confirmed_warning, confirmed
                responses.append(record)
                if warning == "invalid-fidelity-verification":
                    invalid = True
                    failures[pair["id"]] = issues.get(
                        pair["id"],
                        ["Return a valid issues JSON object using the unchanged unit ID."],
                    )
                elif issues:
                    failures.update(issues)
                else:
                    verified[pair["id"]] = pair["edited"]
                queue.task_done()

        if count:
            await _run_workers(worker, min(count, max(1, self.max_parallel_chunks)))
        warning = (
            "invalid-fidelity-verification"
            if invalid
            else ("semantic-fidelity-failed" if failures else None)
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
        )
        warnings = list(dict.fromkeys((*cleaned.warnings, *validated.warnings)))
        if _FIDELITY_WARNINGS.intersection(warnings):
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
