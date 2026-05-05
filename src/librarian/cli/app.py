"""Typer CLI adapter."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from rich.console import Console

from librarian.api.app import create_app
from librarian.config import Settings
from librarian.domain.ids import DocumentId, digest_text
from librarian.ingest.extractors import CompositeExtractor
from librarian.pipeline.chunking import ChunkingPolicy, chunk_text
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
def api(
    host: str | None = typer.Option(None, help="API bind host."),
    port: int | None = typer.Option(None, help="API bind port."),
) -> None:
    """Run the Librarian API service."""
    settings = Settings()
    uvicorn.run(
        create_app,
        factory=True,
        host=host or settings.api_host,
        port=port or settings.api_port,
    )
