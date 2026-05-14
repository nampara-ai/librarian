"""Transcript normalization, sentence reconstruction, and export helpers."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
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
