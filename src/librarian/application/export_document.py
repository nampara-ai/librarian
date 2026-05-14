"""Document export rendering."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from librarian.application.transcripts import find_quote_in_transcript, parse_transcript
from librarian.domain.models import Classification, CleanedOutput, Document, TranscriptCitation

ExportFormat = Literal["txt", "md", "json"]


@dataclass(frozen=True, slots=True)
class ExportedDocument:
    """Document export payload."""

    document: Document
    output: CleanedOutput
    classification: Classification | None
    transcript_citation: TranscriptCitation | None = None

    def render(self, format: ExportFormat) -> str:
        """Render the export in the requested format."""
        if format == "txt":
            return self.output.text
        if format == "md":
            return self._render_markdown()
        if format == "json":
            return self._render_json()
        raise ValueError(f"Unsupported export format: {format}")

    def classification_label(self) -> str | None:
        """Return a compact classification label."""
        if self.classification is None:
            return None
        return f"{self.classification.code} - {self.classification.label}"

    def _render_markdown(self) -> str:
        title = self.document.source.filename.rsplit(".", 1)[0]
        lines = [f"# {title}", ""]
        classification = self.classification_label()
        if classification:
            lines.extend([f"Classification: {classification}", ""])
        if self.transcript_citation:
            citation = self.transcript_citation
            lines.extend(
                [
                    "## Source Citation",
                    "",
                    f"- Time: {citation.start_seconds:.3f}s-{citation.end_seconds:.3f}s",
                    f"- Segments: {citation.start_segment_index}-{citation.end_segment_index}",
                    f"- Match: {citation.strategy} ({citation.confidence:.3f})",
                    "",
                    "> " + citation.matched_text.replace("\n", " "),
                    "",
                ]
            )
        lines.append(self.output.text)
        return "\n".join(lines)

    def _render_json(self) -> str:
        payload: dict[str, object] = {
            "document_id": str(self.document.id),
            "filename": self.document.source.filename,
            "classification": self.classification_label(),
            "text": self.output.text,
        }
        if self.transcript_citation:
            citation = self.transcript_citation
            payload["transcript_citation"] = {
                "matched_text": citation.matched_text,
                "start_seconds": citation.start_seconds,
                "end_seconds": citation.end_seconds,
                "start_segment_index": citation.start_segment_index,
                "end_segment_index": citation.end_segment_index,
                "strategy": citation.strategy,
                "confidence": citation.confidence,
            }
        return json.dumps(payload, indent=2)


def transcript_citation_for_document(
    document: Document,
    quote: str | None,
    *,
    min_confidence: float = 0.82,
) -> TranscriptCitation | None:
    """Map an export quote back to transcript source timestamps when possible."""
    if quote is None:
        return None
    normalized_quote = quote.strip()
    if not normalized_quote:
        raise ValueError("citation quote must contain searchable text")
    try:
        segments = parse_transcript(document.source.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("Document source is not available as a timestamped transcript") from exc
    if not segments:
        raise ValueError("Document source is not a timestamped transcript")
    match = find_quote_in_transcript(
        segments,
        normalized_quote,
        min_confidence=min_confidence,
    )
    if match is None:
        raise ValueError("Citation quote was not found in transcript source")
    return TranscriptCitation(
        matched_text=match.matched_text,
        start_seconds=match.start_seconds,
        end_seconds=match.end_seconds,
        start_segment_index=match.start_segment_index,
        end_segment_index=match.end_segment_index,
        strategy=match.strategy,
        confidence=match.confidence,
    )
