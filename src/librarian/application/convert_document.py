"""Document conversion services."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

_MAX_METADATA_BYTES = 64 * 1024
_LIBRARIAN_ARTIFACT_TYPES = frozenset({"conversion-sidecar", "import-report"})


class ConversionFormat(StrEnum):
    """Supported conversion output formats."""

    MARKDOWN = "md"
    TEXT = "txt"


class DirectoryOutputMode(StrEnum):
    """Batch conversion output placement."""

    NEW_DIRECTORY = "new-directory"
    ORIGINAL = "original"
    SUBDIRECTORY = "subdirectory"


class ConversionFailureType(StrEnum):
    """Classified conversion failures."""

    DEPENDENCY_MISSING = "dependency_missing"
    EMPTY_OUTPUT = "empty_output"
    OUTPUT_EXISTS = "output_exists"
    UNSUPPORTED = "unsupported"
    EXTRACTION_FAILED = "extraction_failed"


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
    error_type: ConversionFailureType | None = None


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

    def to_json_dict(self) -> dict[str, object]:
        """Return a JSON-serializable result payload."""
        return {
            "summary": {
                "converted": self.converted,
                "skipped": self.skipped,
                "failed": self.failed,
            },
            "items": [
                {
                    "source_path": str(item.source_path),
                    "output_path": str(item.output_path) if item.output_path else None,
                    "status": item.status,
                    "error": item.error,
                    "error_type": item.error_type.value if item.error_type else None,
                }
                for item in self.items
            ],
        }


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
        write_sidecar: bool = False,
    ) -> ConvertedDocument:
        """Convert one file and write the rendered output."""
        output_exists = await asyncio.to_thread(output_path.exists)
        if output_exists and not overwrite:
            raise FileExistsError(f"Output already exists: {output_path}")
        text = await self.extractor.extract(source_path)
        rendered = render_conversion(text, source_path=source_path, format=format)
        await asyncio.to_thread(output_path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(output_path.write_text, rendered, encoding="utf-8")
        if write_sidecar:
            await write_conversion_sidecar(
                source_path=source_path,
                output_path=output_path,
                format=format,
                text=rendered,
            )
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
        write_sidecar: bool = False,
        allowed_root: Path | None = None,
    ) -> BatchConversionResult:
        """Convert supported files in a directory."""
        validate_directory_output(
            source_dir=source_dir,
            output_mode=output_mode,
            output_dir=output_dir,
        )
        files = discover_supported_files(
            source_dir,
            supported_extensions=self.extractor.supported_extensions,
            recursive=recursive,
            allowed_root=allowed_root,
            exclude_paths=conversion_output_exclusions(
                source_dir=source_dir,
                output_mode=output_mode,
                output_dir=output_dir,
                subdirectory_name=subdirectory_name,
            ),
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
            if not overwrite:
                destination = await unique_output_path(destination)
            try:
                await self.convert_file(
                    source_path,
                    destination,
                    format=format,
                    overwrite=overwrite,
                    write_sidecar=True,
                )
            except Exception as exc:
                items.append(
                    BatchConversionItem(
                        source_path=source_path,
                        output_path=destination,
                        status="failed",
                        error=str(exc),
                        error_type=classify_conversion_error(exc),
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
    allowed_root: Path | None = None,
    exclude_paths: tuple[Path, ...] = (),
) -> list[Path]:
    """Find supported files in stable order."""
    pattern = "**/*" if recursive else "*"
    excluded = tuple(path.resolve() for path in exclude_paths)
    resolved_allowed_root = allowed_root.resolve() if allowed_root else None
    return sorted(
        (
            path
            for path in source_dir.glob(pattern)
            if path.is_file()
            and path.suffix.lower() in supported_extensions
            and not _is_under_any(path.resolve(), excluded)
            and _is_under_allowed_root(path, resolved_allowed_root)
            and not _has_librarian_sidecar(path)
        ),
        key=lambda item: str(item.relative_to(source_dir)).lower(),
    )


def conversion_output_exclusions(
    *,
    source_dir: Path,
    output_mode: DirectoryOutputMode,
    output_dir: Path | None,
    subdirectory_name: str,
) -> tuple[Path, ...]:
    """Return output roots that should be skipped during recursive discovery."""
    if output_mode == DirectoryOutputMode.SUBDIRECTORY:
        return (source_dir / subdirectory_name,)
    if output_mode == DirectoryOutputMode.NEW_DIRECTORY and output_dir is not None:
        return (output_dir,)
    return ()


def validate_directory_output(
    *,
    source_dir: Path,
    output_mode: DirectoryOutputMode,
    output_dir: Path | None,
) -> None:
    """Validate directory conversion output placement."""
    if output_mode != DirectoryOutputMode.NEW_DIRECTORY:
        return
    if output_dir is None:
        raise ValueError("--output-dir is required for new-directory mode")
    try:
        source_dir.resolve().relative_to(output_dir.resolve())
    except ValueError:
        return
    raise ValueError("output_dir must not be source_dir or an ancestor of source_dir")


def _is_under_any(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def _is_under_allowed_root(path: Path, allowed_root: Path | None) -> bool:
    if allowed_root is None:
        return True
    try:
        resolved = path.resolve()
        resolved.relative_to(allowed_root)
    except (OSError, ValueError):
        return False
    return True


def _has_librarian_sidecar(path: Path) -> bool:
    if path.suffix.lower() == ".json" and _is_librarian_metadata_file(path):
        return True
    sidecar = path.with_suffix(f"{path.suffix}.json")
    return _is_librarian_metadata_file(sidecar)


def _is_librarian_metadata_file(path: Path) -> bool:
    try:
        stat = path.stat()
        if not path.is_file() or stat.st_size > _MAX_METADATA_BYTES:
            return False
        payload_obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload_obj, dict):
        return False
    payload = cast(dict[str, object], payload_obj)
    return (
        payload.get("generated_by") == "librarian"
        and payload.get("artifact_type") in _LIBRARIAN_ARTIFACT_TYPES
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


async def unique_output_path(path: Path) -> Path:
    """Return a non-colliding output path by appending a numeric suffix."""
    exists = await asyncio.to_thread(path.exists)
    if not exists:
        return path
    for index in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not await asyncio.to_thread(candidate.exists):
            return candidate
    raise FileExistsError(f"Could not find available output path for {path}")


async def write_conversion_sidecar(
    *,
    source_path: Path,
    output_path: Path,
    format: ConversionFormat,
    text: str,
) -> None:
    """Write sidecar conversion metadata."""
    payload = json.dumps(
        {
            "generated_by": "librarian",
            "artifact_type": "conversion-sidecar",
            "source_path": str(source_path),
            "output_path": str(output_path),
            "format": format.value,
            "output_chars": len(text),
        },
        indent=2,
    )
    sidecar_path = output_path.with_suffix(f"{output_path.suffix}.json")
    await asyncio.to_thread(sidecar_path.write_text, payload, encoding="utf-8")


def classify_conversion_error(exc: Exception) -> ConversionFailureType:
    """Classify conversion failures for reports and manifests."""
    message = str(exc).lower()
    if isinstance(exc, FileExistsError):
        return ConversionFailureType.OUTPUT_EXISTS
    if isinstance(exc, ValueError) and "unsupported file extension" in message:
        return ConversionFailureType.UNSUPPORTED
    if "requires installing" in message or "executable on path" in message:
        return ConversionFailureType.DEPENDENCY_MISSING
    if "no extractable" in message or "no ocr text" in message:
        return ConversionFailureType.EMPTY_OUTPUT
    return ConversionFailureType.EXTRACTION_FAILED


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
