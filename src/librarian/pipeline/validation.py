"""Output validation and LLM artifact filtering."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from librarian.pipeline.markdown import (
    normalize_markdown_whitespace,
    protected_spans,
    transform_prose,
)

FIDELITY_WARNINGS = frozenset(
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
        "new-adjacent-repetition",
        "new-lowercase-paragraph",
        "substantial-content-loss",
        "suspiciously-short-output",
        "invalid-edit-structure",
        "truncated-model-output",
        "changed-protected-placeholder",
        "unknown-protected-placeholder",
        "invalid-prose-patch",
        "unchanged-unclean-prose",
        "changed-code-block",
        "changed-table-shape",
        "changed-table-values",
        "semantic-fidelity-failed",
        "editorial-quality-failed",
        "changed-literal-link",
        "invalid-fidelity-verification",
        "model-elided-text",
    }
)

# Review findings are fallible semantic judgments, distinct from structural guards.
ADVISORY_WARNINGS = frozenset(
    {"semantic-fidelity-failed", "editorial-quality-failed", "invalid-fidelity-verification"}
)
DETERMINISTIC_WARNINGS = FIDELITY_WARNINGS - ADVISORY_WARNINGS

ARTIFACT_PATTERNS = [
    r"I am ready to (?:begin|process)",
    r"I'm ready to (?:begin|process|edit)",
    r"Please provide (?:the|your) (?:raw )?transcript",
    r"\[The assistant(?:'s response)? (?:would|will|is)",
    r"\[Omitted long matching line\]",
    r"(?:Here is|I have) (?:the )?cleaned (?:the )?(?:text|transcript)",
    r"As (?:an AI|a language model)",
    r"I don't have access to",
    r"I cannot (?:see|access|read)",
    r"(?:The )?(?:input|text) (?:provided )?(?:is|was) (?:empty|blank)",
    r"there is no (?:text|content|input) to",
]

ARTIFACT_REGEX = re.compile("|".join(ARTIFACT_PATTERNS), re.IGNORECASE)
CONTEXT_MARKER_REGEX = re.compile(
    r"\[(?:CONTEXT:|CONTINUING FROM:|previous context|next context)",
    re.IGNORECASE,
)
MARKDOWN_HEADING_REGEX = re.compile(r"(?m)^#{1,6}\s+\S")
MARKDOWN_LIST_REGEX = re.compile(r"(?m)^\s*(?:[-*+]\s+|\d+[.)]\s+)\S")
ORPHAN_LIST_MARKER_REGEX = re.compile(r"(?m)^\s*(?:[-*+]|\d+[.)])\s*$")
MARKDOWN_TABLE_SEPARATOR_REGEX = re.compile(
    r"(?m)^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
MARKDOWN_TABLE_ROW_REGEX = re.compile(r"(?m)^\s*\|.+\|\s*$")
LOCAL_IMAGE_REFERENCE_REGEX = re.compile(
    r"!\[[^\]]*\]\((?![a-z][a-z0-9+.-]*:|/|#)([^)\s]+)\)", re.IGNORECASE
)
PAGE_BREAK_REGEX = re.compile(r"<!--\s*page-break\s*-->", re.IGNORECASE)
ALL_IMAGE_REFERENCE_REGEX = re.compile(r"!\[[^\]]*\]\(([^)\n]+)\)")
UNCLEAR_TRANSCRIPTION_REGEX = re.compile(r"\[transcription unclear:\s*([^\]]+)\]", re.IGNORECASE)
VERBATIM_NUMBER_REGEX = re.compile(
    r"(?<=\b[vV])\d+(?:[.,]\d+)+|(?<!\w)\d+(?:[.,]\d+)*|(?<!\d)\d+\b"
)
LITERAL_LINK_REGEX = re.compile(
    r"(?<![\w@.-])(?:https?://|www\.)[^\s<>()\[\]]+"
    r"|(?<![\w@.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}(?:/[^\s<>()\[\]]*)?"
)


def literal_links(text: str) -> list[str]:
    return [m.group().rstrip(".,!?;:\"'’”") for m in LITERAL_LINK_REGEX.finditer(text)]


REPEATED_TAIL_MIN_PATTERN_CHARS = 12
REPEATED_TAIL_MAX_PATTERN_CHARS = 240
REPEATED_TAIL_MIN_REPEATS = 8

# Content retention is measured over "content words": alphabetic tokens of at
# least four letters that are not conversational filler. Removing filler,
# stutters, and false starts is legitimate copyediting of a transcript and must
# never read as lost content, while dropping a sentence always does.
CONTENT_WORD_REGEX = re.compile(r"[^\W\d_]{4,}")
FILLER_WORDS = frozenset(
    {
        "actually",
        "alright",
        "anyway",
        "anyways",
        "basically",
        "gonna",
        "gotta",
        "hmmm",
        "just",
        "kind",
        "kinda",
        "know",
        "like",
        "literally",
        "mean",
        "okay",
        "really",
        "right",
        "sort",
        "sorta",
        "umm",
        "ummm",
        "wanna",
        "well",
        "yeah",
        "yknow",
    }
)
# Below this share of surviving source content words a candidate has lost
# meaning, whatever a reviewer says about it. Ten percent covers spelling and
# inflection repairs; a dropped sentence in a short unit costs far more.
MIN_CONTENT_RETENTION = 0.9
# Units with fewer content words than this are too short for the ratio to be
# meaningful; the other guards (empty output, numbers, links) still apply.
MIN_CONTENT_WORDS_FOR_RETENTION = 10
# At most this share of a unit's content words may be explained as word
# substitutions (``utilize`` -> ``use``); beyond it, new words are padding.
MAX_SUBSTITUTION_SHARE = 0.2
_STEM_CHARS = 5
ELLIPSIS_REGEX = re.compile(r"…|(?<!\.)\.(?:\s?\.){2}(?!\.)")


def content_words(text: str) -> Counter[str]:
    """Case-folded multiset of the text's content words (filler excluded)."""
    return Counter(
        word
        for word in (match.group().casefold() for match in CONTENT_WORD_REGEX.finditer(text))
        if word not in FILLER_WORDS
    )


