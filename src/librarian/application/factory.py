"""Application composition helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from librarian.application.classify_document import ClassifyDocument
from librarian.application.clean_chunks import CleanChunks
from librarian.application.ingest_document import IngestDocument
from librarian.application.ports import ApplicationMetrics, LLMProvider
from librarian.application.process_document import ProcessDocument
from librarian.application.search_library import SearchLibrary
from librarian.config import Settings
from librarian.ingest.extractors import CompositeExtractor
from librarian.llm import LazyLLMProvider, build_provider
from librarian.observability import NoOpMetricsRecorder
from librarian.pipeline.chunking import ChunkingPolicy
from librarian.prompts import PromptCatalog
from librarian.storage.sqlite import SQLiteDatabase, SQLiteRepository
from librarian.taxonomy.dewey import DeweyTaxonomy


@dataclass(frozen=True, slots=True)
class IngestContainer:
    """Application services needed before LLM-backed processing."""

    settings: Settings
    database: SQLiteDatabase
    repository: SQLiteRepository
    ingest_document: IngestDocument
    search_library: SearchLibrary


@dataclass(frozen=True, slots=True)
class ApplicationContainer(IngestContainer):
    """Composed application services."""

    process_document: ProcessDocument


async def build_ingest_container(
    settings: Settings | None = None,
    *,
    metrics: ApplicationMetrics | None = None,
) -> IngestContainer:
    """Build concrete application services that do not require an LLM provider."""
    resolved_settings = settings or Settings()
    database = SQLiteDatabase(resolved_settings.database_path)
    await database.initialize()
    repository = SQLiteRepository(database)
    extractor = CompositeExtractor(
        ocr_language=resolved_settings.ocr_language,
        ocr_timeout_seconds=resolved_settings.ocr_timeout_seconds,
        ocr_pdf_dpi=resolved_settings.ocr_pdf_dpi,
        ocr_pdf_max_pages=resolved_settings.ocr_pdf_max_pages,
        ocr_preprocess_mode=resolved_settings.ocr_preprocess_mode,
        ocr_threshold=resolved_settings.ocr_threshold,
        ocr_preserve_page_images=resolved_settings.ocr_preserve_page_images,
        ocr_correction_provider=LazyLLMProvider(resolved_settings, metrics=metrics),
        ocr_correction_mode=resolved_settings.ocr_llm_correction,
        ocr_correction_model=resolved_settings.ocr_llm_model or resolved_settings.llm_model,
        ocr_low_confidence_threshold=resolved_settings.ocr_low_confidence_threshold,
        ocr_max_correction_response_chars=resolved_settings.llm_max_response_chars,
        ocr_page_concurrency=resolved_settings.ocr_page_concurrency,
        ocr_fail_on_page_error=resolved_settings.ocr_fail_on_page_error,
        text_max_input_bytes=resolved_settings.text_max_input_bytes,
        docx_max_input_bytes=resolved_settings.docx_max_input_bytes,
        pdf_max_input_bytes=resolved_settings.pdf_max_input_bytes,
        pdf_max_pages=resolved_settings.pdf_max_pages,
        universal_max_input_bytes=resolved_settings.universal_max_input_bytes,
        universal_timeout_seconds=resolved_settings.universal_timeout_seconds,
        metrics=metrics,
    )
    ingest = IngestDocument(
        documents=repository,
        content=repository,
        extractor=extractor,
        max_source_bytes=resolved_settings.max_source_bytes,
    )
    return IngestContainer(
        settings=resolved_settings,
        database=database,
        repository=repository,
        ingest_document=ingest,
        search_library=SearchLibrary(repository),
    )


async def build_container(
    settings: Settings | None = None,
    *,
    metrics: ApplicationMetrics | None = None,
    tracer: Any | None = None,
) -> ApplicationContainer:
    """Build concrete application services."""
    ingest_container = await build_ingest_container(settings, metrics=metrics)
    resolved_settings = ingest_container.settings
    repository = ingest_container.repository
    provider = _build_provider(resolved_settings, metrics=metrics)
    cleaner = CleanChunks(
        provider=provider,
        prompt_catalog=PromptCatalog(),
        prompt_version=resolved_settings.cleaning_prompt_version,
        model=resolved_settings.llm_model,
        coherence_mode=resolved_settings.coherence_mode,
        max_parallel_chunks=resolved_settings.llm_max_concurrency,
        max_response_chars=resolved_settings.llm_max_response_chars,
    )
    taxonomy = DeweyTaxonomy()
    classifier = ClassifyDocument(
        provider=provider,
        prompt_catalog=PromptCatalog(),
        prompt_version=resolved_settings.classification_prompt_version,
        model=resolved_settings.llm_model,
        taxonomy=taxonomy,
        max_response_chars=resolved_settings.llm_max_response_chars,
    )
    policy = ChunkingPolicy(
        target_chars=resolved_settings.chunk_target_chars,
        overlap_chars=resolved_settings.chunk_overlap_chars,
    )
    process = ProcessDocument(
        documents=repository,
        runs=repository,
        chunks=repository,
        content=repository,
        outputs=repository,
        events=repository,
        cleaner=cleaner,
        classifier=classifier,
        chunking_policy=policy,
        metrics=metrics or NoOpMetricsRecorder(),
        tracer=tracer,
    )
    return ApplicationContainer(
        settings=resolved_settings,
        database=ingest_container.database,
        repository=repository,
        ingest_document=ingest_container.ingest_document,
        search_library=ingest_container.search_library,
        process_document=process,
    )


def _build_provider(
    settings: Settings,
    *,
    metrics: ApplicationMetrics | None = None,
) -> LLMProvider:
    return build_provider(settings, metrics=metrics)
