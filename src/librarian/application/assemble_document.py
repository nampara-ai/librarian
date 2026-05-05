"""Application service for final assembly."""

from __future__ import annotations

import re
from collections.abc import Sequence

from librarian.application.clean_chunks import CleanedChunk


def assemble_cleaned_document(chunks: Sequence[CleanedChunk]) -> str:
    """Assemble cleaned chunks and perform light boundary cleanup."""
    text = "\n\n".join(chunk.text for chunk in chunks if chunk.text.strip())
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()
