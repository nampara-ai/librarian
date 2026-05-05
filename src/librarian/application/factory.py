"""Application composition helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from librarian.application.classify_document import ClassifyDocument
from librarian.application.clean_chunks import CleanChunks, CoherenceMode
from librarian.application.ingest_document import IngestDocument
from librarian.application.ports import LLMProvider
from librarian.application.process_document import ProcessDocument
from librarian.config import Settings
from librarian.ingest.extractors import CompositeExtractor
from librarian.llm import MockLLMProvider, OpenAICompatibleProvider
from librarian.pipeline.chunking import ChunkingPolicy
from librarian.prompts import PromptCatalog
from librarian.storage.sqlite import SQLiteDatabase, SQLiteRepository
from librarian.taxonomy.dewey import DeweyTaxonomy


@dataclass(frozen=True, slots=True)
class ApplicationContainer:
    """Composed application services."""

    settings: Settings
    database: SQLiteDatabase
    repository: SQLiteRepository
    ingest_document: IngestDocument
    process_document: ProcessDocument


async def build_container(settings: Settings | None = None) -> ApplicationContainer:
    """Build concrete application services."""
    resolved_settings = settings or Settings()
    database = SQLiteDatabase(resolved_settings.database_path)
    await database.initialize()
    repository = SQLiteRepository(database)
    extractor = CompositeExtractor()
    provider = _build_provider(resolved_settings)
    cleaner = CleanChunks(
        provider=provider,
        prompt_catalog=PromptCatalog(),
        prompt_version=resolved_settings.cleaning_prompt_version,
        model=resolved_settings.llm_model,
        coherence_mode=cast(CoherenceMode, resolved_settings.coherence_mode),
    )
    taxonomy = DeweyTaxonomy()
    classifier = ClassifyDocument(
        provider=provider,
        prompt_catalog=PromptCatalog(),
        prompt_version=resolved_settings.classification_prompt_version,
        model=resolved_settings.llm_model,
        taxonomy=taxonomy,
    )
    policy = ChunkingPolicy(
        target_chars=resolved_settings.chunk_target_chars,
        overlap_chars=resolved_settings.chunk_overlap_chars,
    )
    ingest = IngestDocument(documents=repository, content=repository, extractor=extractor)
    process = ProcessDocument(
        documents=repository,
        runs=repository,
        chunks=repository,
        content=repository,
        outputs=repository,
        search=repository,
        events=repository,
        cleaner=cleaner,
        classifier=classifier,
        chunking_policy=policy,
    )
    return ApplicationContainer(
        settings=resolved_settings,
        database=database,
        repository=repository,
        ingest_document=ingest,
        process_document=process,
    )


def _build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "mock":
        return MockLLMProvider()
    if settings.llm_provider == "openai-compatible":
        return OpenAICompatibleProvider(
            api_key_env=settings.llm_api_key_env,
        base_url=settings.llm_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
        max_concurrency=settings.llm_max_concurrency,
        max_retries=settings.llm_max_retries,
        retry_base_delay_seconds=settings.llm_retry_base_delay_seconds,
        retry_max_delay_seconds=settings.llm_retry_max_delay_seconds,
    )
    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider}")