def content_retention(source: str, candidate: str) -> float | None:
    """Share of the source's content words that survive in the candidate.

    A word survives when it appears verbatim, when the candidate holds a word
    sharing its first five letters (``transitions`` -> ``transition``), or
    when the candidate has a word the source lacked in its place (a
    substitution such as ``utilize`` -> ``use``). Dropped words with nothing in
    their place are losses; a model that pads cannot buy back more words than
    it added. Returns ``None`` when the source is too short to measure.
    """
    source_words = content_words(source)
    total = sum(source_words.values())
    if total < MIN_CONTENT_WORDS_FOR_RETENTION:
        return None
    candidate_words = content_words(candidate)
    matched = sum((source_words & candidate_words).values())
    missing = source_words - candidate_words
    if not missing:
        return 1.0
    novel = candidate_words - source_words
    stems: Counter[str] = Counter()
    for word, count in novel.items():
        if len(word) > _STEM_CHARS:
            stems[word[:_STEM_CHARS]] += count
    repaired = 0
    for word, count in missing.items():
        if len(word) <= _STEM_CHARS:
            continue
        available = stems.get(word[:_STEM_CHARS], 0)
        if available:
            used = min(count, available)
            stems[word[:_STEM_CHARS]] = available - used
            repaired += used
    unexplained = sum(missing.values()) - repaired
    # Substitution credit is capped: a copyedit swaps the odd word, while a
    # model that rewrites half a passage in its own words has lost the half.
    substitutions = min(
        unexplained,
        sum(novel.values()) - repaired,
        int(total * MAX_SUBSTITUTION_SHARE),
    )
    return (matched + repaired + substitutions) / total


def ellipsis_count(text: str) -> int:
    """Number of ellipses (``…``, ``...``, ``. . .``) in the text."""
    return len(ELLIPSIS_REGEX.findall(text))


PAGE_BREAK_MARKER = "<!-- page-break -->"
_WORD_RE = re.compile(r"\w+")
# Words taken on each side of a source page break to locate it in the candidate.
_PAGE_BREAK_ANCHOR_WORDS = 6
# Share of anchor words that must match, in order, for a placement to count.
_PAGE_BREAK_MIN_MATCH = 0.6


def _words(text: str) -> list[tuple[str, int, int]]:
    return [(m.group().casefold(), m.start(), m.end()) for m in _WORD_RE.finditer(text)]


