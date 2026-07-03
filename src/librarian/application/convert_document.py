"""Document conversion services."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

from librarian.observability import sanitize_error_message

_MAX_METADATA_BYTES = 64 * 1024
_LIBRARIAN_ARTIFACT_TYPES = frozenset(
    {"conversion-sidecar", "import-report", "pdf-page-extraction-manifest"}
)


class ConversionFormat(StrEnum):
    """Supported conversion output formats."""

    MARKDOWN = "md"
    TEXT = "txt"


class DirectoryOutputMode(StrEnum):
    """Batch conversion output placement."""

    NEW_DIRECTORY = "new-directory"
    ORIGINAL = "original"
    SUBDIRECTORY = "subdirectory"
    WORKSPACE = "workspace"


def workspace_output_dir(data_dir: Path, source_path: Path) -> Path:
    """Default converted-output directory inside the runtime workspace.

    Directory imports get a per-source subdirectory so unrelated imports do not
    collide; single-file imports share the flat converted root.
    """
    root = data_dir / "converted"
    if source_path.is_dir():
        return root / source_path.name
    return root


def resolve_workspace_output(
    output_mode: DirectoryOutputMode,
    *,
    data_dir: Path,
    source_path: Path,
    output_dir: Path | None,
) -> tuple[DirectoryOutputMode, Path | None]:
    """Translate workspace mode into a concrete new-directory target.

    Workspace mode owns its output location, so combining it with an explicit
    output_dir is rejected before any conversion starts.
    """
    if output_mode != DirectoryOutputMode.WORKSPACE:
        return output_mode, output_dir
    if output_dir is not None:
        raise ValueError("output_dir is only supported with new-directory output mode")
    return DirectoryOutputMode.NEW_DIRECTORY, workspace_output_dir(data_dir, source_path)


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


class ConversionMetrics(Protocol):
    """Metrics sink for conversion outcomes."""

    def record_conversion_failure(
        self,
        *,
        failure_type: str,
        source_extension: str,
    ) -> None: ...


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
    metrics: ConversionMetrics | None = None

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
        await asyncio.to_thread(_reject_symlinked_output_path, output_path)
        sidecar_path = output_path.with_suffix(f"{output_path.suffix}.json")
        if write_sidecar:
            await asyncio.to_thread(_reject_symlinked_output_path, sidecar_path)
        output_exists = await asyncio.to_thread(output_path.exists)
        if output_exists and not overwrite:
            raise FileExistsError(f"Output already exists: {output_path}")
        page_manifest_path = output_path.with_suffix(f"{output_path.suffix}.pages.json")
        set_page_manifest_path = getattr(self.extractor, "set_page_manifest_path", None)
        if callable(set_page_manifest_path):
            # Every convert_file call sets this explicitly (path or None), so the
            # value never leaks in from a prior extraction. Concurrent conversions
            # run in separate asyncio task contexts, so the underlying ContextVar
            # stays isolated per-conversion.
            set_page_manifest_path(page_manifest_path if write_sidecar else None)
        text = await self.extractor.extract(source_path)
        # Capture the extractor's per-run metadata immediately, before any further
        # await: the extractor instance is shared, so a concurrently-converting
        # file (parallel imports) would otherwise overwrite last_metadata and the
        # sidecar would record the wrong source provenance.
        metadata_obj = getattr(self.extractor, "last_metadata", None)
        metadata = cast(dict[str, object], metadata_obj) if isinstance(metadata_obj, dict) else None
        rendered = render_conversion(text, source_path=source_path, format=format)
        await asyncio.to_thread(output_path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(_write_text_atomic, output_path, rendered)
        if write_sidecar:
            await write_conversion_sidecar(
                source_path=source_path,
                output_path=output_path,
                format=format,
                text=rendered,
                extraction_metadata=metadata if isinstance(metadata, dict) else None,
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
            except Exception as exc:  # noqa: BLE001 - failure recorded per file
                self.record_conversion_failure(source_path, exc)
                items.append(
                    BatchConversionItem(
                        source_path=source_path,
                        output_path=destination,
                        status="failed",
                        error=sanitize_error_message(exc),
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

    def record_conversion_failure(self, source_path: Path, exc: Exception) -> None:
        """Record a classified conversion failure if metrics are configured."""
        if self.metrics is None:
            return
        self.metrics.record_conversion_failure(
            failure_type=classify_conversion_error(exc).value,
            source_extension=source_path.suffix.lower() or "<none>",
        )


def discover_supported_files(
    source_dir: Path,
    *,
    supported_extensions: frozenset[str],
    recursive: bool,
    allowed_root: Path | None = None,
    exclude_paths: tuple[Path, ...] = (),
) -> list[Path]:
    """Find supported files in stable order."""
    return sorted(
        iter_supported_files(
            source_dir,
            supported_extensions=supported_extensions,
            recursive=recursive,
            allowed_root=allowed_root,
            exclude_paths=exclude_paths,
        ),
        key=lambda item: str(item.relative_to(source_dir)).lower(),
    )


def iter_supported_files(
    source_dir: Path,
    *,
    supported_extensions: frozenset[str],
    recursive: bool,
    allowed_root: Path | None = None,
    exclude_paths: tuple[Path, ...] = (),
) -> Iterator[Path]:
    """Yield supported files without materializing the whole tree."""
    pattern = "**/*" if recursive else "*"
    excluded = tuple(path.resolve() for path in exclude_paths)
    resolved_allowed_root = allowed_root.resolve() if allowed_root else None
    for path in source_dir.glob(pattern):
        if not path.is_file() or path.suffix.lower() not in supported_extensions:
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if _is_under_any(resolved, excluded):
            continue
        if not _is_under_allowed_root(path, resolved_allowed_root):
            continue
        if _has_librarian_sidecar(path):
            continue
        yield path


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
    if path.suffix.lower() == ".json" and _is_librarian_metadata_file(
        path,
        allow_large_prefix=True,
    ):
        return True
    sidecar = path.with_suffix(f"{path.suffix}.json")
    return _is_librarian_metadata_file(sidecar)


def _is_librarian_metadata_file(path: Path, *, allow_large_prefix: bool = False) -> bool:
    try:
        stat = path.stat()
        if not path.is_file():
            return False
        if stat.st_size > _MAX_METADATA_BYTES:
            if not allow_large_prefix:
                return False
            prefix = _read_text_prefix(path, max_bytes=_MAX_METADATA_BYTES)
            return (
                '"generated_by": "librarian"' in prefix
                and any(
                    f'"artifact_type": "{kind}"' in prefix
                    for kind in _LIBRARIAN_ARTIFACT_TYPES
                )
            )
        payload_obj = json.loads(_read_limited_text_file(path, max_bytes=_MAX_METADATA_BYTES))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload_obj, dict):
        return False
    payload = cast(dict[str, object], payload_obj)
    return (
        payload.get("generated_by") == "librarian"
        and payload.get("artifact_type") in _LIBRARIAN_ARTIFACT_TYPES
    )


def _read_text_prefix(path: Path, *, max_bytes: int) -> str:
    with path.open("rb") as handle:
        return handle.read(max_bytes).decode("utf-8", errors="ignore")


def _read_limited_text_file(path: Path, *, max_bytes: int) -> str:
    with path.open("rb") as handle:
        payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"Metadata file exceeds {max_bytes} bytes: {path}")
    return payload.decode("utf-8")


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
    if source_path.suffix.lower() == ".pdf" and normalized.startswith("---\n"):
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
    extraction_metadata: dict[str, object] | None = None,
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
            "extraction": extraction_metadata,
        },
        indent=2,
    )
    sidecar_path = output_path.with_suffix(f"{output_path.suffix}.json")
    await asyncio.to_thread(_write_text_atomic, sidecar_path, payload)


def _write_text_atomic(path: Path, payload: str) -> None:
    _reject_symlinked_output_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary_path.write_text(payload, encoding="utf-8")
        temporary_path.replace(path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _reject_symlinked_output_path(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"Output path must not be a symlink: {path}")
    for parent in reversed(path.parents):
        if parent.exists() and parent.is_symlink():
            raise ValueError(f"Output path crosses symlinked parent: {path}")


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
    text = re.sub(r"^---\n.*?\n---\n?", "", text, flags=re.DOTALL)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
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
