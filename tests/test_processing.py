import asyncio
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

    assert run.status == "succeeded"
    assert run.total_chunks == 1
    assert run.completed_chunks == 1
    assert output is not None
    assert "horse training transcript" in output.text
    assert classification is not None
    assert classification.code == "636.1"
    assert ingested.document.id in results

    second_run = await container.process_document.execute(ingested.document.id)
    events = await container.repository.list_events(second_run.id)

    assert any("1 cache hit(s)" in event for event in events)


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
async def test_malformed_search_query_is_controlled_error(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    container = await build_container(settings)

    with pytest.raises(ValueError, match="Invalid search query"):
        await container.repository.search('"')
