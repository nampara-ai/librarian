"""Typer CLI adapter."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, cast

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from librarian.application.benchmark import (
    benchmark_result_json,
    load_benchmark_text,
    run_benchmark_suite,
)
from librarian.application.convert_document import (
    ConversionFormat,
    DirectoryOutputMode,
    DocumentConverter,
    validate_directory_output,
)
from librarian.application.corpus_eval import (
    corpus_eval_result_json,
    load_corpus_eval_suite,
    run_corpus_eval_suite,
)
from librarian.application.eval import eval_result_json, load_eval_suite, run_eval_suite
from librarian.application.export_document import ExportedDocument, ExportFormat
from librarian.application.factory import build_container, build_ingest_container
from librarian.application.import_library import (
    ImportLibrary,
    ImportProcessingMode,
    write_import_report,
)
from librarian.application.jobs import QueueWorker
from librarian.application.ports import SearchScope
from librarian.application.synthetic_corpus import generate_synthetic_corpus
from librarian.application.transcripts import (
    TranscriptFormat,
    find_quote_in_transcript_file,
    format_compact_timestamp,
    normalize_transcript_file,
    transcript_match_json,
)
from librarian.config import Settings
from librarian.domain.ids import DocumentId, RunId, digest_text
from librarian.domain.models import DocumentStatus, RunStage, RunStatus
from librarian.ingest.extractors import CompositeExtractor
from librarian.llm import LazyLLMProvider
from librarian.observability import sanitize_error_message
from librarian.pipeline.chunking import ChunkingPolicy, chunk_text
from librarian.storage.sqlite import SQLiteDatabase, SQLiteRunQueue
from librarian.version import __version__

app = typer.Typer(no_args_is_help=True)
console = Console()
_DEFAULT_WORKSPACE_RESTORE_MAX_EXPANDED_BYTES = 10 * 1024 * 1024 * 1024
_MAX_WORKSPACE_BACKUP_MANIFEST_BYTES = 64 * 1024
_MAX_WORKSPACE_BACKUP_MEMBERS = 100_000
_MAX_PAGE_MANIFEST_READ_BYTES = 256 * 1024 * 1024


@app.command()
def version() -> None:
    """Print the Librarian version."""
    console.print(__version__)


@app.command()
def doctor(
    strict: Annotated[
        bool,
        typer.Option(help="Exit non-zero when optional conversion dependencies are missing."),
    ] = False,
) -> None:
    """Check local runtime dependencies and conversion tools."""
    settings = Settings()
    checks = [
        ("Python", sys.version.split()[0], "ok", sys.executable),
        ("Data directory", "configured", "ok", str(settings.data_dir)),
        ("SQLite database", "configured", "ok", str(settings.database_path)),
        *_doctor_module_checks(),
        *_doctor_tool_checks(),
    ]
    table = Table("Check", "Capability", "Status", "Detail")
    for name, capability, status, detail in checks:
        table.add_row(name, capability, status, detail)
    console.print(table)
    if strict and any(status == "missing" for _, _, status, _ in checks):
        raise typer.Exit(1)


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Workspace path to initialize.")] = Path("."),
) -> None:
    """Initialize a local Librarian workspace."""
    settings = Settings()
    root = path.resolve()
    data_dir = root / settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "content").mkdir(exist_ok=True)
    config_path = data_dir / "config.json"
    _write_cli_output_atomic(
        config_path,
        json.dumps(
            {
                "database_path": str(root / settings.database_path),
                "data_dir": str(data_dir),
                "llm_provider": settings.llm_provider,
                "llm_model": settings.llm_model,
            },
            indent=2,
        ),
    )
    asyncio.run(SQLiteDatabase(root / settings.database_path).initialize())
    console.print(f"Initialized Librarian workspace at {data_dir}")


@app.command()
def migrate() -> None:
    """Apply database migrations."""
    settings = Settings()
    asyncio.run(SQLiteDatabase(settings.database_path).initialize())
    console.print(f"Applied migrations to {settings.database_path}")


@app.command("db-maintain")
def db_maintain(
    vacuum: Annotated[
        bool,
        typer.Option(help="Also run VACUUM after checkpoint/optimize."),
    ] = False,
) -> None:
    """Run SQLite optimize, WAL checkpoint, and optional vacuum maintenance."""
    settings = Settings()

    async def run() -> None:
        database = SQLiteDatabase(settings.database_path)
        await database.initialize()
        result = await database.maintain(vacuum=vacuum)
        console.print(
            "SQLite maintenance complete: "
            f"busy={result.checkpoint_busy}, "
            f"log_frames={result.checkpoint_log_frames}, "
            f"checkpointed={result.checkpoint_checkpointed_frames}, "
            f"vacuumed={result.vacuumed}"
        )

    asyncio.run(run())


@app.command("db-check")
def db_check() -> None:
    """Verify SQLite integrity, foreign keys, and migrations."""
    settings = Settings()

    async def run() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            result = await database.verify()
        except FileNotFoundError as exc:
            console.print(sanitize_error_message(exc))
            raise typer.Exit(1) from exc
        console.print(
            "SQLite verification complete: "
            f"integrity_ok={result.integrity_ok}, "
            f"foreign_key_violations={result.foreign_key_violations}, "
            f"applied_migrations={result.applied_migrations}"
        )
        if not result.ok:
            raise typer.Exit(1)

    asyncio.run(run())


@app.command("db-stats")
def db_stats(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable storage sizing details."),
    ] = False,
) -> None:
    """Show SQLite file size, page usage, row counts, and stored text totals."""
    settings = Settings()

    async def run() -> None:
        database = SQLiteDatabase(settings.database_path)
        await database.initialize()
        result = await database.stats()
        payload = {
            "database_path": str(result.database_path),
            "database_file_bytes": result.database_file_bytes,
            "wal_file_bytes": result.wal_file_bytes,
            "shm_file_bytes": result.shm_file_bytes,
            "total_sqlite_bytes": result.total_sqlite_bytes,
            "page_size_bytes": result.page_size_bytes,
            "page_count": result.page_count,
            "freelist_count": result.freelist_count,
            "used_page_bytes": result.used_page_bytes,
            "free_page_bytes": result.free_page_bytes,
            "table_counts": result.table_counts,
            "source_file_bytes": result.source_file_bytes,
            "stored_text_bytes": {
                "content_blobs": result.content_blob_text_bytes,
                "chunks": result.chunk_text_bytes,
                "cleaned_chunks": result.cleaned_chunk_text_bytes,
                "cleaned_chunk_cache": result.cleaned_cache_text_bytes,
                "cleaned_outputs": result.cleaned_output_text_bytes,
            },
        }
        if json_output:
            console.out(json.dumps(payload, indent=2, sort_keys=True))
            return
        console.print(f"SQLite database: {result.database_path}")
        console.print(
            "Files: "
            f"database={result.database_file_bytes:,} bytes, "
            f"wal={result.wal_file_bytes:,} bytes, "
            f"shm={result.shm_file_bytes:,} bytes, "
            f"total={result.total_sqlite_bytes:,} bytes"
        )
        console.print(
            "Pages: "
            f"size={result.page_size_bytes:,} bytes, "
            f"count={result.page_count:,}, "
            f"freelist={result.freelist_count:,}, "
            f"used={result.used_page_bytes:,} bytes, "
            f"free={result.free_page_bytes:,} bytes"
        )
        console.print(f"Source file bytes: {result.source_file_bytes:,}")
        console.print(
            "Stored text bytes: "
            f"raw/content={result.content_blob_text_bytes:,}, "
            f"chunks={result.chunk_text_bytes:,}, "
            f"cleaned_chunks={result.cleaned_chunk_text_bytes:,}, "
            f"cache={result.cleaned_cache_text_bytes:,}, "
            f"cleaned_outputs={result.cleaned_output_text_bytes:,}"
        )
        table = Table("Table", "Rows")
        for table_name, count in sorted(result.table_counts.items()):
            table.add_row(table_name, f"{count:,}")
        console.print(table)

    asyncio.run(run())


@app.command("api-audit")
def api_audit(
    limit: Annotated[
        int,
        typer.Option(help="Maximum audit events to print.", min=1, max=1_000),
    ] = 100,
    offset: Annotated[int, typer.Option(help="Audit events to skip.", min=0)] = 0,
    event: Annotated[
        str | None,
        typer.Option(help="Restrict to one audit event type."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print machine-readable audit events."),
    ] = False,
) -> None:
    """List durable API security audit events."""
    settings = Settings()

    async def run() -> None:
        database = SQLiteDatabase(settings.database_path)
        await database.initialize()
        rows = await asyncio.to_thread(
            _read_api_audit_events,
            database,
            limit,
            offset,
            event,
        )
        if json_output:
            console.out(json.dumps({"events": rows, "limit": limit, "offset": offset}, indent=2))
            return
        table = Table(
            "ID",
            "Created",
            "Event",
            "Method",
            "Path",
            "Client",
            "Credential",
            "Retry After",
        )
        for row in rows:
            table.add_row(
                str(row["id"]),
                str(row["created_at"]),
                str(row["event"]),
                str(row["method"]),
                str(row["path"]),
                str(row["client_host"]),
                _audit_credential_summary(row),
                str(row["retry_after_seconds"] or ""),
            )
        console.print(table)

    asyncio.run(run())


@app.command("db-backup")
def db_backup(
    output: Annotated[Path, typer.Argument(help="Destination SQLite backup path.")],
    overwrite: Annotated[
        bool,
        typer.Option(help="Overwrite an existing backup file."),
    ] = False,
) -> None:
    """Create a consistent online SQLite database backup."""
    settings = Settings()

    async def run() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            result = await database.backup(output, overwrite=overwrite)
        except (FileExistsError, FileNotFoundError, ValueError) as exc:
            console.print(sanitize_error_message(exc))
            raise typer.Exit(1) from exc
        console.print(
            "SQLite backup complete: "
            f"{result.source_path} -> {result.destination_path} "
            f"({result.byte_size} bytes)"
        )

    asyncio.run(run())


@app.command("db-restore")
def db_restore(
    backup: Annotated[Path, typer.Argument(help="Source SQLite backup path.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm replacing the configured database."),
    ] = False,
) -> None:
    """Restore the configured SQLite database from a verified backup."""
    if not yes:
        console.print("Refusing to restore without --yes; this replaces the configured database.")
        raise typer.Exit(1)
    settings = Settings()

    async def run() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            result = await database.restore(backup)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            console.print(sanitize_error_message(exc))
            raise typer.Exit(1) from exc
        console.print(
            "SQLite restore complete: "
            f"{result.source_path} -> {result.destination_path} "
            f"({result.byte_size} bytes)"
        )

    asyncio.run(run())


@app.command("workspace-backup")
def workspace_backup(
    output: Annotated[Path, typer.Argument(help="Destination workspace .zip path.")],
    overwrite: Annotated[
        bool,
        typer.Option(help="Overwrite an existing workspace archive."),
    ] = False,
) -> None:
    """Create a workspace archive with data files and a consistent SQLite backup."""
    settings = Settings()
    expanded_output = output.expanduser()
    _reject_symlinked_cli_output_path(expanded_output)
    output_path = expanded_output.resolve()
    if output_path.exists() and not overwrite:
        console.print(f"Workspace backup destination already exists: {output_path}")
        raise typer.Exit(1)

    async def run() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                db_backup_path = Path(tmp_dir) / "librarian.sqlite"
                db_backup = await database.backup(db_backup_path, overwrite=True)
                file_count = _write_workspace_backup_archive(
                    output_path,
                    settings=settings,
                    db_backup_path=db_backup.destination_path,
                )
        except (FileExistsError, FileNotFoundError, RuntimeError, ValueError) as exc:
            console.print(sanitize_error_message(exc))
            raise typer.Exit(1) from exc
        console.print(
            "Workspace backup complete: "
            f"{output_path} ({file_count} file(s), {output_path.stat().st_size} bytes)"
        )

    asyncio.run(run())


@app.command("workspace-restore")
def workspace_restore(
    archive: Annotated[Path, typer.Argument(help="Source workspace .zip archive.")],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Confirm replacing workspace data and database files."),
    ] = False,
    max_expanded_bytes: Annotated[
        int,
        typer.Option(help="Maximum total uncompressed archive bytes to restore."),
    ] = _DEFAULT_WORKSPACE_RESTORE_MAX_EXPANDED_BYTES,
) -> None:
    """Restore data files and SQLite database from a workspace archive."""
    if not yes:
        console.print("Refusing to restore workspace without --yes; this replaces local data.")
        raise typer.Exit(1)
    if max_expanded_bytes <= 0:
        raise typer.BadParameter("max-expanded-bytes must be greater than 0")
    settings = Settings()

    async def run() -> None:
        database = SQLiteDatabase(settings.database_path)
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                archive_path = archive.expanduser().resolve()
                db_restore_source, database_archive_path = _extract_workspace_database_snapshot(
                    archive.expanduser().resolve(),
                    temporary_dir=Path(tmp_dir),
                    max_expanded_bytes=max_expanded_bytes,
                )
                result = await database.restore(db_restore_source)
                _restore_workspace_data_files(
                    archive_path,
                    settings=settings,
                    database_archive_path=database_archive_path,
                    max_expanded_bytes=max_expanded_bytes,
                )
        except (FileNotFoundError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
            console.print(sanitize_error_message(exc))
            raise typer.Exit(1) from exc
        console.print(
            "Workspace restore complete: "
            f"{archive.expanduser().resolve()} -> {settings.data_dir.resolve()} "
            f"({result.byte_size} database bytes)"
        )

    asyncio.run(run())


@app.command()
def chunk(
    path: Annotated[Path, typer.Argument(exists=True, readable=True, dir_okay=False)],
    target_chars: Annotated[int, typer.Option(help="Target chunk size in characters.")] = 12_000,
    overlap_chars: Annotated[int, typer.Option(help="Chunk overlap in characters.")] = 800,
) -> None:
    """Extract and chunk a document without calling an LLM."""
    resolved_path = path.resolve()

    async def run() -> None:
        settings = Settings()
        extractor = _build_extractor(settings)
        text = await extractor.extract(path)
        document_id = DocumentId(digest_text("doc", str(resolved_path)))
        chunks = chunk_text(
            document_id,
            text,
            ChunkingPolicy(target_chars=target_chars, overlap_chars=overlap_chars),
        )
        console.print(f"{len(chunks)} chunk(s)")
        for item in chunks:
            console.print(
                f"{item.ordinal:>4} {item.id} {item.start_char}-{item.end_char} "
                f"{len(item.text):>7} chars"
            )

    asyncio.run(run())


@app.command()
def convert(
    path: Annotated[Path, typer.Argument(exists=True, readable=True, dir_okay=False)],
    output: Annotated[Path, typer.Option(help="Output .md or .txt path.")],
    format: Annotated[str, typer.Option(help="Output format: md or txt.")] = "md",
    overwrite: Annotated[bool, typer.Option(help="Overwrite existing output.")] = False,
    sidecar_metadata: Annotated[
        bool,
        typer.Option(help="Write .json sidecar metadata next to converted output."),
    ] = False,
) -> None:
    """Convert one source file to Markdown or plain text."""
    conversion_format = _conversion_format(format)

    async def run() -> None:
        settings = Settings()
        converter = DocumentConverter(_build_extractor(settings))
        result = await converter.convert_file(
            path.resolve(),
            output.resolve(),
            format=conversion_format,
            overwrite=overwrite,
            write_sidecar=sidecar_metadata,
        )
        console.print(f"Converted {result.source_path} -> {result.output_path}")

    asyncio.run(run())


@app.command("convert-dir")
def convert_dir(
    path: Annotated[Path, typer.Argument(exists=True, readable=True, file_okay=False)],
    format: Annotated[str, typer.Option(help="Output format: md or txt.")] = "md",
    output_mode: Annotated[
        str,
        typer.Option(help="Output mode: new-directory, original, subdirectory."),
    ] = "subdirectory",
    output_dir: Annotated[
        Path | None,
        typer.Option(help="Target directory for new-directory mode."),
    ] = None,
    subdirectory_name: Annotated[
        str,
        typer.Option(help="Subdirectory name for subdirectory mode."),
    ] = "librarian-converted",
    recursive: Annotated[bool, typer.Option(help="Recurse into child directories.")] = False,
    overwrite: Annotated[bool, typer.Option(help="Overwrite existing outputs.")] = False,
    sidecar_metadata: Annotated[
        bool,
        typer.Option(help="Deprecated; batch conversion always writes provenance sidecars."),
    ] = False,
) -> None:
    """Batch convert supported files in a directory."""
    conversion_format = _conversion_format(format)
    mode = _directory_output_mode(output_mode)
    _validate_cli_directory_output(
        path.resolve(),
        mode,
        output_dir.expanduser() if output_dir else None,
    )

    async def run() -> None:
        settings = Settings()
        converter = DocumentConverter(_build_extractor(settings))
        result = await converter.convert_directory(
            path.resolve(),
            format=conversion_format,
            output_mode=mode,
            output_dir=output_dir.expanduser() if output_dir else None,
            subdirectory_name=subdirectory_name,
            recursive=recursive,
            overwrite=overwrite,
            write_sidecar=sidecar_metadata,
        )
        table = Table("Status", "Source", "Output", "Error")
        for item in result.items:
            table.add_row(
                item.status,
                str(item.source_path),
                str(item.output_path) if item.output_path else "",
                item.error or "",
            )
        console.print(table)
        console.print(
            f"Converted {result.converted}, skipped {result.skipped}, failed {result.failed}"
        )
        if result.failed:
            raise typer.Exit(code=1)

    asyncio.run(run())


@app.command("transcript-normalize")
def transcript_normalize(
    path: Annotated[Path, typer.Argument(exists=True, readable=True, dir_okay=False)],
    output: Annotated[Path, typer.Option(help="Output transcript path.")],
    format: Annotated[
        str,
        typer.Option(help="Output format: md, txt, srt, vtt, or csv."),
    ] = "md",
    merge_sentences: Annotated[
        bool,
        typer.Option(help="Merge short timestamp segments into sentence-like spans."),
    ] = True,
    overwrite: Annotated[bool, typer.Option(help="Overwrite existing output.")] = False,
) -> None:
    """Normalize a timestamped transcript without calling an LLM."""
    transcript_format = _transcript_format(format)
    try:
        segment_count = normalize_transcript_file(
            path.resolve(),
            output.resolve(),
            format=transcript_format,
            merge_sentences=merge_sentences,
            overwrite=overwrite,
        )
    except (FileExistsError, OSError, UnicodeDecodeError, ValueError) as exc:
        console.print(sanitize_error_message(exc))
        raise typer.Exit(1) from exc
    console.print(f"Normalized {segment_count} transcript segment(s) -> {output.resolve()}")


@app.command("transcript-find")
def transcript_find(
    path: Annotated[Path, typer.Argument(exists=True, readable=True, dir_okay=False)],
    quote: Annotated[str, typer.Argument(help="Quote or phrase to locate.")],
    min_confidence: Annotated[
        float,
        typer.Option(help="Minimum fuzzy-match confidence.", min=0.0, max=1.0),
    ] = 0.78,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Render match evidence as JSON."),
    ] = False,
) -> None:
    """Find a quote in a timestamped transcript and print source evidence."""
    try:
        match = find_quote_in_transcript_file(
            path.resolve(),
            quote,
            min_confidence=min_confidence,
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        console.print(sanitize_error_message(exc))
        raise typer.Exit(1) from exc
    if match is None:
        console.print("No transcript quote match found")
        raise typer.Exit(1)
    if json_output:
        console.print(transcript_match_json(match))
        return
    table = Table("Start", "End", "Strategy", "Confidence", "Matched Text")
    table.add_row(
        format_compact_timestamp(match.start_seconds),
        format_compact_timestamp(match.end_seconds),
        match.strategy,
        f"{match.confidence:.3f}",
        match.matched_text,
    )
    console.print(table)


@app.command("import")
def import_directory(
    path: Annotated[Path, typer.Argument(exists=True, readable=True)],
    format: Annotated[str, typer.Option(help="Converted output format: md or txt.")] = "md",
    output_mode: Annotated[
        str,
        typer.Option(help="Output mode: new-directory, original, subdirectory."),
    ] = "subdirectory",
    output_dir: Annotated[
        Path | None,
        typer.Option(help="Target directory for new-directory mode."),
    ] = None,
    subdirectory_name: Annotated[
        str,
        typer.Option(help="Subdirectory name for subdirectory mode."),
    ] = "librarian-converted",
    recursive: Annotated[bool, typer.Option(help="Recurse into child directories.")] = False,
    overwrite: Annotated[bool, typer.Option(help="Overwrite existing converted outputs.")] = False,
    process: Annotated[bool, typer.Option(help="Process each document immediately.")] = False,
    queue: Annotated[
        bool,
        typer.Option(help="Enqueue each document for worker processing."),
    ] = False,
    manifest: Annotated[
        Path | None,
        typer.Option(help="JSON manifest path for import progress/resume."),
    ] = None,
    resume: Annotated[bool, typer.Option(help="Resume from an existing manifest.")] = False,
    report: Annotated[Path | None, typer.Option(help="Write final JSON report.")] = None,
    sidecar_metadata: Annotated[
        bool,
        typer.Option(help="Deprecated; batch import always writes provenance sidecars."),
    ] = False,
) -> None:
    """Convert a file or directory, ingest outputs, and optionally process or enqueue."""
    if process and queue:
        raise typer.BadParameter("Choose only one of --process or --queue")
    conversion_format = _conversion_format(format)
    mode = _directory_output_mode(output_mode)
    resolved_path = path.resolve()
    source_dir = resolved_path if resolved_path.is_dir() else resolved_path.parent
    _validate_cli_directory_output(
        source_dir,
        mode,
        output_dir.expanduser() if output_dir else None,
    )
    processing_mode = ImportProcessingMode.NONE
    if process:
        processing_mode = ImportProcessingMode.PROCESS
    elif queue:
        processing_mode = ImportProcessingMode.QUEUE

    async def run() -> None:
        container = (
            await build_ingest_container()
            if processing_mode == ImportProcessingMode.NONE
            else await build_container()
        )
        importer = ImportLibrary(
            converter=DocumentConverter(_build_extractor(container.settings)),
            ingest=container.ingest_document,
            process=getattr(container, "process_document", None),
            queue_factory=lambda: SQLiteRunQueue(container.database),
            manifest_max_bytes=container.settings.api_max_import_manifest_bytes,
        )
        try:
            result = await importer.import_path(
                resolved_path,
                format=conversion_format,
                output_mode=mode,
                processing_mode=processing_mode,
                output_dir=output_dir.expanduser() if output_dir else None,
                subdirectory_name=subdirectory_name,
                recursive=recursive,
                overwrite=overwrite,
                manifest_path=manifest.expanduser() if manifest else None,
                resume=resume,
                write_sidecar=sidecar_metadata,
            )
        except ValueError as exc:
            raise typer.BadParameter(sanitize_error_message(exc)) from exc
        if report:
            try:
                await write_import_report(report.expanduser(), result)
            except ValueError as exc:
                raise typer.BadParameter(sanitize_error_message(exc)) from exc
        table = Table("Status", "Source", "Converted", "Document", "Run", "Error")
        for item in result.items:
            table.add_row(
                item.status,
                str(item.source_path),
                str(item.converted_path) if item.converted_path else "",
                str(item.document_id) if item.document_id else "",
                str(item.run_id) if item.run_id else "",
                item.error or "",
            )
        console.print(table)
        console.print(
            "Converted "
            f"{result.converted}, ingested {result.ingested}, processed {result.processed}, "
            f"queued {result.queued}, skipped {result.skipped}, failed {result.failed}"
        )
        if result.failed:
            raise typer.Exit(code=1)

    asyncio.run(run())


@app.command("runs")
def list_runs(
    limit: Annotated[int, typer.Option(help="Maximum runs.", min=1, max=500)] = 100,
    offset: Annotated[int, typer.Option(help="Runs to skip.", min=0)] = 0,
) -> None:
    """List processing runs."""

    async def run() -> None:
        container = await build_ingest_container()
        runs = await container.repository.list_runs(limit=limit, offset=offset)
        table = Table("ID", "Document", "Status", "Stage", "Chunks", "Error")
        for item in runs:
            table.add_row(
                str(item.id),
                str(item.document_id),
                item.status.value,
                item.stage.value,
                f"{item.completed_chunks}/{item.total_chunks}",
                item.error or "",
            )
        console.print(table)

    asyncio.run(run())


@app.command("run-cancel")
def cancel_run(
    run_id: Annotated[str, typer.Argument(help="Run ID to cancel.")],
) -> None:
    """Mark a queued or running run as canceled."""

    async def run() -> None:
        container = await build_ingest_container()
        existing = await container.repository.get_run(RunId(run_id))
        if existing is None:
            raise typer.BadParameter(f"Run not found: {run_id}")
        if existing.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELED}:
            raise typer.BadParameter(f"Run is terminal and cannot be canceled: {run_id}")
        await container.repository.update_status(
            existing.id,
            status=RunStatus.CANCELED,
            stage=RunStage.COMPLETE,
            error="canceled by user",
        )
        if container.settings.job_backend == "sqlite":
            await SQLiteRunQueue(container.database).cancel(existing.id, error="canceled by user")
        console.print(f"Canceled {existing.id}")

    asyncio.run(run())


@app.command("run-retry")
def retry_run(
    run_id: Annotated[str, typer.Argument(help="Failed run ID to retry.")],
    queue: Annotated[bool, typer.Option(help="Enqueue retry instead of processing now.")] = False,
) -> None:
    """Replay a failed run as a new processing run."""

    async def run() -> None:
        container = await build_container()
        existing = await container.repository.get_run(RunId(run_id))
        if existing is None:
            raise typer.BadParameter(f"Run not found: {run_id}")
        if existing.status != RunStatus.FAILED:
            raise typer.BadParameter(f"Run is not failed: {run_id}")
        new_run = await container.process_document.start(existing.document_id)
        if queue:
            try:
                await SQLiteRunQueue(container.database).enqueue(new_run.id)
            except Exception as exc:
                error = sanitize_error_message(exc)
                await container.repository.update_status(
                    new_run.id,
                    status=RunStatus.FAILED,
                    stage=RunStage.COMPLETE,
                    error=f"submission failed: {error}",
                )
                raise typer.BadParameter(
                    f"Failed to enqueue retry {new_run.id}: {error}"
                ) from exc
            console.print(f"Queued retry {new_run.id}")
            return
        finished = await container.process_document.execute_existing(new_run.id)
        console.print(f"Retry {finished.id}: {finished.status.value}")

    asyncio.run(run())


@app.command("queue")
def inspect_queue(
    limit: Annotated[int, typer.Option(help="Maximum queue rows.", min=1, max=500)] = 100,
    offset: Annotated[int, typer.Option(help="Queue rows to skip.", min=0)] = 0,
) -> None:
    """List durable queue items."""

    async def run() -> None:
        container = await build_ingest_container()
        rows = await SQLiteRunQueue(container.database).list(limit=limit, offset=offset)
        table = Table("Run", "Status", "Attempts", "Available", "Locked By", "Error")
        for item in rows:
            table.add_row(
                str(item.run_id),
                item.status.value,
                str(item.attempts),
                item.available_at.isoformat(),
                item.locked_by or "",
                item.last_error or "",
            )
        console.print(table)

    asyncio.run(run())


@app.command()
def ingest(
    path: Annotated[Path, typer.Argument(exists=True, readable=True, dir_okay=False)],
) -> None:
    """Ingest a source file and persist extracted text."""
    resolved_path = path.resolve()

    async def run() -> None:
        container = await build_ingest_container()
        result = await container.ingest_document.execute(resolved_path)
        console.print(f"Ingested {result.document.id}")
        console.print(f"Source: {result.document.source.filename}")
        console.print(f"Extracted: {len(result.raw_text):,} chars")

    asyncio.run(run())


@app.command()
def process(
    document_id: Annotated[str, typer.Argument(help="Document ID to process.")],
) -> None:
    """Process an ingested document."""

    async def run() -> None:
        container = await build_container()
        result = await container.process_document.execute(DocumentId(document_id))
        console.print(f"Run {result.id}: {result.status.value}")

    asyncio.run(run())


@app.command()
def worker(
    once: Annotated[bool, typer.Option(help="Process at most one queued run.")] = False,
    worker_id: Annotated[str | None, typer.Option(help="Stable worker identifier.")] = None,
    poll_interval: Annotated[float, typer.Option(help="Queue poll interval in seconds.")] = 1.0,
) -> None:
    """Run an external SQLite queue worker."""
    settings = Settings()

    async def run() -> None:
        container = await build_container(settings)
        queue = SQLiteRunQueue(container.database)
        worker_runner = QueueWorker(
            queue=queue,
            processor=container.process_document.execute_existing,
            worker_id=worker_id or settings.job_worker_id,
            lease_seconds=settings.job_lease_seconds,
            max_attempts=settings.job_max_attempts,
            poll_interval_seconds=poll_interval,
        )
        if once:
            did_work = await worker_runner.run_once()
            console.print("processed one run" if did_work else "no queued runs")
            return
        await worker_runner.run_forever()

    asyncio.run(run())


@app.command("list")
def list_documents(
    limit: Annotated[int, typer.Option(help="Maximum documents.", min=1, max=500)] = 100,
    offset: Annotated[int, typer.Option(help="Documents to skip.", min=0)] = 0,
) -> None:
    """List ingested documents."""

    async def run() -> None:
        container = await build_ingest_container()
        documents = await container.repository.list(limit=limit, offset=offset)
        table = Table("ID", "Status", "Filename", "Bytes")
        for document in documents:
            table.add_row(
                str(document.id),
                document.status.value,
                document.source.filename,
                str(document.source.byte_size),
            )
        console.print(table)

    asyncio.run(run())


@app.command()
def show(
    document_id: Annotated[str, typer.Argument(help="Document ID to inspect.")],
) -> None:
    """Show document metadata and latest output summary."""

    async def run() -> None:
        container = await build_ingest_container()
        document = await container.repository.get_document(DocumentId(document_id))
        if document is None:
            raise typer.BadParameter(f"Document not found: {document_id}")
        output = await container.repository.get_cleaned_output(DocumentId(document_id))
        classification = await container.repository.get_classification(DocumentId(document_id))
        console.print(f"ID: {document.id}")
        console.print(f"Status: {document.status.value}")
        console.print(f"Source: {document.source.path}")
        if classification:
            console.print(f"Classification: {classification.code} - {classification.label}")
        if output:
            console.print(f"Cleaned chars: {len(output.text):,}")

    asyncio.run(run())


@app.command()
def status(
    run_id: Annotated[str, typer.Argument(help="Run ID to inspect.")],
    event_limit: Annotated[
        int,
        typer.Option(help="Maximum run events to print.", min=1, max=1_000),
    ] = 500,
    event_offset: Annotated[
        int,
        typer.Option(help="Run events to skip.", min=0),
    ] = 0,
) -> None:
    """Show processing run status and events."""

    async def run() -> None:
        container = await build_ingest_container()
        run_record = await container.repository.get_run(RunId(run_id))
        if run_record is None:
            raise typer.BadParameter(f"Run not found: {run_id}")
        console.print(f"Run {run_record.id}: {run_record.status.value} ({run_record.stage.value})")
        for event in await container.repository.list_events(
            run_record.id,
            limit=event_limit,
            offset=event_offset,
        ):
            console.print(event)

    asyncio.run(run())


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search query.")],
    limit: Annotated[int, typer.Option(help="Maximum results.", min=1, max=500)] = 20,
    offset: Annotated[int, typer.Option(help="Results to skip.", min=0)] = 0,
    details: Annotated[
        bool,
        typer.Option(help="Show ranked snippets instead of document IDs only."),
    ] = False,
    phrase: Annotated[
        bool,
        typer.Option(help="Treat the query as one exact adjacent phrase."),
    ] = False,
    classification_code: Annotated[
        str | None,
        typer.Option(help="Restrict results to one Dewey classification code."),
    ] = None,
    classification_prefix: Annotated[
        str | None,
        typer.Option(help="Restrict results to Dewey classification codes with this prefix."),
    ] = None,
    document_status: Annotated[
        str | None,
        typer.Option(help="Restrict results to one document status."),
    ] = None,
    filename_contains: Annotated[
        str | None,
        typer.Option(help="Restrict results to source filenames containing this text."),
    ] = None,
    created_after: Annotated[
        str | None,
        typer.Option(help="Restrict to documents created at or after this ISO timestamp."),
    ] = None,
    created_before: Annotated[
        str | None,
        typer.Option(help="Restrict to documents created at or before this ISO timestamp."),
    ] = None,
    scope: Annotated[
        str,
        typer.Option(help="Search cleaned outputs or raw extracted source text."),
    ] = "cleaned",
) -> None:
    """Search cleaned outputs."""

    async def run() -> None:
        container = await build_ingest_container()
        status_filter = _document_status_filter(document_status)
        search_scope = _search_scope_filter(scope)
        created_after_filter = _datetime_filter(created_after, option_name="created-after")
        created_before_filter = _datetime_filter(created_before, option_name="created-before")
        if details:
            try:
                results = await container.search_library.results(
                    query,
                    limit=limit,
                    offset=offset,
                    classification_code=classification_code,
                    classification_prefix=classification_prefix,
                    document_status=status_filter,
                    filename_contains=filename_contains,
                    created_after=created_after_filter,
                    created_before=created_before_filter,
                    scope=search_scope,
                    phrase=phrase,
                )
                total = await container.search_library.count(
                    query,
                    classification_code=classification_code,
                    classification_prefix=classification_prefix,
                    document_status=status_filter,
                    filename_contains=filename_contains,
                    created_after=created_after_filter,
                    created_before=created_before_filter,
                    scope=search_scope,
                    phrase=phrase,
                )
            except ValueError as exc:
                raise typer.BadParameter(sanitize_error_message(exc)) from exc
            console.print(
                f"Showing {len(results)} of {total} results (offset={offset}, limit={limit})"
            )
            table = Table("Document ID", "Source", "Run ID", "Status", "Class", "Score", "Snippet")
            for result in results:
                table.add_row(
                    str(result.document_id),
                    result.source,
                    str(result.run_id) if result.run_id else "",
                    result.document_status.value,
                    result.classification_code or "",
                    f"{result.score:.3f}",
                    result.snippet,
                )
            console.print(table)
            return
        try:
            ids = await container.search_library.search(
                query,
                limit=limit,
                offset=offset,
                classification_code=classification_code,
                classification_prefix=classification_prefix,
                document_status=status_filter,
                filename_contains=filename_contains,
                created_after=created_after_filter,
                created_before=created_before_filter,
                scope=search_scope,
                phrase=phrase,
            )
        except ValueError as exc:
            raise typer.BadParameter(sanitize_error_message(exc)) from exc
        for document_id in ids:
            console.print(document_id)

    asyncio.run(run())


@app.command()
def export(
    document_id: Annotated[str, typer.Argument(help="Document ID to export.")],
    output: Annotated[Path | None, typer.Option(help="Optional output path.")] = None,
    format: Annotated[str, typer.Option(help="Export format: txt, md, json.")] = "txt",
) -> None:
    """Export cleaned text and metadata for a document."""

    async def run() -> None:
        export_format = cast(ExportFormat, format.lower())
        if export_format not in {"txt", "md", "json"}:
            raise typer.BadParameter("format must be one of: txt, md, json")
        container = await build_ingest_container()
        document = await container.repository.get_document(DocumentId(document_id))
        if document is None:
            raise typer.BadParameter(f"Document not found: {document_id}")
        cleaned = await container.repository.get_cleaned_output(DocumentId(document_id))
        if cleaned is None:
            raise typer.BadParameter(f"Cleaned output not found: {document_id}")
        classification = await container.repository.get_classification(DocumentId(document_id))
        rendered = ExportedDocument(document, cleaned, classification).render(export_format)
        if output:
            await asyncio.to_thread(_write_cli_output_atomic, output, rendered)
            console.print(f"Exported {document.id} to {output}")
        else:
            console.print(rendered)

    asyncio.run(run())


@app.command()
def benchmark(
    paragraphs: Annotated[int, typer.Option(help="Synthetic paragraph count.", min=1)] = 100,
    paragraph_chars: Annotated[int, typer.Option(help="Characters per paragraph.", min=1)] = 1_000,
    input_path: Annotated[
        Path | None,
        typer.Option(help="Optional UTF-8 text file to benchmark instead of synthetic text."),
    ] = None,
    repeats: Annotated[int, typer.Option(help="Number of repeated benchmark runs.", min=1)] = 1,
    output: Annotated[Path | None, typer.Option(help="Optional JSON output path.")] = None,
) -> None:
    """Benchmark chunking and configured cleaning provider throughput."""

    async def run() -> None:
        container = await build_container()
        result = await run_benchmark_suite(
            cleaner=container.process_document.cleaner,
            document_id=DocumentId("doc_benchmark"),
            text=load_benchmark_text(
                input_path,
                paragraphs=paragraphs,
                paragraph_chars=paragraph_chars,
            ),
            policy=container.process_document.chunking_policy,
            repeats=repeats,
        )
        rendered = benchmark_result_json(result)
        if output:
            await asyncio.to_thread(_write_cli_output_atomic, output, rendered)
            console.print(f"Wrote benchmark results to {output}")
            return
        console.print(rendered)

    asyncio.run(run())


@app.command("eval")
def eval_suite(
    path: Annotated[Path, typer.Argument(exists=True, readable=True, dir_okay=False)],
    output: Annotated[Path | None, typer.Option(help="Optional JSON output path.")] = None,
) -> None:
    """Run a prompt/model evaluation suite."""

    async def run() -> None:
        container = await build_container()
        result = await run_eval_suite(container, load_eval_suite(path))
        rendered = eval_result_json(result)
        if output:
            await asyncio.to_thread(_write_cli_output_atomic, output, rendered)
            console.print(f"Wrote eval results to {output}")
        else:
            console.print(rendered)
        if not result.passed:
            raise typer.Exit(code=1)

    asyncio.run(run())


@app.command("corpus-eval")
def corpus_eval(
    path: Annotated[Path, typer.Argument(exists=True, readable=True, dir_okay=False)],
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory for converted eval artifacts."),
    ] = Path(".librarian/corpus-eval"),
    output: Annotated[Path | None, typer.Option(help="Optional JSON result path.")] = None,
    overwrite: Annotated[
        bool,
        typer.Option(help="Overwrite existing converted eval artifacts."),
    ] = False,
) -> None:
    """Run corpus-level conversion, processing, and search evaluation."""

    async def run() -> None:
        container = await build_container()
        result = await run_corpus_eval_suite(
            container,
            load_corpus_eval_suite(path),
            output_dir=output_dir,
            overwrite=overwrite,
        )
        rendered = corpus_eval_result_json(result)
        if output:
            await asyncio.to_thread(_write_cli_output_atomic, output, rendered)
            console.print(f"Wrote corpus eval results to {output}")
        else:
            console.print(rendered)
        if not result.passed:
            raise typer.Exit(code=1)

    asyncio.run(run())


@app.command("generate-corpus")
def generate_corpus(
    output_dir: Annotated[
        Path,
        typer.Option(help="Directory that will receive corpus/ and corpus_eval_cases.json."),
    ] = Path(".librarian/synthetic-corpus"),
    documents: Annotated[int, typer.Option(help="Number of source documents.", min=1)] = 3,
    paragraphs: Annotated[
        int,
        typer.Option(help="Paragraphs per source document.", min=1),
    ] = 200,
    paragraph_sentences: Annotated[
        int,
        typer.Option(help="Sentences per paragraph.", min=1),
    ] = 4,
    include_docx: Annotated[
        bool,
        typer.Option(help="Also generate sanitized DOCX fixtures with tables/headers/footers."),
    ] = False,
    include_pdf: Annotated[
        bool,
        typer.Option(help="Also generate sanitized embedded-text PDF fixtures."),
    ] = False,
    include_scanned_pdf: Annotated[
        bool,
        typer.Option(help="Also generate sanitized scanned and mixed OCR PDF fixtures."),
    ] = False,
    include_noisy_ocr_pdf: Annotated[
        bool,
        typer.Option(help="Also generate a sanitized noisy scanned OCR PDF fixture."),
    ] = False,
    include_transcript_captions: Annotated[
        bool,
        typer.Option(help="Also generate sanitized SRT/VTT transcript caption fixtures."),
    ] = False,
    overwrite: Annotated[bool, typer.Option(help="Overwrite existing generated files.")] = False,
) -> None:
    """Generate a deterministic sanitized corpus-eval fixture."""
    try:
        result = generate_synthetic_corpus(
            corpus_dir=output_dir / "corpus",
            suite_path=output_dir / "corpus_eval_cases.json",
            documents=documents,
            paragraphs_per_document=paragraphs,
            sentences_per_paragraph=paragraph_sentences,
            include_docx=include_docx,
            include_pdf=include_pdf,
            include_scanned_pdf=include_scanned_pdf,
            include_noisy_ocr_pdf=include_noisy_ocr_pdf,
            include_transcript_captions=include_transcript_captions,
            overwrite=overwrite,
        )
    except (FileExistsError, ValueError) as exc:
        raise typer.BadParameter(sanitize_error_message(exc)) from exc
    console.print(f"Generated {len(result.files)} synthetic document(s)")
    console.print(f"Corpus: {result.corpus_dir}")
    console.print(f"Suite: {result.suite_path}")
    console.print(f"Size: {result.total_bytes:,} bytes, {result.total_chars:,} characters")


@app.command("page-manifest")
def page_manifest(
    path: Annotated[Path, typer.Argument(exists=True, readable=True, dir_okay=False)],
    limit: Annotated[int, typer.Option(help="Maximum page rows to print.", min=1, max=1_000)] = 50,
    offset: Annotated[int, typer.Option(help="Page rows to skip.", min=0)] = 0,
    failures_only: Annotated[
        bool,
        typer.Option(help="Only print failed page rows."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print a machine-readable summary."),
    ] = False,
) -> None:
    """Inspect a PDF page extraction manifest."""
    try:
        payload, pages = _read_pdf_page_manifest(path.expanduser())
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(sanitize_error_message(exc)) from exc
    visible_pages = [
        page
        for page in pages
        if not failures_only or str(page.get("status") or "") == "failed"
    ]
    page_window = visible_pages[offset : offset + limit]
    statuses = _count_manifest_values(pages, "status")
    sources = _count_manifest_values(pages, "source")
    warnings = _count_manifest_warnings(pages)
    corrected = sum(1 for page in pages if page.get("corrected") is True)
    confidences = [
        float(confidence)
        for page in pages
        if isinstance((confidence := page.get("confidence")), int | float)
    ]
    attempts = sum(
        int(attempt_count)
        for page in pages
        if isinstance((attempt_count := page.get("attempts")), int)
    )
    average_confidence = (
        f"{sum(confidences) / len(confidences):.1f}" if confidences else "n/a"
    )
    manifest_summary_obj = payload.get("summary")
    manifest_summary = (
        cast(dict[str, object], manifest_summary_obj)
        if isinstance(manifest_summary_obj, dict)
        else {}
    )
    if json_output:
        summary = {
            "manifest_path": str(path.resolve()),
            "schema_version": payload.get("schema_version"),
            "manifest_status": manifest_summary.get("status"),
            "manifest_summary": manifest_summary,
            "source_sha256": payload.get("source_sha256", ""),
            "page_count": payload.get("page_count", len(pages)),
            "statuses": statuses,
            "sources": sources,
            "warnings": warnings,
            "corrected_pages": corrected,
            "attempts": attempts,
            "average_confidence": (
                round(sum(confidences) / len(confidences), 1) if confidences else None
            ),
            "limit": limit,
            "offset": offset,
            "failures_only": failures_only,
            "pages": [
                {
                    "page_number": page.get("page_number"),
                    "source": page.get("source"),
                    "status": page.get("status"),
                    "chars": page.get("chars"),
                    "confidence": page.get("confidence"),
                    "corrected": page.get("corrected") is True,
                    "attempts": page.get("attempts", 0),
                    "duration_ms": page.get("duration_ms"),
                    "warnings": _manifest_warning_list(page),
                    "error": page.get("error"),
                }
                for page in page_window
            ],
        }
        console.out(json.dumps(summary, indent=2, sort_keys=True))
        return
    console.print(f"Manifest: {path.resolve()}")
    if payload.get("schema_version") is not None or manifest_summary.get("status") is not None:
        console.print(
            "Schema: "
            f"{payload.get('schema_version', '')}; "
            f"status={manifest_summary.get('status', '')}"
        )
    console.print(f"Source SHA-256: {payload.get('source_sha256', '')}")
    console.print(
        "Pages: "
        f"{payload.get('page_count', len(pages))} "
        f"(succeeded={statuses.get('succeeded', 0)}, "
        f"failed={statuses.get('failed', 0)}, "
        f"pending={statuses.get('pending', 0)})"
    )
    console.print(
        "Sources: "
        f"embedded={sources.get('embedded', 0)}, "
        f"ocr={sources.get('ocr', 0)}, "
        f"empty={sources.get('empty', 0)}, "
        f"pending={sources.get('pending', 0)}; "
        f"corrected={corrected}; attempts={attempts}; avg_confidence={average_confidence}"
    )
    if warnings:
        warning_summary = ", ".join(
            f"{warning}={count}" for warning, count in sorted(warnings.items())
        )
        console.print(f"Warnings: {warning_summary}")
    failed_errors = [
        f"page {page.get('page_number')}: {error}"
        for page in page_window
        if isinstance((error := page.get("error")), str) and error
    ]
    if failed_errors:
        console.print(f"Errors: {'; '.join(failed_errors)}")
    table = Table(
        "Page",
        "Source",
        "Status",
        "Chars",
        "Confidence",
        "Corrected",
        "Attempts",
        "Duration ms",
        "Warnings",
        "Error",
    )
    for page in page_window:
        confidence = page.get("confidence")
        duration_ms = page.get("duration_ms")
        table.add_row(
            str(page.get("page_number", "")),
            str(page.get("source", "")),
            str(page.get("status", "")),
            str(page.get("chars", "")),
            f"{float(confidence):.1f}" if isinstance(confidence, int | float) else "",
            "yes" if page.get("corrected") is True else "",
            str(page.get("attempts", "")),
            f"{float(duration_ms):.1f}" if isinstance(duration_ms, int | float) else "",
            ", ".join(_manifest_warning_list(page)),
            str(page.get("error") or ""),
        )
    console.print(table)


@app.command()
def api(
    host: str | None = typer.Option(None, help="API bind host."),
    port: int | None = typer.Option(None, help="API bind port."),
) -> None:
    """Run the Librarian API service."""
    settings = Settings()
    bind_host = host or settings.api_host
    if bind_host in {"0.0.0.0", "::", "[::]"}:  # noqa: S104
        if not (
            settings.api_key
            or settings.api_keys
            or settings.api_key_sha256
            or settings.api_key_hashes
        ):
            raise typer.BadParameter(
                "LIBRARIAN_API_KEY, LIBRARIAN_API_KEYS, LIBRARIAN_API_KEY_SHA256, "
                "or LIBRARIAN_API_KEY_HASHES is required when binding publicly"
            )
        if settings.api_import_root is None:
            raise typer.BadParameter("LIBRARIAN_API_IMPORT_ROOT is required when binding publicly")
    uvicorn.run(
        "librarian.api.app:create_app",
        factory=True,
        host=bind_host,
        port=port or settings.api_port,
    )


def _conversion_format(value: str) -> ConversionFormat:
    normalized = value.lower()
    if normalized in {"md", "markdown"}:
        return ConversionFormat.MARKDOWN
    if normalized in {"txt", "text"}:
        return ConversionFormat.TEXT
    raise typer.BadParameter("format must be one of: md, txt")


def _transcript_format(value: str) -> TranscriptFormat:
    normalized = value.lower()
    aliases = {
        "markdown": TranscriptFormat.MARKDOWN,
        "text": TranscriptFormat.TEXT,
    }
    try:
        return aliases.get(normalized, TranscriptFormat(normalized))
    except ValueError as exc:
        raise typer.BadParameter("format must be one of: md, txt, srt, vtt, csv") from exc


def _directory_output_mode(value: str) -> DirectoryOutputMode:
    try:
        return DirectoryOutputMode(value)
    except ValueError as exc:
        raise typer.BadParameter(
            "output-mode must be one of: new-directory, original, subdirectory"
        ) from exc


def _validate_cli_directory_output(
    source_dir: Path,
    output_mode: DirectoryOutputMode,
    output_dir: Path | None,
) -> None:
    try:
        validate_directory_output(
            source_dir=source_dir,
            output_mode=output_mode,
            output_dir=output_dir,
        )
        if output_mode == DirectoryOutputMode.NEW_DIRECTORY and output_dir is not None:
            _reject_symlinked_cli_output_path(output_dir / ".librarian-output-probe")
    except ValueError as exc:
        raise typer.BadParameter(sanitize_error_message(exc)) from exc


def _document_status_filter(value: str | None) -> DocumentStatus | None:
    if value is None:
        return None
    try:
        return DocumentStatus(value)
    except ValueError as exc:
        raise typer.BadParameter(
            "document-status must be one of: ingested, processing, ready, failed"
        ) from exc


def _search_scope_filter(value: str) -> SearchScope:
    if value in {"cleaned", "raw"}:
        return cast(SearchScope, value)
    raise typer.BadParameter("scope must be one of: cleaned, raw")


def _datetime_filter(value: str | None, *, option_name: str) -> datetime | None:
    if value is None:
        return None
    try:
        normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise typer.BadParameter(f"{option_name} must be an ISO-8601 timestamp") from exc


def _write_cli_output_atomic(path: Path, payload: str) -> None:
    _reject_symlinked_cli_output_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary_path.write_text(payload, encoding="utf-8")
        temporary_path.replace(path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _reject_symlinked_cli_output_path(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"Output path must not be a symlink: {path}")
    for parent in reversed(path.parents):
        if parent.is_absolute() and len(parent.parts) <= 2:
            continue
        if parent.exists() and parent.is_symlink():
            raise ValueError(f"Output path crosses symlinked parent: {path}")


def _doctor_module_checks() -> list[tuple[str, str, str, str]]:
    modules = [
        ("pdfplumber", "embedded PDF extraction", "pip install -e '.[pdf]'"),
        ("pdf2image", "scanned PDF rasterization", "pip install -e '.[ocr]'"),
        ("pytesseract", "OCR confidence diagnostics", "pip install -e '.[ocr]'"),
        ("markitdown", "broad-format conversion", "pip install -e '.[universal]'"),
    ]
    return [
        (
            module_name,
            capability,
            "ok" if _module_available(module_name) else "missing",
            "installed" if _module_available(module_name) else hint,
        )
        for module_name, capability, hint in modules
    ]


def _doctor_tool_checks() -> list[tuple[str, str, str, str]]:
    tools = [
        ("tesseract", "image/PDF OCR", "brew install tesseract"),
        ("pdftoppm", "PDF page rasterization", "brew install poppler"),
    ]
    rows: list[tuple[str, str, str, str]] = []
    for tool_name, capability, hint in tools:
        tool_path = shutil.which(tool_name)
        rows.append(
            (
                tool_name,
                capability,
                "ok" if tool_path else "missing",
                tool_path or hint,
            )
        )
    return rows


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _read_pdf_page_manifest(path: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    if path.is_symlink():
        raise ValueError(f"PDF page manifest path must not be a symlink: {path}")
    if _path_crosses_symlink(path):
        raise ValueError(f"PDF page manifest path crosses symlinked parent: {path}")
    if path.stat().st_size > _MAX_PAGE_MANIFEST_READ_BYTES:
        raise ValueError(
            "PDF page manifest exceeds read limit of "
            f"{_MAX_PAGE_MANIFEST_READ_BYTES} bytes: {path}"
        )
    try:
        payload_obj: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("PDF page manifest is invalid JSON") from exc
    if not isinstance(payload_obj, dict):
        raise ValueError("PDF page manifest must be a JSON object")
    payload = cast(dict[str, object], payload_obj)
    if payload.get("artifact_type") != "pdf-page-extraction-manifest":
        raise ValueError("PDF page manifest has unexpected artifact_type")
    pages_obj = payload.get("pages")
    if not isinstance(pages_obj, list):
        raise ValueError("PDF page manifest is missing pages")
    pages: list[dict[str, object]] = []
    for page in cast(list[object], pages_obj):
        if not isinstance(page, dict):
            raise ValueError("PDF page manifest contains an invalid page record")
        pages.append(cast(dict[str, object], page))
    return payload, pages


def _read_api_audit_events(
    database: SQLiteDatabase,
    limit: int,
    offset: int,
    event: str | None,
) -> list[dict[str, object]]:
    with database.connect() as connection:
        if event:
            rows = connection.execute(
                """
                SELECT id, event, method, path, client_host, credential_present,
                       credential_scope, retry_after_seconds, created_at
                FROM api_audit_events
                WHERE event = ?
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (event, limit, offset),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT id, event, method, path, client_host, credential_present,
                       credential_scope, retry_after_seconds, created_at
                FROM api_audit_events
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "event": str(row["event"]),
            "method": str(row["method"]),
            "path": str(row["path"]),
            "client_host": str(row["client_host"]),
            "credential_present": bool(row["credential_present"]),
            "credential_scope": (
                str(row["credential_scope"]) if row["credential_scope"] is not None else None
            ),
            "retry_after_seconds": (
                int(row["retry_after_seconds"])
                if row["retry_after_seconds"] is not None
                else None
            ),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


