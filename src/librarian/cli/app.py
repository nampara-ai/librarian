"""Typer CLI adapter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
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
from librarian.application.eval import eval_result_json, load_eval_suite, run_eval_suite
from librarian.application.export_document import ExportedDocument, ExportFormat
from librarian.application.factory import build_container, build_ingest_container
from librarian.application.import_library import (
    ImportLibrary,
    ImportProcessingMode,
    write_import_report,
)
from librarian.application.jobs import QueueWorker
from librarian.config import Settings
from librarian.domain.ids import DocumentId, RunId, digest_text
from librarian.domain.models import RunStage, RunStatus
from librarian.ingest.extractors import CompositeExtractor
from librarian.pipeline.chunking import ChunkingPolicy, chunk_text
from librarian.storage.sqlite import SQLiteDatabase, SQLiteRunQueue
from librarian.version import __version__

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command()
def version() -> None:
    """Print the Librarian version."""
    console.print(__version__)


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
    config_path.write_text(
        json.dumps(
            {
                "database_path": str(root / settings.database_path),
                "data_dir": str(data_dir),
                "llm_provider": settings.llm_provider,
                "llm_model": settings.llm_model,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    asyncio.run(SQLiteDatabase(root / settings.database_path).initialize())
    console.print(f"Initialized Librarian workspace at {data_dir}")


@app.command()
def migrate() -> None:
    """Apply database migrations."""
    settings = Settings()
    asyncio.run(SQLiteDatabase(settings.database_path).initialize())
    console.print(f"Applied migrations to {settings.database_path}")


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
        output_dir.resolve() if output_dir else None,
    )

    async def run() -> None:
        settings = Settings()
        converter = DocumentConverter(_build_extractor(settings))
        result = await converter.convert_directory(
            path.resolve(),
            format=conversion_format,
            output_mode=mode,
            output_dir=output_dir.resolve() if output_dir else None,
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


@app.command("import")
def import_directory(
    path: Annotated[Path, typer.Argument(exists=True, readable=True, file_okay=False)],
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
    """Convert a directory, ingest outputs, and optionally process or enqueue."""
    if process and queue:
        raise typer.BadParameter("Choose only one of --process or --queue")
    conversion_format = _conversion_format(format)
    mode = _directory_output_mode(output_mode)
    _validate_cli_directory_output(
        path.resolve(),
        mode,
        output_dir.resolve() if output_dir else None,
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
        )
        result = await importer.import_directory(
            path.resolve(),
            format=conversion_format,
            output_mode=mode,
            processing_mode=processing_mode,
            output_dir=output_dir.resolve() if output_dir else None,
            subdirectory_name=subdirectory_name,
            recursive=recursive,
            overwrite=overwrite,
            manifest_path=manifest.resolve() if manifest else None,
            resume=resume,
            write_sidecar=sidecar_metadata,
        )
        if report:
            await write_import_report(report.resolve(), result)
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
            await SQLiteRunQueue(container.database).enqueue(new_run.id)
            console.print(f"Queued retry {new_run.id}")
            return
        finished = await container.process_document.execute_existing(new_run.id)
        console.print(f"Retry {finished.id}: {finished.status.value}")

    asyncio.run(run())


@app.command("queue")
def inspect_queue(
    limit: Annotated[int, typer.Option(help="Maximum queue rows.", min=1, max=500)] = 100,
) -> None:
    """List durable queue items."""

    async def run() -> None:
        container = await build_ingest_container()
        rows = await SQLiteRunQueue(container.database).list(limit=limit)
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
) -> None:
    """Show processing run status and events."""

    async def run() -> None:
        container = await build_ingest_container()
        run_record = await container.repository.get_run(RunId(run_id))
        if run_record is None:
            raise typer.BadParameter(f"Run not found: {run_id}")
        console.print(f"Run {run_record.id}: {run_record.status.value} ({run_record.stage.value})")
        for event in await container.repository.list_events(run_record.id):
            console.print(event)

    asyncio.run(run())


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search query.")],
    limit: Annotated[int, typer.Option(help="Maximum results.", min=1, max=500)] = 20,
) -> None:
    """Search cleaned outputs."""

    async def run() -> None:
        container = await build_ingest_container()
        try:
            results = await container.repository.search(query, limit=limit)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        for document_id in results:
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
            await asyncio.to_thread(output.write_text, rendered, encoding="utf-8")
            console.print(f"Exported {document.id} to {output}")
        else:
            console.print(rendered)

    asyncio.run(run())


@app.command()
def benchmark(
    paragraphs: Annotated[int, typer.Option(help="Synthetic paragraph count.")] = 100,
    paragraph_chars: Annotated[int, typer.Option(help="Characters per paragraph.")] = 1_000,
    input_path: Annotated[
        Path | None,
        typer.Option(help="Optional UTF-8 text file to benchmark instead of synthetic text."),
    ] = None,
    repeats: Annotated[int, typer.Option(help="Number of repeated benchmark runs.")] = 1,
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
            repeats=max(1, repeats),
        )
        rendered = benchmark_result_json(result)
        if output:
            await asyncio.to_thread(output.write_text, rendered, encoding="utf-8")
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
            await asyncio.to_thread(output.write_text, rendered, encoding="utf-8")
            console.print(f"Wrote eval results to {output}")
        else:
            console.print(rendered)
        if not result.passed:
            raise typer.Exit(code=1)

    asyncio.run(run())


@app.command()
def api(
    host: str | None = typer.Option(None, help="API bind host."),
    port: int | None = typer.Option(None, help="API bind port."),
) -> None:
    """Run the Librarian API service."""
    settings = Settings()
    bind_host = host or settings.api_host
    if bind_host in {"0.0.0.0", "::", "[::]"}:  # noqa: S104
        if not settings.api_key:
            raise typer.BadParameter("LIBRARIAN_API_KEY is required when binding publicly")
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
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _build_extractor(settings: Settings) -> CompositeExtractor:
    return CompositeExtractor(
        ocr_language=settings.ocr_language,
        ocr_timeout_seconds=settings.ocr_timeout_seconds,
        ocr_pdf_dpi=settings.ocr_pdf_dpi,
        ocr_pdf_max_pages=settings.ocr_pdf_max_pages,
        text_max_input_bytes=settings.text_max_input_bytes,
        docx_max_input_bytes=settings.docx_max_input_bytes,
        pdf_max_input_bytes=settings.pdf_max_input_bytes,
        pdf_max_pages=settings.pdf_max_pages,
        universal_max_input_bytes=settings.universal_max_input_bytes,
        universal_timeout_seconds=settings.universal_timeout_seconds,
    )