def _ordered_matches(anchor: list[str], window: list[str]) -> int:
    """Count anchor words found in order inside the window (subsequence match)."""
    matched = 0
    position = 0
    for word in anchor:
        try:
            position = window.index(word, position) + 1
        except ValueError:
            continue
        matched += 1
    return matched


def restore_page_breaks(source: str, candidate: str) -> tuple[str, bool]:
    """Put the source's page-break markers back where the model lost them.

    Small models drop a few ``<!-- page-break -->`` markers from a long chunk
    (and occasionally invent one), which used to reject the whole chunk on
    every attempt. Each source break is located in the candidate by the words
    on either side of it, and the candidate's markers are rebuilt from those
    positions. Returns the candidate unchanged when any break cannot be placed
    with confidence, so the structural check still fails rather than guess.
    """
    segments = PAGE_BREAK_REGEX.split(source)
    breaks = len(segments) - 1
    stripped = PAGE_BREAK_REGEX.sub("", candidate)
    if breaks == 0:
        text = normalize_markdown_whitespace(stripped) if stripped != candidate else candidate
        return text, stripped != candidate
    words = _words(stripped)
    tokens = [word for word, _start, _end in words]
    positions: list[int] = []
    cursor = 0
    for index in range(1, len(segments)):
        before = [m.group().casefold() for m in _WORD_RE.finditer(segments[index - 1])]
        after = [m.group().casefold() for m in _WORD_RE.finditer(segments[index])]
        before = before[-_PAGE_BREAK_ANCHOR_WORDS:]
        after = after[:_PAGE_BREAK_ANCHOR_WORDS]
        if not before and not after:
            return candidate, False
        best_score = 0
        best_index = -1
        needed = max(1, round((len(before) + len(after)) * _PAGE_BREAK_MIN_MATCH))
        for split in range(cursor, len(tokens) + 1):
            left = tokens[max(cursor, split - len(before) - 2) : split]
            right = tokens[split : split + len(after) + 2]
            score = _ordered_matches(before, left) + _ordered_matches(after, right)
            if score > best_score:
                best_score, best_index = score, split
                if score == len(before) + len(after):
                    break
        if best_index < 0 or best_score < needed:
            return candidate, False
        positions.append(best_index)
        cursor = best_index
    pieces: list[str] = []
    char_cursor = 0
    for split in positions:
        if split >= len(words):
            boundary = len(stripped)
        elif split == 0:
            boundary = 0
        else:
            boundary = words[split - 1][2]
            # Keep closing punctuation and quotes with the sentence they end.
            while boundary < len(stripped) and stripped[boundary] in ".,;:!?)\"'\u2019\u201d*_":
                boundary += 1
        pieces.append(stripped[char_cursor:boundary].rstrip())
        char_cursor = boundary
    pieces.append(stripped[char_cursor:].lstrip())
    rebuilt = f"\n\n{PAGE_BREAK_MARKER}\n\n".join(piece.strip("\n") for piece in pieces)
    return normalize_markdown_whitespace(rebuilt), True


