"""Typer CLI adapter."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from rich.console import Console
from rich.table import Table

from librarian.application.factory import build_container
from librarian.config import Settings
from librarian.domain.ids import DocumentId, RunId, digest_text
from librarian.ingest.extractors import CompositeExtractor
from librarian.pipeline.chunking import ChunkingPolicy, chunk_text
from librarian.storage.sqlite import SQLiteDatabase
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
def chunk(
    path: Annotated[Path, typer.Argument(exists=True, readable=True, dir_okay=False)],
    target_chars: Annotated[int, typer.Option(help="Target chunk size in characters.")] = 12_000,
    overlap_chars: Annotated[int, typer.Option(help="Chunk overlap in characters.")] = 800,
) -> None:
    """Extract and chunk a document without calling an LLM."""
    resolved_path = path.resolve()

    async def run() -> None:
        extractor = CompositeExtractor()
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
def ingest(
    path: Annotated[Path, typer.Argument(exists=True, readable=True, dir_okay=False)],
) -> None:
    """Ingest a source file and persist extracted text."""
    resolved_path = path.resolve()

    async def run() -> None:
        container = await build_container()
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


@app.command("list")
def list_documents() -> None:
    """List ingested documents."""

    async def run() -> None:
        container = await build_container()
        documents = await container.repository.list()
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
        container = await build_container()
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
        container = await build_container()
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
    limit: Annotated[int, typer.Option(help="Maximum results.")] = 20,
) -> None:
    """Search cleaned outputs."""

    async def run() -> None:
        container = await build_container()
        results = await container.repository.search(query, limit=limit)
        for document_id in results:
            console.print(document_id)

    asyncio.run(run())


@app.command()
def export(
    document_id: Annotated[str, typer.Argument(help="Document ID to export.")],
    output: Annotated[Path | None, typer.Option(help="Optional output path.")] = None,
) -> None:
    """Export cleaned text for a document."""

    async def run() -> None:
        container = await build_container()
        document = await container.repository.get_document(DocumentId(document_id))
        if document is None:
            raise typer.BadParameter(f"Document not found: {document_id}")
        cleaned = await container.repository.get_cleaned_output(DocumentId(document_id))
        if cleaned is None:
            raise typer.BadParameter(f"Cleaned output not found: {document_id}")
        classification = await container.repository.get_classification(DocumentId(document_id))
        header = [
            "---",
            f"Document ID: {document.id}",
            f"Source: {document.source.filename}",
        ]
        if classification:
            header.append(f"Classification: {classification.code} - {classification.label}")
        header.extend(["---", "", cleaned.text])
        rendered = "\n".join(header)
        if output:
            await asyncio.to_thread(output.write_text, rendered, encoding="utf-8")
            console.print(f"Exported {document.id} to {output}")
        else:
            console.print(rendered)

    asyncio.run(run())


@app.command()
def api(
    host: str | None = typer.Option(None, help="API bind host."),
    port: int | None = typer.Option(None, help="API bind port."),
) -> None:
    """Run the Librarian API service."""
    settings = Settings()
    uvicorn.run(
        "librarian.api.app:create_app",
        factory=True,
        host=host or settings.api_host,
        port=port or settings.api_port,
    )
