"""FastAPI adapter."""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from librarian.config import Settings
from librarian.taxonomy.dewey import DeweyTaxonomy
from librarian.version import __version__


class HealthResponse(BaseModel):
    status: str


class VersionResponse(BaseModel):
    version: str


class ClassificationResponse(BaseModel):
    classifications: dict[str, str]


def create_app() -> FastAPI:
    """Create the Librarian API app."""
    settings = Settings()
    taxonomy = DeweyTaxonomy()

    app = FastAPI(
        title="Librarian API",
        version=__version__,
        summary="Local-first corpus cleaner and organizer.",
    )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="healthy")

    @app.get("/version", response_model=VersionResponse)
    async def version() -> VersionResponse:
        return VersionResponse(version=__version__)

    @app.get("/classifications", response_model=ClassificationResponse)
    async def classifications() -> ClassificationResponse:
        return ClassificationResponse(classifications=taxonomy.all())

    @app.get("/config")
    async def config() -> dict[str, str | int | None]:
        return {
            "data_dir": str(settings.data_dir),
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "coherence_mode": settings.coherence_mode,
            "chunk_target_chars": settings.chunk_target_chars,
            "chunk_overlap_chars": settings.chunk_overlap_chars,
        }

    return app