@dataclass(frozen=True, slots=True)
class SourceContext:
    before: str = ""
    after: str = ""


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Validated text and warnings."""

    text: str
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())


def validate_cleaned_text(
    result: str,
    *,
    input_size: int,
    source_text: str | None = None,
    context: SourceContext | None = None,
) -> ValidationResult:
    """Remove obvious LLM artifacts and report suspicious output."""
    warnings: list[str] = []
    text = result.strip()

    if not text:
        return ValidationResult(text="", warnings=("empty-output",))

    filtered = filter_artifact_lines(text, source_text=source_text)
    if filtered != text:
        warnings.append("artifact-filtered")
        text = filtered

    if not text:
        warnings.append("empty-after-artifact-filter")
        return ValidationResult(text="", warnings=tuple(warnings))

    if input_size > 100 and len(text) < input_size * 0.2:
        warnings.append("suspiciously-short-output")

    text = _normalize_output_whitespace(text)
    if source_text is not None and len(PAGE_BREAK_REGEX.findall(source_text)) != len(
        PAGE_BREAK_REGEX.findall(text)
    ):
        text, restored = restore_page_breaks(source_text, text)
        if restored:
            warnings.append("page-breaks-restored")
    warnings.extend(_markdown_quality_warnings(text, source_text=source_text, context=context))
    return ValidationResult(text=text, warnings=tuple(warnings))


def _normalize_output_whitespace(text: str) -> str:
    return normalize_markdown_whitespace(text)


def filter_artifact_lines(text: str, *, source_text: str | None = None) -> str:
    """Remove generated assistant lines, never quoted source or code containing the same words."""
    source = (source_text or "").casefold()

    def filter_prose(prose: str) -> str:
        kept: list[str] = []
        for line in prose.splitlines(keepends=True):
            match = ARTIFACT_REGEX.match(line.lstrip())
            if match is None or match.group().casefold() in source:
                kept.append(line)
        return "".join(kept)

    return transform_prose(text, filter_prose).strip()


def repairable_number_spans(
    text: str, *, source_context: str | None = None, context: SourceContext | None = None
) -> dict[tuple[int, int], str]:
    """Recognize missing-zero decade spelling only with temporal and decade evidence.

    Values in measurements, versions, identifiers, and quoted labels stay immutable.
    Each recognized digit has exactly one canonical reading. Read-only neighbors
    keep the decision independent of transport boundaries; queries can retain raw spelling.
    """
    prefix = context.before if context else ""
    suffix = context.after if context else ""
    combined = prefix + text + suffix
    offset = len(prefix)
    supported: set[str] = (
        set(repairable_number_spans(source_context, context=context).values())
        if source_context is not None
        else set()
    )
    pattern = re.compile(
        r"\b(?:in|during|since|throughout|from)\s+(?:the\s+)?['’]([1-9])s\b(?!['’])", re.I
    )
    duration = re.compile(
        r"\s+(?:interval|duration|delay|timeout|pulse|measurement|timer|window)\b", re.I
    )
    peers = re.compile(
        r"\b(?:in|during|since|throughout|from)\s+(?:the\s+['’]?|['’])[1-9]0s\b", re.I
    )
    protected = [*protected_spans(text), *(m.span() for m in re.finditer(r"`[^`\n]+`", text))]
    readings: dict[tuple[int, int], str] = {}
    boundaries = list(re.finditer(r"\n[ \t]*\n", combined))
    for match in pattern.finditer(combined):
        start, end = match.start(1) - offset, match.end(1) - offset
        if not 0 <= start < end <= len(text):
            continue
        if any(left <= start < right for left, right in protected):
            continue
        if duration.match(combined[match.end() :]):
            continue
        canonical = match.group(1) + "0"
        # A character-radius cutoff makes the same lexical reading alternate
        # between repairable and immutable as transport windows move. Use the
        # source paragraph evidence already supplied by the caller instead.
        left = max((b.end() for b in boundaries if b.end() <= match.start()), default=0)
        right = min(
            (b.start() for b in boundaries if b.start() >= match.end()), default=len(combined)
        )
        local = combined[left:right]
        if (
            canonical in supported
            or re.search(r"\bdecades?\b", local, re.I)
            or any(not duration.match(local[peer.end() :]) for peer in peers.finditer(local))
        ):
            readings[(start, end)] = canonical
    # A rewrite or a local query can move the same observed decade away from
    # its original preposition. Inherit its established role from source only.
    for match in re.finditer(r"['’]([1-9])s\b(?!['’])", text):
        canonical = match.group(1) + "0"
        if (
            canonical in supported
            and not duration.match(text[match.end() :])
            and not any(left <= match.start(1) < right for left, right in protected)
        ):
            readings[match.span(1)] = canonical
    return readings


def verbatim_number_values(
    text: str, *, source_context: str | None = None, context: SourceContext | None = None
) -> list[str]:
    readings = repairable_number_spans(text, source_context=source_context, context=context)
    return [
        readings.get(match.span(), match.group()) for match in VERBATIM_NUMBER_REGEX.finditer(text)
    ]


def _citation_markers(text: str) -> Counter[str]:
    """Compare citation identities without treating ordinary link labels as citations."""
    markers: Counter[str] = Counter()
    pattern = re.compile(
        r"\[(?:\^)?([0-9A-Za-z][0-9A-Za-z .:-]{0,20})\]"
        r"|(\([A-Z][A-Za-z-]+,\s*\d{4}\))"
    )
    for match in pattern.finditer(text):
        identity = next(value for value in match.groups() if value is not None)
        if not match.group().startswith("[^") and identity.casefold() in {
            "inaudible",
            "unintelligible",
            "unclear",
            "music",
            "applause",
            "laughter",
            "silence",
            "crosstalk",
        }:
            continue
        if text[max(0, match.start() - 1) : match.start()] == "!":
            continue
        if (
            text[match.end() : match.end() + 1] == "("
            and not match.group().startswith("[^")
            and not re.fullmatch(r"[A-Za-z]*[0-9]+", identity)
        ):
            continue
        markers[" ".join(re.sub(r",\s*", ", ", identity).split())] += 1
    for match in re.finditer(r"<sup\b[^>]*>(.*?)</sup>", text, re.I | re.S):
        identity = re.sub(r"<[^>]+>", "", match.group(1)).strip()
        if identity.isdecimal():
            markers[identity] += 1
    return markers


def _markdown_quality_warnings(
    text: str, *, source_text: str | None, context: SourceContext | None = None
) -> list[str]:
    warnings: list[str] = []
    if CONTEXT_MARKER_REGEX.search(text):
        warnings.append("context-marker-leak")
    if ORPHAN_LIST_MARKER_REGEX.search(text):
        warnings.append("orphan-list-marker")
    table_rows = MARKDOWN_TABLE_ROW_REGEX.findall(text)
    if len(table_rows) >= 2 and MARKDOWN_TABLE_SEPARATOR_REGEX.search(text) is None:
        warnings.append("malformed-markdown-table")
    if _has_repeated_tail(text):
        warnings.append("repeated-tail")
    if UNCLEAR_TRANSCRIPTION_REGEX.search(text):
        warnings.append("uncertain-transcription")
    if source_text is None:
        return warnings
    if Counter(literal_links(source_text)) != Counter(literal_links(text)):
        warnings.append("changed-literal-link")
    if _new_adjacent_repetition(source_text, text):
        warnings.append("new-adjacent-repetition")
    before = _lowercase_paragraphs(source_text)
    if any(count > before[word] for word, count in _lowercase_paragraphs(text).items()):
        warnings.append("new-lowercase-paragraph")
    if _paragraph_count(source_text) >= 3 and _paragraph_count(text) <= 1 and len(text) > 500:
        warnings.append("collapsed-paragraphs")
    if MARKDOWN_HEADING_REGEX.search(source_text) and MARKDOWN_HEADING_REGEX.search(text) is None:
        warnings.append("missing-markdown-heading")
    if MARKDOWN_LIST_REGEX.search(source_text) and MARKDOWN_LIST_REGEX.search(text) is None:
        warnings.append("missing-markdown-list")
    if _has_markdown_table(source_text) and not _has_markdown_table(text):
        warnings.append("missing-markdown-table")
    source_citations = _citation_markers(source_text)
    output_citations = _citation_markers(text)
    if source_citations - output_citations:
        warnings.append("missing-citation-marker")
    source_images = Counter(ALL_IMAGE_REFERENCE_REGEX.findall(source_text))
    output_images = Counter(ALL_IMAGE_REFERENCE_REGEX.findall(text))
    if source_images != output_images:
        warnings.append("changed-markdown-images")
    source_code = [
        source_text[a:b].strip()
        for a, b in protected_spans(source_text)
        if source_text[a:b].lstrip().startswith(("```", "~~~"))
    ]
    output_code = [
        text[a:b].strip()
        for a, b in protected_spans(text)
        if text[a:b].lstrip().startswith(("```", "~~~"))
    ]
    if source_code and source_code != output_code:
        warnings.append("changed-code-block")
    source_tables = _table_cells(source_text)
    output_tables = _table_cells(text)
    if source_tables:
        if [len(row) for row in source_tables] != [len(row) for row in output_tables]:
            warnings.append("changed-table-shape")
        elif [[VERBATIM_NUMBER_REGEX.findall(cell) for cell in row] for row in source_tables] != [
            [VERBATIM_NUMBER_REGEX.findall(cell) for cell in row] for row in output_tables
        ]:
            warnings.append("changed-table-values")
    if len(PAGE_BREAK_REGEX.findall(source_text)) != len(PAGE_BREAK_REGEX.findall(text)):
        warnings.append("changed-page-breaks")
    source_numbers = Counter(verbatim_number_values(source_text, context=context))
    output_numbers = Counter(
        verbatim_number_values(text, source_context=source_text, context=context)
    )
    if source_numbers - output_numbers:
        warnings.append("missing-verbatim-number")
    if output_numbers - source_numbers:
        warnings.append("added-verbatim-number")
    if source_numbers == output_numbers and Counter(
        VERBATIM_NUMBER_REGEX.findall(source_text)
    ) != Counter(VERBATIM_NUMBER_REGEX.findall(text)):
        warnings.append("repaired-decade-shorthand")
    retention = content_retention(source_text, text)
    if retention is not None and retention < MIN_CONTENT_RETENTION:
        warnings.append("substantial-content-loss")
    if ellipsis_count(text) > ellipsis_count(source_text):
        # A model that abbreviates ("I own a list that I...") leaves ellipses
        # the source never had. Copyediting never introduces one.
        warnings.append("model-elided-text")
    return warnings


def _lowercase_paragraphs(text: str) -> Counter[str]:
    found: Counter[str] = Counter()

    def inspect(prose: str) -> str:
        paragraphs = re.split(r"\n[ \t]*\n", prose)
        for previous, current in zip(paragraphs, paragraphs[1:], strict=False):
            if not re.search(r"[.!?][\"'”’)]?$", previous.rstrip()):
                continue
            opening = re.match(
                r"(?:and|but|or|so|now|then|we|the|you|he|she|they|it|this|that|"
                r"these|those|for|however|there|what|when|where|why|how|as|i)\b",
                current.lstrip(),
            )
            if opening:
                found[opening.group()] += 1
        return prose

    transform_prose(text, inspect)
    return found


def _new_adjacent_repetition(source: str, candidate: str) -> bool:
    """Reject a newly copied adjacent phrase, without deleting intentional source rhetoric."""

    def prose_only(value: str) -> str:
        pieces: list[str] = []
        cursor = 0
        for left, right in protected_spans(value):
            pieces.append(value[cursor:left])
            cursor = right
        pieces.append(value[cursor:])
        return re.sub(r"\s+", " ", " ".join(pieces)).casefold()

    original, edited = prose_only(source), prose_only(candidate)
    repeated = re.compile(r"\b((?:[\w'-]+\s+){3}[\w'-]+)[.!?…;,:]*\s+\1\b")
    for match in repeated.finditer(edited):
        phrase = re.compile(r"(?<!\w)" + re.escape(match.group(1)) + r"(?!\w)")
        if len(phrase.findall(edited)) > len(phrase.findall(original)):
            return True
    return False


def _paragraph_count(text: str) -> int:
    return sum(1 for paragraph in re.split(r"\n\s*\n", text.strip()) if paragraph.strip())


def _table_cells(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for left, right in protected_spans(text):
        block = text[left:right]
        if block.lstrip().startswith(("```", "~~~")):
            continue
        for line in block.splitlines():
            if MARKDOWN_TABLE_SEPARATOR_REGEX.fullmatch(line):
                continue
            body = line.strip()
            if body.startswith("|"):
                body = body[1:]
            if body.endswith("|") and not body.endswith("\\|"):
                body = body[:-1]
            rows.append([cell.strip() for cell in re.split(r"(?<!\\)\|", body)])
    return rows


def _has_markdown_table(text: str) -> bool:
    return (
        len(MARKDOWN_TABLE_ROW_REGEX.findall(text)) >= 2
        and MARKDOWN_TABLE_SEPARATOR_REGEX.search(text) is not None
    )


def _has_repeated_tail(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", text.strip())
    max_pattern_length = min(
        REPEATED_TAIL_MAX_PATTERN_CHARS,
        len(normalized) // REPEATED_TAIL_MIN_REPEATS,
    )
    if max_pattern_length < REPEATED_TAIL_MIN_PATTERN_CHARS:
        return False

    for pattern_length in range(REPEATED_TAIL_MIN_PATTERN_CHARS, max_pattern_length + 1):
        pattern = normalized[-pattern_length:]
        if len(set(re.findall(r"\w", pattern))) < 2:
            continue
        repeats = 0
        position = len(normalized)
        while position >= pattern_length:
            if normalized[position - pattern_length : position] != pattern:
                break
            repeats += 1
            position -= pattern_length
        if repeats >= REPEATED_TAIL_MIN_REPEATS:
            return True
    return False
