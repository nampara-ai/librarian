"""Document conversion services."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class ConversionFormat(StrEnum):
    """Supported conversion output formats."""

    MARKDOWN = "md"
    TEXT = "txt"


class DirectoryOutputMode(StrEnum):
    """Batch conversion output placement."""

    NEW_DIRECTORY = "new-directory"
    ORIGINAL = "original"
    SUBDIRECTORY = "subdirectory"


class TextConverter(Protocol):
    """Port for source file conversion."""

    supported_extensions: frozenset[str]

    async def extract(self, path: Path) -> str: ...


@dataclass(frozen=True, slots=True)
class ConvertedDocument:
    """Converted file output."""

    source_path: Path
    output_path: Path
    format: ConversionFormat
    text: str


@dataclass(frozen=True, slots=True)
class BatchConversionItem:
    """One batch conversion result."""

    source_path: Path
    output_path: Path | None
    status: str
    error: str | None = None


@dataclass(frozen=True, slots=True)
class BatchConversionResult:
    """Directory conversion summary."""

    items: tuple[BatchConversionItem, ...]

    @property
    def converted(self) -> int:
        return sum(1 for item in self.items if item.status == "converted")

    @property
    def skipped(self) -> int:
        return sum(1 for item in self.items if item.status == "skipped")

    @property
    def failed(self) -> int:
        return sum(1 for item in self.items if item.status == "failed")


@dataclass(frozen=True, slots=True)
class DocumentConverter:
    """Convert arbitrary supported source files to Markdown or plain text."""

    extractor: TextConverter

    async def convert_file(
        self,
        source_path: Path,
        output_path: Path,
        *,
        format: ConversionFormat,
        overwrite: bool = False,
    ) -> ConvertedDocument:
        """Convert one file and write the rendered output."""
        output_exists = await asyncio.to_thread(output_path.exists)
        if output_exists and not overwrite:
            raise FileExistsError(f"Output already exists: {output_path}")
        text = await self.extractor.extract(source_path)
        rendered = render_conversion(text, source_path=source_path, format=format)
        await asyncio.to_thread(output_path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(output_path.write_text, rendered, encoding="utf-8")
        return ConvertedDocument(
            source_path=source_path,
            output_path=output_path,
            format=format,
            text=rendered,
        )

    async def convert_directory(
        self,
        source_dir: Path,
        *,
        format: ConversionFormat,
        output_mode: DirectoryOutputMode,
        output_dir: Path | None = None,
        subdirectory_name: str = "librarian-converted",
        recursive: bool = False,
        overwrite: bool = False,
    ) -> BatchConversionResult:
        """Convert supported files in a directory."""
        files = discover_supported_files(
            source_dir,
            supported_extensions=self.extractor.supported_extensions,
            recursive=recursive,
        )
        items: list[BatchConversionItem] = []
        for source_path in files:
            destination = conversion_output_path(
                source_path,
                source_dir=source_dir,
                format=format,
                output_mode=output_mode,
                output_dir=output_dir,
                subdirectory_name=subdirectory_name,
            )
            try:
                await self.convert_file(
                    source_path,
                    destination,
                    format=format,
                    overwrite=overwrite,
                )
            except Exception as exc:
                items.append(
                    BatchConversionItem(
                        source_path=source_path,
                        output_path=destination,
                        status="failed",
                        error=str(exc),
                    )
                )
                continue
            items.append(
                BatchConversionItem(
                    source_path=source_path,
                    output_path=destination,
                    status="converted",
                )
            )
        return BatchConversionResult(items=tuple(items))


def discover_supported_files(
    source_dir: Path,
    *,
    supported_extensions: frozenset[str],
    recursive: bool,
) -> list[Path]:
    """Find supported files in stable order."""
    pattern = "**/*" if recursive else "*"
    return sorted(
        (
            path
            for path in source_dir.glob(pattern)
            if path.is_file() and path.suffix.lower() in supported_extensions
        ),
        key=lambda item: str(item.relative_to(source_dir)).lower(),
    )


def conversion_output_path(
    source_path: Path,
    *,
    source_dir: Path,
    format: ConversionFormat,
    output_mode: DirectoryOutputMode,
    output_dir: Path | None,
    subdirectory_name: str,
) -> Path:
    """Resolve a converted output path."""
    suffix = ".md" if format == ConversionFormat.MARKDOWN else ".txt"
    relative = source_path.relative_to(source_dir).with_suffix(suffix)
    if output_mode == DirectoryOutputMode.ORIGINAL:
        return source_path.with_suffix(suffix)
    if output_mode == DirectoryOutputMode.SUBDIRECTORY:
        return source_dir / subdirectory_name / relative
    if output_dir is None:
        raise ValueError("--output-dir is required for new-directory mode")
    return output_dir / relative


def render_conversion(text: str, *, source_path: Path, format: ConversionFormat) -> str:
    """Render extracted content as Markdown or plain text."""
    normalized = text.strip()
    if format == ConversionFormat.TEXT:
        return markdown_to_text(normalized) + "\n"
    if source_path.suffix.lower() == ".md":
        return normalized + "\n"
    title = source_path.stem.replace("_", " ").replace("-", " ").strip() or source_path.name
    return f"# {title}\n\n{normalized}\n"


def markdown_to_text(markdown: str) -> str:
    """Convert simple Markdown to readable plain text without extra dependencies."""
    text = re.sub(r"```.*?```", "", markdown, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
