"""Document export rendering."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from librarian.domain.models import Classification, CleanedOutput, Document

ExportFormat = Literal["txt", "md", "json"]


@dataclass(frozen=True, slots=True)
class ExportedDocument:
    """Document export payload."""

    document: Document
    output: CleanedOutput
    classification: Classification | None

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
        lines.append(self.output.text)
        return "\n".join(lines)

    def _render_json(self) -> str:
        return json.dumps(
            {
                "document_id": str(self.document.id),
                "filename": self.document.source.filename,
                "classification": self.classification_label(),
                "text": self.output.text,
            },
            indent=2,
        )
