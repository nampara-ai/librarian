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
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"})
OcrCorrectionMode = Literal["always", "never", "low-confidence"]

OCR_CORRECTION_PROMPT = """You are correcting OCR text extracted from a PDF page.
Preserve every detail, name, number, heading, and paragraph. Fix OCR recognition errors,
line-break artifacts, spacing, and obvious punctuation issues. Do not summarize, omit,
invent, or reorder content. Return only corrected Markdown-compatible text."""


class OcrCorrectionProvider(Protocol):
    """Minimal LLM provider surface needed for OCR correction."""

    @property
    def name(self) -> str: ...

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model: str,
        max_tokens: int,
        temperature: float,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class PdfPageExtraction:
    """One extracted PDF page."""

    page_number: int
    text: str
    source: str
    confidence: float | None = None
    corrected: bool = False
    error: str | None = None


class TextFamilyExtractor:
    """Extractor for UTF-8 text-like files."""

    supported_extensions = frozenset({".txt", ".md", ".csv", ".json"})

    def __init__(self, *, max_input_bytes: int = 100 * 1024 * 1024) -> None:
        self.max_input_bytes = max_input_bytes

    async def extract(self, path: Path) -> str:
        return await asyncio.to_thread(self._extract_sync, path)

    def _extract_sync(self, path: Path) -> str:
        _validate_input_size(path, self.max_input_bytes, "Text extraction input")
        _validate_text_like(path)
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

    def __init__(self, *, max_input_bytes: int = 100 * 1024 * 1024) -> None:
        self.max_input_bytes = max_input_bytes

    async def extract(self, path: Path) -> str:
        return await asyncio.to_thread(self._extract_sync, path)

    def _extract_sync(self, path: Path) -> str:
        _validate_input_size(path, self.max_input_bytes, "DOCX extraction input")
        from docx import Document

        doc = Document(str(path))
        parts = [
            *(_paragraph_text(paragraph) for paragraph in doc.paragraphs),
            *(_table_text(table) for table in doc.tables),
        ]
        for section in doc.sections:
            parts.extend(_paragraph_text(paragraph) for paragraph in section.header.paragraphs)
            parts.extend(_table_text(table) for table in section.header.tables)
            parts.extend(_paragraph_text(paragraph) for paragraph in section.footer.paragraphs)
            parts.extend(_table_text(table) for table in section.footer.tables)
        return "\n\n".join(part for part in parts if part.strip())


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
        ocr_correction_provider: OcrCorrectionProvider | None = None,
        ocr_correction_mode: OcrCorrectionMode = "always",
        ocr_correction_model: str = "mock-cleaner",
        ocr_page_concurrency: int = 2,
        ocr_fail_on_page_error: bool = True,
        max_input_bytes: int = 200 * 1024 * 1024,
        max_pages: int = 1_000,
    ) -> None:
        self.ocr_language = ocr_language
        self.ocr_timeout_seconds = ocr_timeout_seconds
        self.ocr_pdf_dpi = ocr_pdf_dpi
        self.ocr_pdf_max_pages = ocr_pdf_max_pages
        self.ocr_correction_provider = ocr_correction_provider
        self.ocr_correction_mode = ocr_correction_mode
        self.ocr_correction_model = ocr_correction_model
        self.ocr_page_concurrency = ocr_page_concurrency
        self.ocr_fail_on_page_error = ocr_fail_on_page_error
        self.max_input_bytes = max_input_bytes
        self.max_pages = max_pages
        self.last_metadata: dict[str, object] | None = None

    async def extract(self, path: Path) -> str:
        _validate_input_size(path, self.max_input_bytes, "PDF extraction input")
        pages, page_count = await asyncio.to_thread(self._extract_embedded_pages, path)
        if page_count > self.max_pages:
            raise ValueError(
                f"PDF has {page_count} pages, exceeding configured limit {self.max_pages}: {path}"
            )

        empty_pages = [page.page_number for page in pages if not page.text.strip()]
        if len(empty_pages) > self.ocr_pdf_max_pages:
            page_list = ", ".join(str(page_number) for page_number in empty_pages)
            raise ValueError(
                f"PDF contains {len(empty_pages)} scanned/empty pages, exceeding OCR page "
                f"limit {self.ocr_pdf_max_pages}: {page_list}"
            )

        page_results: list[PdfPageExtraction | None] = list(pages)
        semaphore = asyncio.Semaphore(max(1, self.ocr_page_concurrency))

        async def recover_page(page_number: int) -> None:
            async with semaphore:
                try:
                    ocr_text = await asyncio.to_thread(
                        _ocr_pdf_page,
                        path,
                        page_number=page_number,
                        language=self.ocr_language,
                        timeout_seconds=self.ocr_timeout_seconds,
                        dpi=self.ocr_pdf_dpi,
                    )
                    corrected = await self._correct_ocr_page(
                        page_number=page_number,
                        text=ocr_text,
                    )
                    page_results[page_number - 1] = PdfPageExtraction(
                        page_number=page_number,
                        text=corrected,
                        source="ocr",
                        corrected=corrected != ocr_text,
                    )
                except Exception as exc:
                    if self.ocr_fail_on_page_error:
                        raise RuntimeError(
                            f"Unable to OCR scanned PDF page {page_number}: {exc}"
                        ) from exc
                    page_results[page_number - 1] = PdfPageExtraction(
                        page_number=page_number,
                        text="",
                        source="ocr",
                        error=str(exc),
                    )

        await asyncio.gather(*(recover_page(page_number) for page_number in empty_pages))
        final_pages = [page for page in page_results if page is not None]
        if not any(page.text.strip() for page in final_pages):
            raise ValueError(f"No extractable content found in PDF: {path}")

        self.last_metadata = {
            "artifact_type": "pdf-page-extraction",
            "page_count": page_count,
            "pages": [
                {
                    "page_number": page.page_number,
                    "source": page.source,
                    "chars": len(page.text),
                    "confidence": page.confidence,
                    "corrected": page.corrected,
                    "status": "failed" if page.error else "succeeded",
                    "error": page.error,
                }
                for page in final_pages
            ],
        }
        return render_pdf_pages_markdown(path, final_pages)

    def _extract_sync(self, path: Path) -> str:
        """Compatibility wrapper for callers that still use synchronous extraction."""
        _validate_input_size(path, self.max_input_bytes, "PDF extraction input")
        pages, page_count = self._extract_embedded_pages(path)
        if page_count > self.max_pages:
            raise ValueError(
                f"PDF has {page_count} pages, exceeding configured limit {self.max_pages}: {path}"
            )
        empty_pages = [page.page_number for page in pages if not page.text.strip()]
        if len(empty_pages) > self.ocr_pdf_max_pages:
            raise ValueError(
                f"PDF contains {len(empty_pages)} scanned/empty pages, exceeding OCR page "
                f"limit {self.ocr_pdf_max_pages}: {path}"
            )
        page_outputs = list(pages)
        for page_number in empty_pages:
            try:
                ocr_text = _ocr_pdf_page(
                    path,
                    page_number=page_number,
                    language=self.ocr_language,
                    timeout_seconds=self.ocr_timeout_seconds,
                    dpi=self.ocr_pdf_dpi,
                )
            except (RuntimeError, ValueError) as exc:
                raise RuntimeError(
                    f"Unable to OCR scanned PDF page {page_number}: {exc}"
                ) from exc
            page_outputs[page_number - 1] = PdfPageExtraction(
                page_number=page_number,
                text=ocr_text,
                source="ocr",
            )
        self.last_metadata = {
            "artifact_type": "pdf-page-extraction",
            "page_count": page_count,
            "pages": [
                {
                    "page_number": page.page_number,
                    "source": page.source,
                    "chars": len(page.text),
                    "confidence": page.confidence,
                    "corrected": page.corrected,
                    "status": "failed" if page.error else "succeeded",
                    "error": page.error,
                }
                for page in page_outputs
            ],
        }
        return render_pdf_pages_markdown(path, page_outputs)

    def _extract_embedded_pages(self, path: Path) -> tuple[list[PdfPageExtraction], int]:
        try:
            pdfplumber = importlib.import_module("pdfplumber")
        except ImportError as exc:
            raise RuntimeError("PDF support requires installing the 'pdf' extra") from exc

        pages: list[PdfPageExtraction] = []
        pdf_module = cast(Any, pdfplumber)
        with pdf_module.open(path) as pdf:
            page_count = len(pdf.pages)
            for page_number, page in enumerate(pdf.pages, start=1):
                page_text = cast(str | None, page.extract_text())
                if page_text and page_text.strip():
                    pages.append(
                        PdfPageExtraction(
                            page_number=page_number,
                            text=page_text,
                            source="embedded",
                        )
                    )
                else:
                    pages.append(
                        PdfPageExtraction(
                            page_number=page_number,
                            text="",
                            source="empty",
                        )
                    )
        return pages, page_count

    async def _correct_ocr_page(self, *, page_number: int, text: str) -> str:
        if self.ocr_correction_mode == "never":
            return text
        if self.ocr_correction_mode == "low-confidence":
            return text
        provider = self.ocr_correction_provider
        if provider is None:
            raise RuntimeError("LLM OCR correction is enabled but no LLM provider is configured")
        corrected = await provider.complete(
            system_prompt=OCR_CORRECTION_PROMPT,
            user_prompt=(
                f"Correct OCR text from PDF page {page_number}. "
                "Return only corrected text.\n\n"
                f"{text}"
            ),
            model=self.ocr_correction_model,
            max_tokens=8192,
            temperature=0.0,
        )
        clean = str(corrected).strip()
        if not clean:
            raise ValueError(f"LLM OCR correction returned empty text for page {page_number}")
        return clean


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
        ocr_correction_provider: OcrCorrectionProvider | None = None,
        ocr_correction_mode: OcrCorrectionMode = "always",
        ocr_correction_model: str = "mock-cleaner",
        ocr_page_concurrency: int = 2,
        ocr_fail_on_page_error: bool = True,
        text_max_input_bytes: int = 100 * 1024 * 1024,
        docx_max_input_bytes: int = 100 * 1024 * 1024,
        pdf_max_input_bytes: int = 200 * 1024 * 1024,
        pdf_max_pages: int = 1_000,
        universal_max_input_bytes: int = 50 * 1024 * 1024,
        universal_timeout_seconds: int = 120,
    ) -> None:
        extractors = [
            TextFamilyExtractor(max_input_bytes=text_max_input_bytes),
            DocxExtractor(max_input_bytes=docx_max_input_bytes),
            PdfExtractor(
                ocr_language=ocr_language,
                ocr_timeout_seconds=ocr_timeout_seconds,
                ocr_pdf_dpi=ocr_pdf_dpi,
                ocr_pdf_max_pages=ocr_pdf_max_pages,
                ocr_correction_provider=ocr_correction_provider,
                ocr_correction_mode=ocr_correction_mode,
                ocr_correction_model=ocr_correction_model,
                ocr_page_concurrency=ocr_page_concurrency,
                ocr_fail_on_page_error=ocr_fail_on_page_error,
                max_input_bytes=pdf_max_input_bytes,
                max_pages=pdf_max_pages,
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
        self.last_metadata: dict[str, object] | None = None

    async def extract(self, path: Path) -> str:
        extension = path.suffix.lower()
        extractor = self._extractors.get(extension)
        if extractor is None:
            raise ValueError(f"Unsupported file extension: {extension}")
        text = await extractor.extract(path)
        metadata = getattr(extractor, "last_metadata", None)
        self.last_metadata = metadata if isinstance(metadata, dict) else None
        return text


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


def _ocr_pdf_page(
    path: Path,
    *,
    page_number: int,
    language: str = "eng",
    timeout_seconds: int = 120,
    dpi: int = 200,
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
                first_page=page_number,
                last_page=page_number,
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
        raise ValueError(f"No OCR text found on PDF page {page_number}: {path}")
    return text


def render_pdf_pages_markdown(path: Path, pages: Sequence[PdfPageExtraction]) -> str:
    """Render ordered PDF page records as canonical Markdown."""
    title = path.stem.replace("_", " ").replace("-", " ").strip() or path.name
    lines = [
        "---",
        "generated_by: librarian",
        "artifact_type: pdf-page-extraction",
        f"source_file: {path.name}",
        f"page_count: {len(pages)}",
        "---",
        "",
        f"# {title}",
        "",
    ]
    for page in sorted(pages, key=lambda item: item.page_number):
        if not page.text.strip() and page.error:
            lines.extend(
                [
                    (
                        f"<!-- page: {page.page_number} source: {page.source} "
                        f"status: failed error: {page.error} -->"
                    ),
                    "",
                ]
            )
            continue
        lines.extend(
            [
                (
                    f"<!-- page: {page.page_number} source: {page.source} "
                    f"corrected: {str(page.corrected).lower()} -->"
                ),
                "",
                f"## Page {page.page_number}",
                "",
                page.text.strip(),
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


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
    _validate_input_size(path, max_input_bytes, "Broad format conversion input")


def _validate_input_size(path: Path, max_input_bytes: int, label: str) -> None:
    byte_size = path.stat().st_size
    if byte_size > max_input_bytes:
        raise ValueError(f"{label} exceeds {max_input_bytes} bytes: {path}")


def _validate_text_like(path: Path) -> None:
    with path.open("rb") as handle:
        sample = handle.read(4096)
    if b"\x00" in sample:
        raise ValueError(f"Text extraction input appears to be binary: {path}")


def _paragraph_text(paragraph: Any) -> str:
    text = str(getattr(paragraph, "text", "")).strip()
    if not text:
        return ""
    style_name = str(getattr(getattr(paragraph, "style", None), "name", "")).lower()
    if "list bullet" in style_name:
        return f"- {text}"
    if "list number" in style_name:
        return f"1. {text}"
    return text


def _table_text(table: Any) -> str:
    rows: list[str] = []
    for row in table.rows:
        cells = [_paragraph_text(cell) for cell in row.cells]
        rendered = " | ".join(cell for cell in cells if cell)
        if rendered:
            rows.append(rendered)
    return "\n".join(rows)
