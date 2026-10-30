"""A source-grounded spelling sheet shared by editors and fidelity auditors."""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import cast

from librarian.application.ports import LLMProvider
from librarian.pipeline.validation import VERBATIM_NUMBER_REGEX
from librarian.prompts import PromptCatalog


async def editorial_context(
    provider: LLMProvider,
    catalog: PromptCatalog,
    model: str,
    source: str,
) -> str:
    snippets = [source[:1200]]
    seen: set[str] = set()
    for match in re.finditer(r"\b[A-Z][A-Za-z'-]{2,}(?:\s+[A-Z][A-Za-z'-]{1,}){0,3}\b", source):
        name = match.group().casefold()
        if name in seen:
            continue
        seen.add(name)
        snippets.append(source[max(0, match.start() - 100) : match.end() + 160])
        if sum(len(item) for item in snippets) >= 10_000:
            break
    sample = "\n[... ]\n".join(snippets)
    raw = await provider.complete(
        system_prompt=catalog.get("repair", "context_v1"),
        user_prompt=sample,
        model=model,
        max_tokens=16_384,
        temperature=0.0,
    )
    return parse_editorial_context(raw, source)


def parse_editorial_context(raw: str, source: str) -> str:
    if len(raw) > 64_000:
        raise ValueError("Editorial context exceeded its response limit")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise ValueError("Editorial context returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Editorial context must be a JSON object")
    entries = cast("dict[str, object]", payload).get("corrections")
    if not isinstance(entries, list):
        raise ValueError("Editorial context must contain a corrections list")
    accepted: list[dict[str, str]] = []
    for entry in cast("list[object]", entries)[:64]:
        if not isinstance(entry, dict):
            continue
        correction = cast("dict[str, object]", entry)
        observed, preferred = correction.get("observed"), correction.get("preferred")
        if not isinstance(observed, str) or not isinstance(preferred, str):
            continue
        a, b = observed.casefold().strip(), preferred.casefold().strip()
        if not a or not b or len(a) > 100 or len(b) > 100 or a not in source.casefold():
            continue
        if VERBATIM_NUMBER_REGEX.findall(observed) != VERBATIM_NUMBER_REGEX.findall(preferred):
            continue
        if a[0] != b[0] or SequenceMatcher(None, a, b).ratio() < 0.6:
            continue
        accepted.append({"observed": observed, "preferred": preferred})
    return json.dumps({"approved_spelling_corrections": accepted}, ensure_ascii=False)
