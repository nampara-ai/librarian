from pathlib import Path

import pytest

from librarian.application.factory import build_container
from librarian.config import Settings


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
