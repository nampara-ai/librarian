"""Text extraction adapters."""

from __future__ import annotations

import asyncio
import importlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, cast

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"})


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
            return _ocr_pdf(path)
        return "\n\n".join(parts)


class ImageOcrExtractor:
    """Extractor for image files through Tesseract OCR."""

    supported_extensions = IMAGE_EXTENSIONS

    async def extract(self, path: Path) -> str:
        return await asyncio.to_thread(_ocr_image, path)


class MarkItDownExtractor:
    """Optional broad-format extractor using Microsoft's MarkItDown."""

    supported_extensions = frozenset(
        {
            ".epub",
            ".html",
            ".htm",
            ".msg",
            ".pptx",
            ".rtf",
            ".wav",
            ".mp3",
            ".xls",
            ".xlsx",
            ".xml",
            ".zip",
        }
    )

    async def extract(self, path: Path) -> str:
        return await asyncio.to_thread(self._extract_sync, path)

    def _extract_sync(self, path: Path) -> str:
        try:
            markitdown_module = importlib.import_module("markitdown")
        except ImportError as exc:
            raise RuntimeError(
                "Broad format conversion requires installing the 'universal' extra"
            ) from exc

        markitdown_class = markitdown_module.MarkItDown
        converter = markitdown_class()
        result = converter.convert(str(path))
        text_content = getattr(result, "text_content", None)
        if not isinstance(text_content, str) or not text_content.strip():
            raise ValueError(f"No extractable content found: {path}")
        return text_content


class CompositeExtractor:
    """Route extraction by file extension."""

    def __init__(self) -> None:
        extractors = [
            TextFamilyExtractor(),
            DocxExtractor(),
            PdfExtractor(),
            ImageOcrExtractor(),
            MarkItDownExtractor(),
        ]
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


def _ocr_image(path: Path) -> str:
    tesseract_path = shutil.which("tesseract")
    if tesseract_path is None:
        raise RuntimeError("OCR requires the 'tesseract' executable on PATH")
    completed = subprocess.run(  # noqa: S603
        [tesseract_path, str(path), "stdout", "-l", "eng"],
        check=True,
        capture_output=True,
        text=True,
    )
    text = completed.stdout.strip()
    if not text:
        raise ValueError(f"No OCR text found in image: {path}")
    return text


def _ocr_pdf(path: Path) -> str:
    if shutil.which("tesseract") is None:
        raise RuntimeError("Scanned PDF OCR requires the 'tesseract' executable on PATH")
    try:
        pdf2image = importlib.import_module("pdf2image")
    except ImportError as exc:
        raise RuntimeError("Scanned PDF OCR requires installing the 'ocr' extra") from exc

    with tempfile.TemporaryDirectory() as tmp_dir:
        image_paths = cast(
            list[Path],
            pdf2image.convert_from_path(
                path,
                dpi=300,
                output_folder=tmp_dir,
                fmt="png",
                paths_only=True,
            ),
        )
        parts = [_ocr_image(image_path) for image_path in image_paths]

    text = "\n\n".join(part for part in parts if part.strip())
    if not text:
        raise ValueError(f"No OCR text found in PDF: {path}")
    return text
