"""Transcript normalization, sentence reconstruction, and export helpers."""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path


class TranscriptFormat(StrEnum):
    """Supported transcript render formats."""

    MARKDOWN = "md"
    TEXT = "txt"
    SRT = "srt"
    CSV = "csv"


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """One timestamped transcript segment."""

    text: str
    start_seconds: float
    duration_seconds: float
    speaker: str | None = None

    @property
    def end_seconds(self) -> float:
        """Return the segment end timestamp."""
        return self.start_seconds + self.duration_seconds


@dataclass(frozen=True, slots=True)
class TranscriptMatch:
    """A quote match mapped back to transcript segment timestamps."""

    quote: str
    matched_text: str
    start_seconds: float
    end_seconds: float
    start_segment_index: int
    end_segment_index: int
    strategy: str
    confidence: float


@dataclass(frozen=True, slots=True)
class _SegmentBoundary:
    segment_index: int
    start_pos: int
    end_pos: int


_TIMESTAMP_TOKEN = r"(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[\.,]\d{1,3})?"  # noqa: S105
_TIMESTAMP_RANGE_RE = re.compile(
    rf"^\s*(?:\d+\s+)?(?:\[|\()?({_TIMESTAMP_TOKEN})\s*(?:-->|-|to|–|—)\s*"
    rf"({_TIMESTAMP_TOKEN})(?:\]|\))?\s*(.*)$",
    re.IGNORECASE,
)
_TIMESTAMP_PREFIX_RE = re.compile(
    rf"^\s*(?:\[|\()?({_TIMESTAMP_TOKEN})(?:\]|\))?\s*(.*)$",
    re.IGNORECASE,
)
_SPEAKER_RE = re.compile(r"^(?P<speaker>[A-Za-z][\w .'-]{0,48}):\s+(?P<text>.+)$")
_SENTENCE_END_RE = re.compile(r"""[.!?。！？]["')\]]*$""")
_COMMON_ABBREVIATIONS = {
    "adj.",
    "adm.",
    "approx.",
    "asst.",
    "avg.",
    "capt.",
    "cf.",
    "col.",
    "corp.",
    "dept.",
    "dr.",
    "e.g.",
    "etc.",
    "fig.",
    "gen.",
    "gov.",
    "hon.",
    "i.e.",
    "inc.",
    "jr.",
    "ltd.",
    "mr.",
    "mrs.",
    "ms.",
    "no.",
    "prof.",
    "rep.",
    "rev.",
    "sec.",
    "sen.",
    "sr.",
    "st.",
    "vs.",
}
_MAX_SENTENCE_DURATION_SECONDS = 24.0
_MAX_SENTENCE_WORDS = 80
_MAX_SEGMENTS_PER_SENTENCE = 20
_MAX_QUOTE_MATCH_WINDOW_SEGMENTS = 12


def parse_timestamp(value: str) -> float:
    """Parse a transcript timestamp into seconds."""
    clean = value.strip().replace(",", ".")
    if not clean:
        raise ValueError("timestamp must not be empty")
    parts = clean.split(":")
    if len(parts) == 2:
        minutes = int(parts[0])
        seconds = float(parts[1])
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
        return hours * 3600 + minutes * 60 + seconds
    if len(parts) == 1:
        return float(parts[0])
    raise ValueError(f"invalid timestamp: {value}")


