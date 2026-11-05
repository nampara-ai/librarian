"""Candidate risks that require a focused decision, never automatic word deletion."""

from __future__ import annotations

import json
import re

from librarian.pipeline.markdown import protected_spans
from librarian.pipeline.validation import UNCLEAR_TRANSCRIPTION_REGEX

EDITORIAL_AUDIT = "editorial-risks-v1"
EDITORIAL_AUDIT_MARKER = "editorial-risks-audited-v1"
_REPAIR = re.compile(
    r"\bor\s+(?:rather\b|not\b)"
    r"|\b(?:no|sorry|actually)[,;:— -]+(?:I\s+mean|rather|make\s+that)\b"
    r"|\bI\s+mean\b|\blet\s+me\s+(?:correct|rephrase)\b|\bscratch\s+that\b",
    re.I,
)
_BACKTICKS = re.compile(r"`+")
_QUOTES = re.compile(r'"[^"\n]+"|“[^”]+”|‘[^’]+’')
_ALTERNATIVE = re.compile(r",\s+or\s+(?:I|we|you|he|she|they)\b", re.I)
_COMMA_SPACE = re.compile(r"(?<=\S)[ \t]+,")


def editorial_checks(candidate: str, source: str = "") -> list[str]:
    """Require explicit scrutiny of repair cues without treating them as defects.

    Literal code and unchanged authored quotations are not abandoned narration.
    Deliberate alternatives, uncertainty, and humor still require a negative vote.
    """
    excluded = [
        *protected_spans(candidate),
        *(m.span() for m in UNCLEAR_TRANSCRIPTION_REGEX.finditer(candidate)),
    ]
    cursor = 0
    while marker := _BACKTICKS.search(candidate, cursor):
        closing = candidate.find(marker[0], marker.end())
        if closing < 0:
            excluded.append((marker.start(), len(candidate)))
            break
        cursor = closing + len(marker[0])
        excluded.append((marker.start(), cursor))
    for quote in _QUOTES.finditer(source):
        start = candidate.find(quote[0])
        if start >= 0:
            excluded.append((start, start + len(quote[0])))
    checks: list[str] = []
    for pattern, instruction in (
        (
            _REPAIR,
            "Resolve an abandoned speech repair to the final intended reading. "
            "Reject this proposal "
            "if the excerpt expresses a genuine alternative, uncertainty, "
            "deliberate rhetoric or humor, "
            "an authored quotation, or a linguistic example. "
            "Preserve those meanings and the author’s voice.",
        ),
        (
            _ALTERNATIVE,
            "Repair a grammatically broken alternative or aside join in this excerpt. "
            "Retain meaningful direct address, uncertainty, and the author's voice. "
            "Reject this proposal if the existing join is grammatical, a genuine alternative, "
            "an authored quotation, or an example being discussed.",
        ),
        (
            _COMMA_SPACE,
            "Remove unintended whitespace before the comma. Reject this proposal if that "
            "spacing belongs to literal code, mathematics, an identifier, "
            "or an example being discussed.",
        ),
    ):
        for match in pattern.finditer(candidate):
            if any(left <= match.start() < right for left, right in excluded):
                continue
            excerpt = candidate[max(0, match.start() - 90) : match.end() + 110]
            check = instruction + "\nCandidate excerpt: " + json.dumps(excerpt, ensure_ascii=False)
            if check not in checks:
                checks.append(check)
            if len(checks) == 4:
                return checks
    return checks
