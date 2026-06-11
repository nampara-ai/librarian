import asyncio
import logging
from pathlib import Path
from typing import Any, cast

import pytest

from librarian.application.classify_document import ClassifyDocument
from librarian.application.clean_chunks import CleanChunks
from librarian.application.factory import build_container
from librarian.application.ports import EventSink, OutputRepository
from librarian.application.process_document import ProcessDocument
from librarian.config import Settings
from librarian.domain.ids import ChunkId, DocumentId, RunId
from librarian.domain.models import (
    Chunk,
    Classification,
    CleanedOutput,
    DocumentStatus,
    RunStage,
    RunStatus,
)
from librarian.prompts import PromptCatalog
from librarian.taxonomy.dewey import DeweyTaxonomy


class FakeSpan:
    def __init__(self, name: str, attributes: dict[str, object]) -> None:
        self.name = name
        self.attributes = dict(attributes)

    def __enter__(self) -> "FakeSpan":
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


class FakeTracer:
    def __init__(self) -> None:
        self.spans: list[FakeSpan] = []

    def start_as_current_span(
        self,
        name: str,
        *,
        attributes: dict[str, object],
    ) -> FakeSpan:
        span = FakeSpan(name, attributes)
        self.spans.append(span)
        return span


@pytest.mark.asyncio
async def test_ingest_process_and_search_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "horse-notes.txt"
    source.write_text(
        "This is a rough horse training transcript. um The colt needs groundwork.",
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    container = await build_container(settings)

    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.execute(ingested.document.id)
    output = await container.repository.get_cleaned_output(ingested.document.id)
    classification = await container.repository.get_classification(ingested.document.id)
    results = await container.repository.search("horse")
    detailed_results = await container.repository.search_results("horse")
    classification_filtered = await container.repository.search(
        "horse",
        classification_code="636.1",
    )
    filename_filtered = await container.repository.search(
        "horse",
        filename_contains="horse-notes",
    )
    status_filtered = await container.repository.search(
        "horse",
        document_status=DocumentStatus.READY,
    )
    raw_results = await container.repository.search_results(
        "rough horse",
        scope="raw",
        filename_contains="horse-notes",
    )
    cleaned_facets = await container.repository.search_facets("horse")
    raw_facets = await container.repository.search_facets("rough horse", scope="raw")
    wrong_classification = await container.repository.search(
        "horse",
        classification_code="000.0",
    )

    assert run.status == "succeeded"
    assert run.total_chunks == 1
    assert run.completed_chunks == 1
    assert output is not None
    assert "horse training transcript" in output.text
    assert classification is not None
    assert classification.code == "636.1"
    assert ingested.document.id in results
    assert detailed_results[0].document_id == ingested.document.id
    assert detailed_results[0].run_id == run.id
    assert detailed_results[0].filename == "horse-notes.txt"
    assert detailed_results[0].document_status == DocumentStatus.READY
    assert detailed_results[0].classification_code == "636.1"
    assert "<mark>horse</mark>" in detailed_results[0].snippet.casefold()
    assert ingested.document.id in classification_filtered
    assert ingested.document.id in filename_filtered
    assert ingested.document.id in status_filtered
    assert raw_results[0].document_id == ingested.document.id
    assert raw_results[0].run_id is None
    assert raw_results[0].source == "raw"
    assert "<mark>rough</mark>" in raw_results[0].snippet
    assert cleaned_facets.classifications[0].value == "636.1"
    assert cleaned_facets.statuses[0].value == "ready"
    assert cleaned_facets.sources[0].value == "cleaned"
    assert cleaned_facets.filenames[0].value == "horse-notes.txt"
    assert raw_facets.sources[0].value == "raw"
    assert ingested.document.id not in wrong_classification

    second_run = await container.process_document.execute(ingested.document.id)
    events = await container.repository.list_events(second_run.id)

    assert any("1 cache hit(s)" in event for event in events)


@pytest.mark.asyncio
async def test_processing_logs_structured_run_stage_metrics(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = tmp_path / "horse-notes.txt"
    source.write_text("Horse transcript for structured run logs.", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)

    with caplog.at_level(logging.INFO, logger="librarian.application.process_document"):
        run = await container.process_document.execute(ingested.document.id)

    stage_records = [
        record for record in caplog.records if record.getMessage() == "run_stage_finished"
    ]
    assert any(cast(Any, record).stage == "clean" for record in stage_records)
    clean_record = cast(
        Any,
        next(record for record in stage_records if cast(Any, record).stage == "clean"),
    )
    assert clean_record.run_id == str(run.id)
    assert clean_record.document_id == str(ingested.document.id)
    assert clean_record.status == "succeeded"
    assert clean_record.duration_ms >= 0
    assert source.read_text(encoding="utf-8") not in caplog.text


@pytest.mark.asyncio
async def test_processing_emits_stage_tracing_spans(tmp_path: Path) -> None:
    source = tmp_path / "horse-notes.txt"
    source.write_text("Horse transcript for tracing spans.", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    tracer = FakeTracer()
    container = await build_container(settings, tracer=tracer)
    ingested = await container.ingest_document.execute(source)

    run = await container.process_document.execute(ingested.document.id)

    stage_spans = [span for span in tracer.spans if span.name == "librarian.run_stage"]
    assert {span.attributes["librarian.stage"] for span in stage_spans} >= {
        "ingest",
        "clean",
        "classify",
        "index",
    }
    clean_span = next(
        span for span in stage_spans if span.attributes["librarian.stage"] == "clean"
    )
    assert clean_span.attributes["librarian.run_id"] == str(run.id)
    assert clean_span.attributes["librarian.document_id"] == str(ingested.document.id)
    assert clean_span.attributes["librarian.status"] == "succeeded"
    assert isinstance(clean_span.attributes["librarian.duration_ms"], float)


@pytest.mark.asyncio
async def test_search_normalizes_punctuation_and_hyphenated_queries(tmp_path: Path) -> None:
    source = tmp_path / "horse-notes.txt"
    source.write_text(
        "This transcript mentions canter transitions, saddle fit, and follow-up care.",
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)

    await container.process_document.execute(ingested.document.id)
    hyphenated = await container.repository.search("canter-transitions")
    punctuated = await container.repository.search("follow-up care?!")

    assert ingested.document.id in hyphenated
    assert ingested.document.id in punctuated


@pytest.mark.asyncio
async def test_search_supports_quoted_exact_phrases(tmp_path: Path) -> None:
    first = tmp_path / "first.txt"
    first.write_text(
        "This transcript mentions follow up care after a clinic visit.",
        encoding="utf-8",
    )
    second = tmp_path / "second.txt"
    second.write_text("This transcript mentions care and then follow up later.", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    container = await build_container(settings)
    first_ingested = await container.ingest_document.execute(first)
    second_ingested = await container.ingest_document.execute(second)

    await container.process_document.execute(first_ingested.document.id)
    await container.process_document.execute(second_ingested.document.id)
    loose = await container.repository.search("follow-up care")
    exact = await container.repository.search('"follow-up care"')

    assert first_ingested.document.id in loose
    assert second_ingested.document.id in loose
    assert first_ingested.document.id in exact
    assert second_ingested.document.id not in exact


@pytest.mark.asyncio
async def test_search_results_include_transcript_timestamp_citation(tmp_path: Path) -> None:
    source = tmp_path / "captions.srt"
    source.write_text(
        "1\n"
        "00:00:05,000 --> 00:00:08,000\n"
        "Ada: Opening remarks about saddle fit.\n\n"
        "2\n"
        "00:00:08,000 --> 00:00:12,000\n"
        "The follow up care plan starts tomorrow.\n",
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)

    results = await container.repository.search_results(
        '"follow up care plan"',
        scope="raw",
    )

    assert results[0].document_id == ingested.document.id
    assert results[0].transcript_citation is not None
    assert results[0].transcript_citation.start_seconds == 8.0
    assert results[0].transcript_citation.end_seconds == 12.0
    assert results[0].transcript_citation.strategy == "exact-normalized"
    assert "follow up care plan" in results[0].transcript_citation.matched_text


@pytest.mark.asyncio
async def test_identical_chunks_from_different_documents_do_not_collide(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    container = await build_container(settings)
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("Shared horse transcript.", encoding="utf-8")
    second.write_text("Shared horse transcript.\n", encoding="utf-8")

    first_doc = await container.ingest_document.execute(first)
    second_doc = await container.ingest_document.execute(second)
    await container.process_document.execute(first_doc.document.id)
    await container.process_document.execute(second_doc.document.id)

    assert len(await container.repository.list_for_document(first_doc.document.id)) == 1
    assert len(await container.repository.list_for_document(second_doc.document.id)) == 1


@pytest.mark.asyncio
async def test_canceled_run_cannot_be_executed(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)
    await container.repository.update_status(
        run.id,
        status=RunStatus.CANCELED,
        stage=RunStage.COMPLETE,
        error="canceled by user",
    )

    with pytest.raises(ValueError, match="terminal"):
        await container.process_document.execute_existing(run.id)


@pytest.mark.asyncio
async def test_running_run_observes_cancellation_without_succeeding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)
    calls = 0
    original_is_canceled = container.repository.is_run_canceled

    async def cancel_after_start(run_id: RunId) -> bool:
        nonlocal calls
        calls += 1
        if calls >= 2:
            await container.repository.update_status(
                run_id,
                status=RunStatus.CANCELED,
                stage=RunStage.COMPLETE,
                error="canceled by user",
            )
        return await original_is_canceled(run_id)

    monkeypatch.setattr(container.repository, "is_run_canceled", cancel_after_start)

    with pytest.raises(RuntimeError, match="Run canceled"):
        await container.process_document.execute_existing(run.id)

    latest = await container.repository.get_run(run.id)
    document = await container.repository.get_document(ingested.document.id)
    output = await container.repository.get_cleaned_output(ingested.document.id)
    assert latest is not None
    assert document is not None
    assert latest.status == RunStatus.CANCELED
    assert document.status == DocumentStatus.INGESTED
    assert output is None


@pytest.mark.asyncio
async def test_balanced_coherence_carries_context_within_parallel_groups() -> None:
    class RecordingProvider:
        name = "recording"

        def __init__(self) -> None:
            self.prompts: list[str] = []

        async def complete(
            self,
            *,
            system_prompt: str,
            user_prompt: str,
            model: str,
            max_tokens: int,
            temperature: float,
        ) -> str:
            del system_prompt, model, max_tokens, temperature
            self.prompts.append(user_prompt)
            return user_prompt

    provider = RecordingProvider()
    cleaner = CleanChunks(
        provider=provider,
        prompt_catalog=PromptCatalog(),
        prompt_version="cmos_v1",
        model="test",
        coherence_mode="balanced",
        max_parallel_chunks=2,
        balanced_group_size=2,
    )
    chunks = [
        Chunk(
            id=ChunkId(f"chk_{index}"),
            document_id=DocumentId("doc_test"),
            ordinal=index,
            text=f"chunk {index}",
            start_char=index,
            end_char=index + 1,
            sha256=f"sha-{index}",
        )
        for index in range(4)
    ]

    await cleaner.execute(chunks)

    assert provider.prompts[0] == "chunk 0"
    assert provider.prompts[1].startswith("[CONTEXT:")
    assert "chunk 0" in provider.prompts[1]
    assert provider.prompts[2] == "chunk 2"
    assert provider.prompts[3].startswith("[CONTEXT:")
    assert "chunk 2" in provider.prompts[3]


@pytest.mark.asyncio
async def test_clean_chunks_preserves_markdown_quality_warnings() -> None:
    class CollapsingProvider:
        name = "collapsing"

        async def complete(
            self,
            *,
            system_prompt: str,
            user_prompt: str,
            model: str,
            max_tokens: int,
            temperature: float,
        ) -> str:
            del system_prompt, user_prompt, model, max_tokens, temperature
            return "Visit Notes First paragraph. Second paragraph. First item Second item A B 1 2"

    chunk = Chunk(
        id=ChunkId("chk_quality"),
        document_id=DocumentId("doc_quality"),
        ordinal=0,
        text=(
            "# Visit Notes\n\n"
            "First paragraph.\n\n"
            "Second paragraph with [1].\n\n"
            "- First item\n"
            "- Second item\n\n"
            "| A | B |\n"
            "|---|---|\n"
            "| 1 | 2 |"
        ),
        start_char=0,
        end_char=120,
        sha256="sha-quality",
    )
    cleaner = CleanChunks(
        provider=CollapsingProvider(),
        prompt_catalog=PromptCatalog(),
        prompt_version="cmos_v1",
        model="test",
    )

    results = await cleaner.execute([chunk])

    assert "missing-markdown-heading" in results[0].warnings
    assert "missing-markdown-list" in results[0].warnings
    assert "missing-markdown-table" in results[0].warnings
    assert "missing-citation-marker" in results[0].warnings


@pytest.mark.asyncio
async def test_clean_chunks_rejects_oversized_provider_response() -> None:
    class OversizedProvider:
        name = "oversized"

        async def complete(
            self,
            *,
            system_prompt: str,
            user_prompt: str,
            model: str,
            max_tokens: int,
            temperature: float,
        ) -> str:
            del system_prompt, user_prompt, model, max_tokens, temperature
            return "x" * 11

    chunk = Chunk(
        id=ChunkId("chk_oversized"),
        document_id=DocumentId("doc_oversized"),
        ordinal=0,
        text="Horse transcript.",
        start_char=0,
        end_char=17,
        sha256="sha-oversized",
    )
    cleaner = CleanChunks(
        provider=OversizedProvider(),
        prompt_catalog=PromptCatalog(),
        prompt_version="cmos_v1",
        model="test",
        max_response_chars=10,
    )

    with pytest.raises(ValueError, match="cleaning provider response exceeded"):
        await cleaner.execute([chunk])


@pytest.mark.asyncio
async def test_classifier_rejects_oversized_provider_response() -> None:
    class OversizedProvider:
        name = "oversized"

        async def complete(
            self,
            *,
            system_prompt: str,
            user_prompt: str,
            model: str,
            max_tokens: int,
            temperature: float,
        ) -> str:
            del system_prompt, user_prompt, model, max_tokens, temperature
            return "x" * 11

    classifier = ClassifyDocument(
        provider=OversizedProvider(),
        prompt_catalog=PromptCatalog(),
        prompt_version="dewey_v1",
        model="test",
        taxonomy=DeweyTaxonomy(),
        max_response_chars=10,
    )

    with pytest.raises(ValueError, match="classification provider response exceeded"):
        await classifier.execute(DocumentId("doc_oversized"), "Horse transcript.")


@pytest.mark.asyncio
async def test_canceled_run_does_not_publish_classification_or_search(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript with uniquecancelterm.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)

    class CancelingProvider:
        name = container.process_document.classifier.provider.name

        def __init__(self) -> None:
            self._provider = container.process_document.classifier.provider

        async def complete(
            self,
            *,
            system_prompt: str,
            user_prompt: str,
            model: str,
            max_tokens: int,
            temperature: float,
        ) -> str:
            response = await self._provider.complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            await container.repository.update_status(
                run.id,
                status=RunStatus.CANCELED,
                stage=RunStage.COMPLETE,
                error="canceled by user",
            )
            return response

    classifier = container.process_document.classifier
    canceling_classifier = ClassifyDocument(
        provider=CancelingProvider(),
        prompt_catalog=classifier.prompt_catalog,
        prompt_version=classifier.prompt_version,
        model=classifier.model,
        taxonomy=classifier.taxonomy,
    )

    process = ProcessDocument(
        documents=container.repository,
        runs=container.repository,
        chunks=container.repository,
        content=container.repository,
        outputs=container.repository,
        events=container.repository,
        cleaner=container.process_document.cleaner,
        classifier=canceling_classifier,
        chunking_policy=container.process_document.chunking_policy,
    )

    with pytest.raises(RuntimeError, match="Run canceled"):
        await process.execute_existing(run.id)

    latest = await container.repository.get_run(run.id)
    output = await container.repository.get_cleaned_output(ingested.document.id)
    classification = await container.repository.get_classification(ingested.document.id)
    results = await container.repository.search("uniquecancelterm")
    assert latest is not None
    assert latest.status == RunStatus.CANCELED
    assert output is None
    assert classification is None
    assert ingested.document.id not in results


@pytest.mark.asyncio
async def test_late_publish_failure_marks_run_failed_and_hides_output(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript with index failure.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)

    class FailingPublisher:
        def __init__(self, wrapped: object) -> None:
            self._wrapped = wrapped

        def __getattr__(self, name: str) -> Any:
            return getattr(self._wrapped, name)

        async def publish_successful_run(
            self,
            output: CleanedOutput,
            classification: Classification,
        ) -> None:
            del output, classification
            raise RuntimeError("publish boom")

    process = ProcessDocument(
        documents=container.repository,
        runs=container.repository,
        chunks=container.repository,
        content=container.repository,
        outputs=cast(OutputRepository, FailingPublisher(container.repository)),
        events=container.repository,
        cleaner=container.process_document.cleaner,
        classifier=container.process_document.classifier,
        chunking_policy=container.process_document.chunking_policy,
    )

    with pytest.raises(RuntimeError, match="publish boom"):
        await process.execute_existing(run.id)

    latest = await container.repository.get_run(run.id)
    document = await container.repository.get_document(ingested.document.id)
    output = await container.repository.get_cleaned_output(ingested.document.id)
    classification = await container.repository.get_classification(ingested.document.id)
    assert latest is not None
    assert document is not None
    assert latest.status == RunStatus.FAILED
    assert latest.error == "publish boom"
    assert document.status == DocumentStatus.FAILED
    assert output is None
    assert classification is None


@pytest.mark.asyncio
async def test_start_event_failure_marks_run_failed(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript with event failure.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)

    class FailingEvents:
        def __init__(self, wrapped: object) -> None:
            self._wrapped = wrapped

        def __getattr__(self, name: str) -> Any:
            return getattr(self._wrapped, name)

        async def emit(self, run_id: RunId, stage: RunStage, message: str) -> None:
            del run_id, stage
            if message == "started processing run":
                raise RuntimeError("event boom")

    process = ProcessDocument(
        documents=container.repository,
        runs=container.repository,
        chunks=container.repository,
        content=container.repository,
        outputs=container.repository,
        events=cast(EventSink, FailingEvents(container.repository)),
        cleaner=container.process_document.cleaner,
        classifier=container.process_document.classifier,
        chunking_policy=container.process_document.chunking_policy,
    )

    with pytest.raises(RuntimeError, match="event boom"):
        await process.execute_existing(run.id)

    latest = await container.repository.get_run(run.id)
    document = await container.repository.get_document(ingested.document.id)
    assert latest is not None
    assert document is not None
    assert latest.status == RunStatus.FAILED
    assert latest.error == "event boom"
    assert document.status == DocumentStatus.FAILED


@pytest.mark.asyncio
async def test_start_event_failure_marks_created_run_failed(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript with start event failure.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)

    class FailingEvents:
        def __init__(self, wrapped: object) -> None:
            self._wrapped = wrapped

        def __getattr__(self, name: str) -> Any:
            return getattr(self._wrapped, name)

        async def emit(self, run_id: RunId, stage: RunStage, message: str) -> None:
            del run_id, stage, message
            raise RuntimeError("queued event boom")

    process = ProcessDocument(
        documents=container.repository,
        runs=container.repository,
        chunks=container.repository,
        content=container.repository,
        outputs=container.repository,
        events=cast(EventSink, FailingEvents(container.repository)),
        cleaner=container.process_document.cleaner,
        classifier=container.process_document.classifier,
        chunking_policy=container.process_document.chunking_policy,
    )

    with pytest.raises(RuntimeError, match="queued event boom"):
        await process.start(ingested.document.id)

    runs = await container.repository.list_runs(limit=10)
    assert len(runs) == 1
    assert runs[0].status == RunStatus.FAILED
    assert runs[0].error == "queued event boom"


@pytest.mark.asyncio
async def test_processing_persisted_errors_are_redacted_and_bounded(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript with sensitive provider failure.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)

    class FailingEvents:
        async def emit(self, run_id: RunId, stage: RunStage, message: str) -> None:
            del run_id, stage, message
            raise RuntimeError("api_key=abc123 " + ("private transcript text " * 100))

    process = ProcessDocument(
        documents=container.repository,
        runs=container.repository,
        chunks=container.repository,
        content=container.repository,
        outputs=container.repository,
        events=cast(EventSink, FailingEvents()),
        cleaner=container.process_document.cleaner,
        classifier=container.process_document.classifier,
        chunking_policy=container.process_document.chunking_policy,
    )

    with pytest.raises(RuntimeError, match="api_key=abc123"):
        await process.execute_existing(run.id)

    latest = await container.repository.get_run(run.id)
    assert latest is not None
    assert latest.error is not None
    assert "api_key=[REDACTED]" in latest.error
    assert "abc123" not in latest.error
    assert latest.error.endswith("...[truncated]")
    assert len(latest.error) <= 1_014


@pytest.mark.asyncio
async def test_task_cancellation_marks_run_failed_and_restores_document_status(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript with task cancellation.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    run = await container.process_document.start(ingested.document.id)
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingProvider:
        name = container.process_document.cleaner.provider.name

        async def complete(
            self,
            *,
            system_prompt: str,
            user_prompt: str,
            model: str,
            max_tokens: int,
            temperature: float,
        ) -> str:
            del system_prompt, user_prompt, model, max_tokens, temperature
            started.set()
            await release.wait()
            return "never reached"

    cleaner = container.process_document.cleaner
    blocking_cleaner = type(cleaner)(
        provider=BlockingProvider(),
        prompt_catalog=cleaner.prompt_catalog,
        prompt_version=cleaner.prompt_version,
        model=cleaner.model,
        max_tokens=cleaner.max_tokens,
        temperature=cleaner.temperature,
        coherence_mode=cleaner.coherence_mode,
        max_parallel_chunks=cleaner.max_parallel_chunks,
    )
    process = ProcessDocument(
        documents=container.repository,
        runs=container.repository,
        chunks=container.repository,
        content=container.repository,
        outputs=container.repository,
        events=container.repository,
        cleaner=blocking_cleaner,
        classifier=container.process_document.classifier,
        chunking_policy=container.process_document.chunking_policy,
    )
    task = asyncio.create_task(process.execute_existing(run.id))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    latest = await container.repository.get_run(run.id)
    document = await container.repository.get_document(ingested.document.id)
    assert latest is not None
    assert document is not None
    assert latest.status == RunStatus.FAILED
    assert latest.error == "processing canceled by task cancellation"
    assert document.status == DocumentStatus.FAILED


@pytest.mark.asyncio
async def test_failed_reprocess_preserves_ready_document_status(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    source = tmp_path / "notes.txt"
    source.write_text("Horse transcript with existing good output.", encoding="utf-8")
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)
    await container.process_document.execute(ingested.document.id)
    retry = await container.process_document.start(ingested.document.id)

    class FailingProvider:
        name = container.process_document.classifier.provider.name

        async def complete(
            self,
            *,
            system_prompt: str,
            user_prompt: str,
            model: str,
            max_tokens: int,
            temperature: float,
        ) -> str:
            del system_prompt, user_prompt, model, max_tokens, temperature
            raise RuntimeError("classify boom")

    classifier = container.process_document.classifier
    failing_classifier = ClassifyDocument(
        provider=FailingProvider(),
        prompt_catalog=classifier.prompt_catalog,
        prompt_version=classifier.prompt_version,
        model=classifier.model,
        taxonomy=classifier.taxonomy,
    )

    process = ProcessDocument(
        documents=container.repository,
        runs=container.repository,
        chunks=container.repository,
        content=container.repository,
        outputs=container.repository,
        events=container.repository,
        cleaner=container.process_document.cleaner,
        classifier=failing_classifier,
        chunking_policy=container.process_document.chunking_policy,
    )

    with pytest.raises(RuntimeError, match="classify boom"):
        await process.execute_existing(retry.id)

    failed_run = await container.repository.get_run(retry.id)
    document = await container.repository.get_document(ingested.document.id)
    output = await container.repository.get_cleaned_output(ingested.document.id)
    assert failed_run is not None
    assert document is not None
    assert failed_run.status == RunStatus.FAILED
    assert document.status == DocumentStatus.READY
    assert output is not None


@pytest.mark.asyncio
async def test_failed_extraction_does_not_persist_document(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    source = tmp_path / "notes.bin"
    source.write_bytes(b"not supported")
    container = await build_container(settings)

    with pytest.raises(ValueError, match="Unsupported file extension"):
        await container.ingest_document.execute(source)

    assert list(await container.repository.list()) == []


@pytest.mark.asyncio
async def test_ingest_rejects_source_above_size_limit(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        max_source_bytes=4,
    )
    source = tmp_path / "notes.txt"
    source.write_text("too large", encoding="utf-8")
    container = await build_container(settings)

    with pytest.raises(ValueError, match="Source file exceeds"):
        await container.ingest_document.execute(source)

    assert list(await container.repository.list()) == []


@pytest.mark.asyncio
async def test_ingest_uses_bounded_source_read_when_limit_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        max_source_bytes=10,
    )
    source = tmp_path / "notes.txt"
    source.write_text("safe", encoding="utf-8")
    container = await build_container(settings)

    def fail_read_bytes(self: Path) -> bytes:
        raise AssertionError(f"unbounded read_bytes called for {self}")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)

    ingested = await container.ingest_document.execute(source)

    assert ingested.raw_text == "safe"
    assert ingested.document.source.byte_size == 4


@pytest.mark.asyncio
async def test_malformed_search_query_is_controlled_error(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)

    with pytest.raises(ValueError, match="Invalid search query"):
        await container.repository.search('"')


@pytest.mark.asyncio
@pytest.mark.parametrize("coherence_mode", ["fast", "balanced", "max-coherence"])
async def test_clean_chunks_reports_per_chunk_progress(coherence_mode: str) -> None:
    class EchoProvider:
        name = "echo"

        async def complete(
            self,
            *,
            system_prompt: str,
            user_prompt: str,
            model: str,
            max_tokens: int,
            temperature: float,
        ) -> str:
            del system_prompt, model, max_tokens, temperature
            return user_prompt

    chunks = [
        Chunk(
            id=ChunkId(f"chk_progress_{index}"),
            document_id=DocumentId("doc_progress"),
            ordinal=index,
            text=f"Chunk {index} text.",
            start_char=index * 10,
            end_char=index * 10 + 9,
            sha256=f"sha-progress-{index}",
        )
        for index in range(5)
    ]
    cleaner = CleanChunks(
        provider=EchoProvider(),
        prompt_catalog=PromptCatalog(),
        prompt_version="cmos_v2",
        model="echo-model",
        coherence_mode=cast(Any, coherence_mode),
        max_parallel_chunks=2,
        balanced_group_size=2,
    )
    completions = 0

    async def note() -> None:
        nonlocal completions
        completions += 1

    cleaned = await cleaner.execute(chunks, on_chunk_cleaned=note)

    assert len(cleaned) == len(chunks)
    assert completions == len(chunks)


@pytest.mark.asyncio
async def test_processing_persists_incremental_chunk_progress(tmp_path: Path) -> None:
    source = tmp_path / "progress-notes.txt"
    source.write_text(
        " ".join(f"Sentence number {index} about training horses." for index in range(60)),
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=120,
        chunk_overlap_chars=10,
    )
    container = await build_container(settings)
    ingested = await container.ingest_document.execute(source)

    observed: list[int] = []
    repository = container.repository
    original_update = repository.update_run_progress

    async def recording_update(run_id: RunId, **kwargs: Any) -> None:
        observed.append(int(kwargs["completed_chunks"]))
        await original_update(run_id, **kwargs)

    object.__setattr__(repository, "update_run_progress", recording_update)

    run = await container.process_document.execute(ingested.document.id)

    assert run.status == RunStatus.SUCCEEDED
    assert run.total_chunks > 1
    # Intermediate values must be persisted while cleaning, not just the
    # final total: strictly increasing counts ending at the chunk total.
    increasing = [value for value in observed if value <= run.total_chunks]
    assert len(observed) >= run.total_chunks
    assert sorted(set(increasing)) == list(range(min(increasing), run.total_chunks + 1))
    assert observed[-1] == run.total_chunks