def format_compact_timestamp(seconds: float) -> str:
    """Format seconds as MM:SS or HH:MM:SS."""
    whole = max(0, int(seconds))
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_srt_timestamp(seconds: float) -> str:
    """Format seconds as an SRT timestamp."""
    milliseconds_total = max(0, int(round(seconds * 1000)))
    whole, millis = divmod(milliseconds_total, 1000)
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse_transcript(text: str) -> list[TranscriptSegment]:
    """Parse common timestamped transcript formats into segments.

    Supported inputs include SRT/VTT-ish timestamp ranges and line-oriented
    transcripts with a timestamp prefix. Untimestamped continuation lines are
    appended to the previous segment.
    """
    segments: list[TranscriptSegment] = []
    pending_text: list[str] = []
    pending_start: float | None = None
    pending_end: float | None = None
    pending_speaker: str | None = None

    def flush() -> None:
        nonlocal pending_text, pending_start, pending_end, pending_speaker
        if pending_start is None or not pending_text:
            pending_text = []
            return
        normalized = _normalize_segment_text(" ".join(pending_text))
        if normalized:
            end = pending_end if pending_end is not None else pending_start + 1.0
            duration = max(end - pending_start, 0.001)
            text_value, speaker = _split_speaker(normalized)
            segments.append(
                TranscriptSegment(
                    text=text_value,
                    start_seconds=pending_start,
                    duration_seconds=duration,
                    speaker=speaker or pending_speaker,
                )
            )
        pending_text = []
        pending_start = None
        pending_end = None
        pending_speaker = None

    lines = text.replace("\ufeff", "").splitlines()
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.upper() == "WEBVTT" or line.isdigit():
            continue
        range_match = _TIMESTAMP_RANGE_RE.match(line)
        if range_match:
            flush()
            pending_start = parse_timestamp(range_match.group(1))
            pending_end = parse_timestamp(range_match.group(2))
            rest = _strip_vtt_settings(range_match.group(3))
            if rest:
                pending_text.append(rest)
            continue
        prefix_match = _TIMESTAMP_PREFIX_RE.match(line)
        if prefix_match:
            flush()
            pending_start = parse_timestamp(prefix_match.group(1))
            pending_end = None
            rest = prefix_match.group(2).strip()
            if rest:
                pending_text.append(rest)
            continue
        if pending_start is not None:
            pending_text.append(line)

    flush()
    return _infer_missing_durations(segments)


def merge_transcript_sentences(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    """Merge small transcript segments into sentence-like timestamped spans."""
    merged: list[TranscriptSegment] = []
    current: list[TranscriptSegment] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        text = _normalize_segment_text(" ".join(item.text for item in current))
        start = current[0].start_seconds
        end = max(item.end_seconds for item in current)
        speaker = _merged_speaker(current)
        merged.append(
            TranscriptSegment(
                text=text,
                start_seconds=start,
                duration_seconds=max(end - start, 0.001),
                speaker=speaker,
            )
        )
        current = []

    for segment in segments:
        if (
            current
            and segment.speaker
            and current[-1].speaker
            and segment.speaker != current[-1].speaker
        ):
            flush()
        current.append(segment)
        if _should_flush_sentence(current):
            flush()
    flush()
    return merged


def render_transcript(
    segments: list[TranscriptSegment],
    *,
    format: TranscriptFormat,
    merge_sentences: bool = True,
) -> str:
    """Render transcript segments in the requested format."""
    rendered_segments = merge_transcript_sentences(segments) if merge_sentences else segments
    if format == TranscriptFormat.TEXT:
        return "\n".join(
            _segment_display_text(item, include_speaker=True) for item in rendered_segments
        )
    if format == TranscriptFormat.MARKDOWN:
        return "\n".join(
            f"- [{format_compact_timestamp(item.start_seconds)}] "
            f"{_segment_display_text(item, include_speaker=True)}"
            for item in rendered_segments
        )
    if format == TranscriptFormat.SRT:
        return _render_srt(rendered_segments)
    if format == TranscriptFormat.CSV:
        return _render_csv(rendered_segments)
    raise ValueError(f"Unsupported transcript format: {format.value}")


def normalize_transcript_file(
    input_path: Path,
    output_path: Path,
    *,
    format: TranscriptFormat,
    merge_sentences: bool = True,
    overwrite: bool = False,
) -> int:
    """Normalize a transcript file and write the rendered output."""
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"Output already exists: {output_path}")
    text = input_path.read_text(encoding="utf-8")
    segments = parse_transcript(text)
    if not segments:
        raise ValueError("No timestamped transcript segments found")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_transcript(segments, format=format, merge_sentences=merge_sentences),
        encoding="utf-8",
    )
    return len(segments)


def find_quote_in_transcript(
    segments: list[TranscriptSegment],
    quote: str,
    *,
    min_confidence: float = 0.78,
) -> TranscriptMatch | None:
    """Find a quote and map it back to transcript timestamps."""
    normalized_quote = _normalize_for_matching(quote)
    if not normalized_quote:
        raise ValueError("quote must contain searchable text")
    if not 0 <= min_confidence <= 1:
        raise ValueError("min_confidence must be between 0 and 1")

    exact_match = _find_exact_quote_match(segments, quote, normalized_quote)
    if exact_match is not None:
        return exact_match
    return _find_fuzzy_quote_match(
        segments,
        quote,
        normalized_quote,
        min_confidence=min_confidence,
    )


