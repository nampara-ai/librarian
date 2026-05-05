"""Text extraction adapters."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import Any, cast


class TextFamilyExtractor:
    """Extractor for UTF-8 text-like files."""

    supported_extensions = frozenset({".txt", ".md", ".csv", ".json"})

    async def extract(self, path: Path) -> str:
        return await asyncio.to_thread(self._extract_sync, path)

    def _extract_sync(self, path: Path) -> str:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return text
            return json.dumps(parsed, indent=2, ensure_ascii=False)
        return text


class DocxExtractor:
    """Extractor for DOCX files."""

    supported_extensions = frozenset({".docx"})

    async def extract(self, path: Path) -> str:
        return await asyncio.to_thread(self._extract_sync, path)

    def _extract_sync(self, path: Path) -> str:
        from docx import Document

        doc = Document(str(path))
        return "\n\n".join(paragraph.text for paragraph in doc.paragraphs if paragraph.text.strip())


class PdfExtractor:
    """Extractor for text-bearing PDFs."""

    supported_extensions = frozenset({".pdf"})

    async def extract(self, path: Path) -> str:
        return await asyncio.to_thread(self._extract_sync, path)

    def _extract_sync(self, path: Path) -> str:
        try:
            pdfplumber = importlib.import_module("pdfplumber")
        except ImportError as exc:
            raise RuntimeError("PDF support requires installing the 'pdf' extra") from exc

        parts: list[str] = []
        pdf_module = cast(Any, pdfplumber)
        with pdf_module.open(path) as pdf:
            for page in pdf.pages:
                page_text = cast(str | None, page.extract_text())
                if page_text:
                    parts.append(page_text)

        if not parts:
            raise ValueError(f"No extractable text found in PDF: {path}")
        return "\n\n".join(parts)


class CompositeExtractor:
    """Route extraction by file extension."""

    def __init__(self) -> None:
        extractors = [TextFamilyExtractor(), DocxExtractor(), PdfExtractor()]
        self._extractors = {
            extension: extractor
            for extractor in extractors
            for extension in extractor.supported_extensions
        }
        self.supported_extensions = frozenset(self._extractors)

    async def extract(self, path: Path) -> str:
        extension = path.suffix.lower()
        extractor = self._extractors.get(extension)
        if extractor is None:
            raise ValueError(f"Unsupported file extension: {extension}")
        return await extractor.extract(path)
