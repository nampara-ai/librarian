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
from librarian.ingest.extractors import (
    CachingExtractor,
    CompositeExtractor,
    ExtractionCacheStore,
)
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
    taxonomy: DeweyTaxonomy


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
        ocr_rotation_retry=resolved_settings.ocr_rotation_retry,
        ocr_auto_orient=resolved_settings.ocr_auto_orient,
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
        pdf_engine=resolved_settings.pdf_engine,
        liteparse_ocr_server_url=resolved_settings.liteparse_ocr_server_url,
        liteparse_tessdata_path=resolved_settings.liteparse_tessdata_path,
        liteparse_dpi=resolved_settings.liteparse_dpi,
        liteparse_image_mode=resolved_settings.liteparse_image_mode,
        figure_vision_provider=(
            LazyLLMProvider(resolved_settings, metrics=metrics)
            if resolved_settings.figure_vision_enabled
            else None
        ),
        figure_vision_model=(
            resolved_settings.figure_vision_model or resolved_settings.llm_model
        ),
        figure_vision_max_figures=resolved_settings.figure_vision_max_figures,
        figure_vision_min_bytes=resolved_settings.figure_vision_min_bytes,
        figure_vision_max_bytes=resolved_settings.figure_vision_max_bytes,
        figure_vision_max_concurrency=resolved_settings.figure_vision_max_concurrency,
        figure_vision_max_response_chars=resolved_settings.figure_vision_max_response_chars,
        universal_max_input_bytes=resolved_settings.universal_max_input_bytes,
        universal_timeout_seconds=resolved_settings.universal_timeout_seconds,
        extraction_timeout_seconds=resolved_settings.extraction_timeout_seconds,
        metrics=metrics,
    )
    ingest_extractor: CachingExtractor | CompositeExtractor = extractor
    if resolved_settings.extraction_cache_enabled:
        ingest_extractor = CachingExtractor(
            extractor,
            repository,
            config_signature=extractor.config_signature,
        )
    ingest = IngestDocument(
        documents=repository,
        content=repository,
        extractor=ingest_extractor,
        max_source_bytes=resolved_settings.max_source_bytes,
    )
    return IngestContainer(
        settings=resolved_settings,
        database=database,
        repository=repository,
        ingest_document=ingest,
        search_library=SearchLibrary(repository),
        taxonomy=DeweyTaxonomy(),
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
        max_tokens=resolved_settings.llm_max_output_tokens,
        coherence_mode=resolved_settings.coherence_mode,
        max_parallel_chunks=resolved_settings.llm_max_concurrency,
        max_response_chars=resolved_settings.llm_max_response_chars,
        context_chars=resolved_settings.chunk_overlap_chars,
    )
    taxonomy = ingest_container.taxonomy
    classifier = ClassifyDocument(
        provider=provider,
        prompt_catalog=PromptCatalog(),
        prompt_version=resolved_settings.classification_prompt_version,
        model=resolved_settings.llm_model,
        taxonomy=taxonomy,
        max_tokens=resolved_settings.classification_max_output_tokens,
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
        taxonomy=ingest_container.taxonomy,
        process_document=process,
    )


def cache_wrap_extractor(
    extractor: CompositeExtractor,
    *,
    settings: Settings,
    cache_store: ExtractionCacheStore,
) -> CompositeExtractor | CachingExtractor:
    """Wrap an extractor with the content-hash cache when enabled.

    Used so the convert/import paths (not just direct ingest) benefit from the
    extraction cache — re-importing unchanged files then skips re-extraction.
    """
    if settings.extraction_cache_enabled:
        return CachingExtractor(
            extractor, cache_store, config_signature=extractor.config_signature
        )
    return extractor


def _build_provider(
    settings: Settings,
    *,
    metrics: ApplicationMetrics | None = None,
) -> LLMProvider:
    return build_provider(settings, metrics=metrics)