def find_quote_in_transcript_file(
    input_path: Path,
    quote: str,
    *,
    min_confidence: float = 0.78,
) -> TranscriptMatch | None:
    """Parse a transcript file and find a quote in it."""
    segments = parse_transcript(input_path.read_text(encoding="utf-8"))
    if not segments:
        raise ValueError("No timestamped transcript segments found")
    return find_quote_in_transcript(
        segments,
        quote,
        min_confidence=min_confidence,
    )


def transcript_match_json(match: TranscriptMatch) -> str:
    """Render a quote match as stable JSON."""
    return json.dumps(
        {
            "quote": match.quote,
            "matched_text": match.matched_text,
            "start": format_compact_timestamp(match.start_seconds),
            "end": format_compact_timestamp(match.end_seconds),
            "start_seconds": match.start_seconds,
            "end_seconds": match.end_seconds,
            "start_segment_index": match.start_segment_index,
            "end_segment_index": match.end_segment_index,
            "strategy": match.strategy,
            "confidence": match.confidence,
        },
        indent=2,
    )


def _infer_missing_durations(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    if not segments:
        return []
    inferred: list[TranscriptSegment] = []
    for index, segment in enumerate(segments):
        if segment.duration_seconds > 1.0 or index == len(segments) - 1:
            inferred.append(segment)
            continue
        next_start = segments[index + 1].start_seconds
        if next_start > segment.start_seconds:
            inferred.append(
                TranscriptSegment(
                    text=segment.text,
                    start_seconds=segment.start_seconds,
                    duration_seconds=max(next_start - segment.start_seconds, 0.001),
                    speaker=segment.speaker,
                )
            )
        else:
            inferred.append(segment)
    return inferred


def _find_exact_quote_match(
    segments: list[TranscriptSegment],
    quote: str,
    normalized_quote: str,
) -> TranscriptMatch | None:
    normalized_text, boundaries = _normalized_transcript_index(segments)
    match_start = normalized_text.find(normalized_quote)
    if match_start < 0:
        return None
    match_end = match_start + len(normalized_quote)
    start_index, end_index = _map_normalized_span_to_segments(
        boundaries,
        match_start,
        match_end,
    )
    return _build_transcript_match(
        segments,
        quote=quote,
        start_index=start_index,
        end_index=end_index,
        strategy="exact-normalized",
        confidence=1.0,
    )


def _find_fuzzy_quote_match(
    segments: list[TranscriptSegment],
    quote: str,
    normalized_quote: str,
    *,
    min_confidence: float,
) -> TranscriptMatch | None:
    best: tuple[float, int, int] | None = None
    for start_index in range(len(segments)):
        window_text = ""
        upper = min(len(segments), start_index + _MAX_QUOTE_MATCH_WINDOW_SEGMENTS)
        for end_index in range(start_index, upper):
            if window_text:
                window_text += " "
            window_text += segments[end_index].text
            normalized_window = _normalize_for_matching(window_text)
            if not normalized_window:
                continue
            confidence = _quote_match_confidence(normalized_quote, normalized_window)
            if best is None or confidence > best[0]:
                best = (confidence, start_index, end_index)
    if best is None or best[0] < min_confidence:
        return None
    confidence, start_index, end_index = best
    return _build_transcript_match(
        segments,
        quote=quote,
        start_index=start_index,
        end_index=end_index,
        strategy="fuzzy-window",
        confidence=round(confidence, 6),
    )


def _normalized_transcript_index(
    segments: list[TranscriptSegment],
) -> tuple[str, list[_SegmentBoundary]]:
    parts: list[str] = []
    boundaries: list[_SegmentBoundary] = []
    cursor = 0
    for index, segment in enumerate(segments):
        normalized = _normalize_for_matching(segment.text)
        if parts:
            parts.append(" ")
            cursor += 1
        start_pos = cursor
        parts.append(normalized)
        cursor += len(normalized)
        boundaries.append(
            _SegmentBoundary(
                segment_index=index,
                start_pos=start_pos,
                end_pos=cursor,
            )
        )
    return "".join(parts), boundaries


def _map_normalized_span_to_segments(
    boundaries: list[_SegmentBoundary],
    match_start: int,
    match_end: int,
) -> tuple[int, int]:
    start_index = boundaries[0].segment_index
    end_index = boundaries[-1].segment_index
    for boundary in boundaries:
        if boundary.start_pos <= match_start < boundary.end_pos:
            start_index = boundary.segment_index
            break
    for boundary in boundaries:
        if boundary.start_pos < match_end <= boundary.end_pos:
            end_index = boundary.segment_index
            break
        if match_end > boundary.end_pos:
            end_index = boundary.segment_index
    return start_index, end_index


def _build_transcript_match(
    segments: list[TranscriptSegment],
    *,
    quote: str,
    start_index: int,
    end_index: int,
    strategy: str,
    confidence: float,
) -> TranscriptMatch:
    selected = segments[start_index : end_index + 1]
    return TranscriptMatch(
        quote=quote,
        matched_text=_normalize_segment_text(" ".join(segment.text for segment in selected)),
        start_seconds=selected[0].start_seconds,
        end_seconds=max(segment.end_seconds for segment in selected),
        start_segment_index=start_index,
        end_segment_index=end_index,
        strategy=strategy,
        confidence=confidence,
    )


def _quote_match_confidence(normalized_quote: str, normalized_window: str) -> float:
    sequence_ratio = SequenceMatcher(None, normalized_quote, normalized_window).ratio()
    quote_tokens = set(normalized_quote.split())
    window_tokens = set(normalized_window.split())
    if not quote_tokens or not window_tokens:
        return sequence_ratio
    token_overlap = len(quote_tokens & window_tokens) / len(quote_tokens)
    length_penalty = min(len(normalized_quote), len(normalized_window)) / max(
        len(normalized_quote),
        len(normalized_window),
    )
    return (sequence_ratio * 0.55) + (token_overlap * 0.35) + (length_penalty * 0.10)


def _normalize_for_matching(value: str) -> str:
    lowered = value.lower()
    lowered = re.sub(r"[^\w\s]", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _strip_vtt_settings(value: str) -> str:
    return re.sub(r"\s+(?:align|line|position|size|vertical):\S+", "", value).strip()


def _normalize_segment_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _split_speaker(value: str) -> tuple[str, str | None]:
    match = _SPEAKER_RE.match(value)
    if not match:
        return value, None
    return match.group("text").strip(), match.group("speaker").strip()


def _segment_display_text(segment: TranscriptSegment, *, include_speaker: bool) -> str:
    if include_speaker and segment.speaker:
        return f"{segment.speaker}: {segment.text}"
    return segment.text


def _merged_speaker(segments: list[TranscriptSegment]) -> str | None:
    speakers = {item.speaker for item in segments if item.speaker}
    if len(speakers) == 1:
        return next(iter(speakers))
    return None


def _should_flush_sentence(segments: list[TranscriptSegment]) -> bool:
    if not segments:
        return False
    text = _normalize_segment_text(" ".join(item.text for item in segments))
    duration = max(item.end_seconds for item in segments) - segments[0].start_seconds
    word_count = len(text.split())
    if len(segments) >= _MAX_SEGMENTS_PER_SENTENCE:
        return True
    if duration >= _MAX_SENTENCE_DURATION_SECONDS:
        return True
    if word_count >= _MAX_SENTENCE_WORDS:
        return True
    return _looks_like_sentence_end(text)


def _looks_like_sentence_end(text: str) -> bool:
    if not _SENTENCE_END_RE.search(text):
        return False
    last_token = text.split()[-1].lower().strip('"\'“”’)]}')
    if last_token in _COMMON_ABBREVIATIONS:
        return False
    if re.search(r"\d+\.\d+$", last_token):
        return False
    return True


def _render_srt(segments: list[TranscriptSegment]) -> str:
    blocks: list[str] = []
    for index, segment in enumerate(segments, start=1):
        blocks.append(
            "\n".join(
                [
                    str(index),
                    f"{format_srt_timestamp(segment.start_seconds)} --> "
                    f"{format_srt_timestamp(segment.end_seconds)}",
                    _segment_display_text(segment, include_speaker=True),
                ]
            )
        )
    return "\n\n".join(blocks)


def _render_csv(segments: list[TranscriptSegment]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["start", "end", "duration_seconds", "speaker", "text"])
    for segment in segments:
        writer.writerow(
            [
                format_compact_timestamp(segment.start_seconds),
                format_compact_timestamp(segment.end_seconds),
                f"{segment.duration_seconds:.3f}",
                segment.speaker or "",
                segment.text,
            ]
        )
    return output.getvalue()
