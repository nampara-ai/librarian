"""Text extraction adapters."""

from __future__ import annotations

import asyncio
import importlib
import json
import multiprocessing
import queue
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

    def __init__(
        self,
        *,
        ocr_language: str = "eng",
        ocr_timeout_seconds: int = 120,
        ocr_pdf_dpi: int = 200,
        ocr_pdf_max_pages: int = 100,
    ) -> None:
        self.ocr_language = ocr_language
        self.ocr_timeout_seconds = ocr_timeout_seconds
        self.ocr_pdf_dpi = ocr_pdf_dpi
        self.ocr_pdf_max_pages = ocr_pdf_max_pages

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
            return _ocr_pdf(
                path,
                language=self.ocr_language,
                timeout_seconds=self.ocr_timeout_seconds,
                dpi=self.ocr_pdf_dpi,
                max_pages=self.ocr_pdf_max_pages,
            )
        return "\n\n".join(parts)


class ImageOcrExtractor:
    """Extractor for image files through Tesseract OCR."""

    supported_extensions = IMAGE_EXTENSIONS

    def __init__(self, *, language: str = "eng", timeout_seconds: int = 120) -> None:
        self.language = language
        self.timeout_seconds = timeout_seconds

    async def extract(self, path: Path) -> str:
        return await asyncio.to_thread(
            _ocr_image,
            path,
            language=self.language,
            timeout_seconds=self.timeout_seconds,
        )


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
        }
    )

    def __init__(
        self,
        *,
        max_input_bytes: int = 50 * 1024 * 1024,
        timeout_seconds: int = 120,
    ) -> None:
        self.max_input_bytes = max_input_bytes
        self.timeout_seconds = timeout_seconds

    async def extract(self, path: Path) -> str:
        return await asyncio.to_thread(self._extract_with_timeout, path)

    def _extract_with_timeout(self, path: Path) -> str:
        self._validate_input_size(path)
        result_queue: multiprocessing.Queue[tuple[str, str]] = multiprocessing.Queue(maxsize=1)
        process = multiprocessing.Process(
            target=_markitdown_worker,
            args=(str(path), self.max_input_bytes, result_queue),
            daemon=True,
        )
        process.start()
        process.join(self.timeout_seconds)
        if process.is_alive():
            process.terminate()
            process.join()
            raise TimeoutError(f"Broad format conversion timed out after {self.timeout_seconds}s")
        try:
            status, payload = result_queue.get_nowait()
        except queue.Empty as exc:
            raise RuntimeError("Broad format conversion failed without returning a result") from exc
        if status == "ok":
            return payload
        raise RuntimeError(payload)

    def _extract_sync(self, path: Path) -> str:
        self._validate_input_size(path)
        return _extract_markitdown_sync(path)

    def _validate_input_size(self, path: Path) -> None:
        _validate_markitdown_input_size(path, self.max_input_bytes)


class CompositeExtractor:
    """Route extraction by file extension."""

    def __init__(
        self,
        *,
        ocr_language: str = "eng",
        ocr_timeout_seconds: int = 120,
        ocr_pdf_dpi: int = 200,
        ocr_pdf_max_pages: int = 100,
        universal_max_input_bytes: int = 50 * 1024 * 1024,
        universal_timeout_seconds: int = 120,
    ) -> None:
        extractors = [
            TextFamilyExtractor(),
            DocxExtractor(),
            PdfExtractor(
                ocr_language=ocr_language,
                ocr_timeout_seconds=ocr_timeout_seconds,
                ocr_pdf_dpi=ocr_pdf_dpi,
                ocr_pdf_max_pages=ocr_pdf_max_pages,
            ),
            ImageOcrExtractor(language=ocr_language, timeout_seconds=ocr_timeout_seconds),
            MarkItDownExtractor(
                max_input_bytes=universal_max_input_bytes,
                timeout_seconds=universal_timeout_seconds,
            ),
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


def _ocr_image(path: Path, *, language: str = "eng", timeout_seconds: int = 120) -> str:
    tesseract_path = shutil.which("tesseract")
    if tesseract_path is None:
        raise RuntimeError("OCR requires the 'tesseract' executable on PATH")
    completed = subprocess.run(  # noqa: S603
        [tesseract_path, str(path), "stdout", "-l", language],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    text = completed.stdout.strip()
    if not text:
        raise ValueError(f"No OCR text found in image: {path}")
    return text


def _ocr_pdf(
    path: Path,
    *,
    language: str = "eng",
    timeout_seconds: int = 120,
    dpi: int = 200,
    max_pages: int = 100,
) -> str:
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
                dpi=dpi,
                last_page=max_pages,
                output_folder=tmp_dir,
                fmt="png",
                paths_only=True,
                timeout=timeout_seconds,
            ),
        )
        parts = [
            _ocr_image(image_path, language=language, timeout_seconds=timeout_seconds)
            for image_path in image_paths
        ]

    text = "\n\n".join(part for part in parts if part.strip())
    if not text:
        raise ValueError(f"No OCR text found in PDF: {path}")
    return text


def _markitdown_worker(
    path: str,
    max_input_bytes: int,
    result_queue: multiprocessing.Queue[tuple[str, str]],
) -> None:
    try:
        resolved_path = Path(path)
        _validate_markitdown_input_size(resolved_path, max_input_bytes)
        result_queue.put(("ok", _extract_markitdown_sync(resolved_path)))
    except Exception as exc:
        result_queue.put(("error", str(exc)))


def _extract_markitdown_sync(path: Path) -> str:
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


def _validate_markitdown_input_size(path: Path, max_input_bytes: int) -> None:
    byte_size = path.stat().st_size
    if byte_size > max_input_bytes:
        raise ValueError(f"Broad format conversion input exceeds {max_input_bytes} bytes: {path}")
