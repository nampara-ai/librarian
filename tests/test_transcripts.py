from pathlib import Path

import pytest

from librarian.application.transcripts import (
    TranscriptFormat,
    format_compact_timestamp,
    format_srt_timestamp,
    merge_transcript_sentences,
    normalize_transcript_file,
    parse_timestamp,
    parse_transcript,
    render_transcript,
)


def test_parse_timestamp_formats() -> None:
    assert parse_timestamp("01:02") == 62
    assert parse_timestamp("01:02:03.250") == 3723.25
    assert parse_timestamp("00:00:03,500") == 3.5
    assert format_compact_timestamp(3723.25) == "01:02:03"
    assert format_srt_timestamp(3723.25) == "01:02:03,250"


def test_parse_transcript_supports_srt_and_speaker_lines() -> None:
    text = """1
00:00:01,000 --> 00:00:02,500
Ada: We measured the baseline.

2
00:00:02,500 --> 00:00:04,000
It held at 3.14.
"""

    segments = parse_transcript(text)

    assert len(segments) == 2
    assert segments[0].speaker == "Ada"
    assert segments[0].text == "We measured the baseline."
    assert segments[0].duration_seconds == 1.5
    assert segments[1].text == "It held at 3.14."


def test_merge_transcript_sentences_avoids_common_abbreviation_split() -> None:
    segments = parse_transcript(
        """[00:00] Dr.
[00:01] Ada measured
[00:02] the baseline.
[00:03] Then it changed."""
    )

    merged = merge_transcript_sentences(segments)

    assert [item.text for item in merged] == [
        "Dr. Ada measured the baseline.",
        "Then it changed.",
    ]


def test_render_transcript_formats_csv_and_srt() -> None:
    segments = parse_transcript(
        """00:00-00:02 Ada: First sentence.
00:02-00:04 Ada: Second sentence."""
    )

    csv_output = render_transcript(
        segments,
        format=TranscriptFormat.CSV,
        merge_sentences=False,
    )
    srt_output = render_transcript(
        segments,
        format=TranscriptFormat.SRT,
        merge_sentences=False,
    )

    assert "start,end,duration_seconds,speaker,text" in csv_output
    assert "00:00,00:02,2.000,Ada,First sentence." in csv_output
    assert "00:00:00,000 --> 00:00:02,000" in srt_output
    assert "Ada: First sentence." in srt_output


def test_normalize_transcript_file_rejects_empty_or_existing_output(tmp_path: Path) -> None:
    source = tmp_path / "input.txt"
    output = tmp_path / "output.md"
    source.write_text("No timestamps here.", encoding="utf-8")

    with pytest.raises(ValueError, match="No timestamped transcript"):
        normalize_transcript_file(source, output, format=TranscriptFormat.MARKDOWN)

    source.write_text("[00:00] Hello.", encoding="utf-8")
    output.write_text("existing", encoding="utf-8")
    with pytest.raises(FileExistsError):
        normalize_transcript_file(source, output, format=TranscriptFormat.MARKDOWN)

    count = normalize_transcript_file(
        source,
        output,
        format=TranscriptFormat.MARKDOWN,
        overwrite=True,
    )
    assert count == 1
    assert output.read_text(encoding="utf-8") == "- [00:00] Hello."