def _audit_credential_summary(row: dict[str, object]) -> str:
    scope = row.get("credential_scope")
    if isinstance(scope, str) and scope:
        return f"scope={scope}"
    return "present" if row.get("credential_present") is True else ""


def _count_manifest_values(pages: list[dict[str, object]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for page in pages:
        value = page.get(key)
        if isinstance(value, str) and value:
            counts[value] = counts.get(value, 0) + 1
    return counts


def _count_manifest_warnings(pages: list[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for page in pages:
        for warning in _manifest_warning_list(page):
            counts[warning] = counts.get(warning, 0) + 1
    return counts


def _manifest_warning_list(page: dict[str, object]) -> list[str]:
    value = page.get("warnings")
    if not isinstance(value, list):
        return []
    warnings: list[str] = []
    for warning in cast(list[object], value):
        if isinstance(warning, str):
            warnings.append(warning)
    return warnings


def _write_workspace_backup_archive(
    output_path: Path,
    *,
    settings: Settings,
    db_backup_path: Path,
) -> int:
    data_dir = settings.data_dir.expanduser().resolve()
    database_path = settings.database_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_archive = output_path.with_name(f".{output_path.name}.tmp")
    temporary_archive.unlink(missing_ok=True)
    database_archive_path = _workspace_database_archive_path(
        data_dir=data_dir,
        database_path=database_path,
    )
    file_count = 0
    try:
        with zipfile.ZipFile(
            temporary_archive,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            archive.writestr(
                "workspace-backup.json",
                json.dumps(
                    {
                        "artifact_type": "librarian-workspace-backup",
                        "created_at": datetime.now().astimezone().isoformat(),
                        "data_dir": str(data_dir),
                        "database_path": str(database_path),
                        "database_archive_path": database_archive_path,
                    },
                    indent=2,
                ),
            )
            file_count += 1
            archive.write(db_backup_path, database_archive_path)
            file_count += 1
            if data_dir.exists():
                excluded = _workspace_backup_exclusions(
                    data_dir=data_dir,
                    database_path=database_path,
                    output_path=output_path,
                    temporary_archive=temporary_archive,
                )
                for path in sorted(data_dir.rglob("*")):
                    if not path.is_file() or path.is_symlink():
                        continue
                    resolved = path.resolve()
                    if not _is_relative_to(resolved, data_dir):
                        continue
                    if resolved in excluded:
                        continue
                    archive.write(path, f"data/{path.relative_to(data_dir).as_posix()}")
                    file_count += 1
        temporary_archive.replace(output_path)
    except Exception:
        temporary_archive.unlink(missing_ok=True)
        raise
    return file_count


def _extract_workspace_database_snapshot(
    archive_path: Path,
    *,
    temporary_dir: Path,
    max_expanded_bytes: int,
) -> tuple[Path, str]:
    if not archive_path.exists():
        raise FileNotFoundError(f"Workspace backup archive does not exist: {archive_path}")
    with zipfile.ZipFile(archive_path) as archive:
        manifest = _read_workspace_backup_manifest(archive)
        database_archive_path = str(manifest["database_archive_path"])
        _validate_workspace_archive_members(
            archive,
            database_archive_path=database_archive_path,
            max_expanded_bytes=max_expanded_bytes,
        )
        db_restore_source = temporary_dir / "workspace-restore.sqlite"
        with archive.open(database_archive_path) as source, db_restore_source.open("wb") as target:
            shutil.copyfileobj(source, target)
    return db_restore_source, database_archive_path


def _restore_workspace_data_files(
    archive_path: Path,
    *,
    settings: Settings,
    database_archive_path: str,
    max_expanded_bytes: int,
) -> None:
    expanded_data_dir = settings.data_dir.expanduser()
    if _path_crosses_symlink(expanded_data_dir):
        raise ValueError(f"Workspace restore data_dir crosses symlink: {expanded_data_dir}")
    data_dir = expanded_data_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        _validate_workspace_archive_members(
            archive,
            database_archive_path=database_archive_path,
            max_expanded_bytes=max_expanded_bytes,
        )
        for member in archive.infolist():
            if member.is_dir() or member.filename in {
                "workspace-backup.json",
                database_archive_path,
            }:
                continue
            if not member.filename.startswith("data/"):
                continue
            relative_path = Path(member.filename.removeprefix("data/"))
            destination = (data_dir / relative_path).resolve()
            if not _is_relative_to(destination, data_dir):
                raise ValueError(f"Workspace archive path escapes data dir: {member.filename}")
            _reject_symlinked_workspace_restore_path(data_dir / relative_path, data_dir=data_dir)
            destination.parent.mkdir(parents=True, exist_ok=True)
            _reject_symlinked_workspace_restore_path(data_dir / relative_path, data_dir=data_dir)
            with archive.open(member) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)


def _read_workspace_backup_manifest(archive: zipfile.ZipFile) -> dict[str, object]:
    try:
        manifest_info = archive.getinfo("workspace-backup.json")
        if manifest_info.file_size > _MAX_WORKSPACE_BACKUP_MANIFEST_BYTES:
            raise ValueError(
                "Workspace archive manifest expands to more than "
                f"{_MAX_WORKSPACE_BACKUP_MANIFEST_BYTES} bytes"
            )
        with archive.open(manifest_info) as manifest_file:
            payload_obj: object = json.loads(manifest_file.read().decode("utf-8"))
    except KeyError as exc:
        raise ValueError("Workspace archive is missing workspace-backup.json") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("Workspace archive manifest is invalid JSON") from exc
    if not isinstance(payload_obj, dict):
        raise ValueError("Workspace archive manifest must be a JSON object")
    payload = cast(dict[str, object], payload_obj)
    if payload.get("artifact_type") != "librarian-workspace-backup":
        raise ValueError("Workspace archive manifest has unexpected artifact_type")
    database_archive_path = payload.get("database_archive_path")
    if not isinstance(database_archive_path, str) or not database_archive_path:
        raise ValueError("Workspace archive manifest is missing database_archive_path")
    _validate_workspace_archive_path(database_archive_path)
    return payload


def _validate_workspace_archive_members(
    archive: zipfile.ZipFile,
    *,
    database_archive_path: str,
    max_expanded_bytes: int,
) -> None:
    members = archive.infolist()
    if len(members) > _MAX_WORKSPACE_BACKUP_MEMBERS:
        raise ValueError(
            "Workspace archive contains more than "
            f"{_MAX_WORKSPACE_BACKUP_MEMBERS} members"
        )
    seen_names: set[str] = set()
    expanded_bytes = 0
    has_database_snapshot = False
    for member in members:
        name = member.filename
        if name in seen_names:
            raise ValueError(f"Workspace archive contains duplicate path: {name}")
        seen_names.add(name)
        _validate_workspace_archive_path(name)
        if name == database_archive_path:
            has_database_snapshot = True
        if member.is_dir():
            continue
        if _zip_member_is_symlink(member):
            raise ValueError(f"Workspace archive contains symlink member: {name}")
        expanded_bytes += member.file_size
        if expanded_bytes > max_expanded_bytes:
            raise ValueError(
                f"Workspace archive expands to more than {max_expanded_bytes} bytes"
            )
    if not has_database_snapshot:
        raise ValueError("Workspace archive is missing its database snapshot")


def _validate_workspace_archive_path(name: str) -> None:
    if "\\" in name:
        raise ValueError(f"Workspace archive contains unsafe path: {name}")
    parts = PurePosixPath(name).parts
    if name.startswith("/") or ".." in parts:
        raise ValueError(f"Workspace archive contains unsafe path: {name}")


def _zip_member_is_symlink(member: zipfile.ZipInfo) -> bool:
    return (member.external_attr >> 16) & 0o170000 == 0o120000


def _reject_symlinked_workspace_restore_path(path: Path, *, data_dir: Path) -> None:
    relative_path = path.relative_to(data_dir)
    current = data_dir
    for part in relative_path.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValueError(f"Workspace restore path crosses symlink: {path}")


def _path_crosses_symlink(path: Path) -> bool:
    for current in (*reversed(path.parents), path):
        if current.is_absolute() and len(current.parts) <= 2:
            continue
        if current.exists() and current.is_symlink():
            return True
    return False


def _workspace_database_archive_path(*, data_dir: Path, database_path: Path) -> str:
    try:
        return f"data/{database_path.relative_to(data_dir).as_posix()}"
    except ValueError:
        return f"database/{database_path.name}"


def _workspace_backup_exclusions(
    *,
    data_dir: Path,
    database_path: Path,
    output_path: Path,
    temporary_archive: Path,
) -> set[Path]:
    candidates = {
        database_path,
        Path(f"{database_path}-wal"),
        Path(f"{database_path}-shm"),
        output_path,
        temporary_archive,
    }
    return {path.resolve() for path in candidates if _is_relative_to(path.resolve(), data_dir)}


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _build_extractor(settings: Settings) -> CompositeExtractor:
    return CompositeExtractor(
        ocr_language=settings.ocr_language,
        ocr_timeout_seconds=settings.ocr_timeout_seconds,
        ocr_pdf_dpi=settings.ocr_pdf_dpi,
        ocr_pdf_max_pages=settings.ocr_pdf_max_pages,
        ocr_preprocess_mode=settings.ocr_preprocess_mode,
        ocr_threshold=settings.ocr_threshold,
        ocr_preserve_page_images=settings.ocr_preserve_page_images,
        ocr_correction_provider=LazyLLMProvider(settings),
        ocr_correction_mode=settings.ocr_llm_correction,
        ocr_correction_model=settings.ocr_llm_model or settings.llm_model,
        ocr_low_confidence_threshold=settings.ocr_low_confidence_threshold,
        ocr_max_correction_response_chars=settings.llm_max_response_chars,
        ocr_page_concurrency=settings.ocr_page_concurrency,
        ocr_fail_on_page_error=settings.ocr_fail_on_page_error,
        text_max_input_bytes=settings.text_max_input_bytes,
        docx_max_input_bytes=settings.docx_max_input_bytes,
        pdf_max_input_bytes=settings.pdf_max_input_bytes,
        pdf_max_pages=settings.pdf_max_pages,
        universal_max_input_bytes=settings.universal_max_input_bytes,
        universal_timeout_seconds=settings.universal_timeout_seconds,
    )
