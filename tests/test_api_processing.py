import asyncio
from pathlib import Path
from time import sleep

import pytest
from fastapi.testclient import TestClient

from librarian.api.app import create_app
from librarian.application.factory import build_container
from librarian.application.ingest_document import raw_text_key
from librarian.application.jobs import QueueWorker
from librarian.config import Settings
from librarian.domain.ids import DocumentId
from librarian.storage.sqlite import SQLiteDatabase, SQLiteRunQueue


def test_api_upload_run_and_get_content(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    with TestClient(create_app(settings)) as client:
        upload = client.post(
            "/documents",
            files={"file": ("notes.txt", b"Horse transcript with rough um text.", "text/plain")},
        )
        assert upload.status_code == 200
        document_id = upload.json()["id"]

        run = client.post("/runs", json={"document_id": document_id})
        assert run.status_code == 200
        assert run.json()["status"] == "queued"
        run_id = run.json()["id"]

        run_status = _wait_for_run(client, run_id)
        assert run_status.status_code == 200
        assert run_status.json()["status"] == "succeeded"

        content = client.get(f"/documents/{document_id}/content")
        assert content.status_code == 200
        assert "Horse transcript" in content.json()["text"]

        events = client.get(f"/runs/{run_id}/events")
        assert events.status_code == 200
        assert any("processing complete" in event for event in events.json()["events"])

        stream = client.get(f"/runs/{run_id}/events/stream")
        assert stream.status_code == 200
        assert "event: done" in stream.text

        exported = client.get(f"/documents/{document_id}/export")
        assert exported.status_code == 200
        assert exported.json()["classification"] == "636.1 - Horses & Equines"

        exported_md = client.get(f"/documents/{document_id}/export?format=md")
        assert exported_md.status_code == 200
        assert "# notes" in exported_md.text


def test_api_key_auth(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_key="secret",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/documents").status_code == 401
        assert client.get("/documents", headers={"x-api-key": "secret"}).status_code == 200


def test_public_api_requires_key_and_import_root(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="API_KEY"):
        create_app(
            Settings(
                api_host="0.0.0.0",  # noqa: S104
                data_dir=tmp_path / ".librarian",
                database_path=tmp_path / ".librarian" / "librarian.sqlite",
            )
        )
    with pytest.raises(RuntimeError, match="API_IMPORT_ROOT"):
        create_app(
            Settings(
                api_host="0.0.0.0",  # noqa: S104
                api_key="secret",
                data_dir=tmp_path / ".librarian",
                database_path=tmp_path / ".librarian" / "librarian.sqlite",
            )
        )


def test_api_import_rejects_paths_outside_import_root(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=import_root,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/imports", json={"source_dir": str(outside)})

    assert response.status_code == 400
    assert "import root" in response.json()["detail"]


def test_api_import_rejects_escaping_subdirectory(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    source_dir = import_root / "input"
    source_dir.mkdir(parents=True)
    (source_dir / "notes.txt").write_text("Horse import transcript", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=import_root,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/imports",
            json={
                "source_dir": str(source_dir),
                "subdirectory_name": "../../escaped",
                "processing_mode": "none",
            },
        )

    assert response.status_code == 400
    assert not (tmp_path / "escaped").exists()


def test_api_upload_rejects_oversized_file(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_max_upload_bytes=4,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/documents",
            files={"file": ("notes.txt", b"too large", "text/plain")},
        )

    assert response.status_code == 413
    assert not list((tmp_path / ".librarian" / "uploads").glob("*"))


def test_api_uploads_with_same_filename_keep_distinct_source_files(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        first = client.post(
            "/documents",
            files={"file": ("same.txt", b"first", "text/plain")},
        )
        second = client.post(
            "/documents",
            files={"file": ("same.txt", b"second", "text/plain")},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    uploads = sorted((tmp_path / ".librarian" / "uploads").glob("*/same.txt"))
    assert len(uploads) == 2
    assert {item.read_text(encoding="utf-8") for item in uploads} == {"first", "second"}


def test_api_upload_with_adversarial_filename_uses_safe_fallback(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/documents",
            files={"file": ("..", b"safe text", "text/plain")},
        )

    assert response.status_code == 200
    assert response.json()["filename"] == "upload.txt"
    uploads = sorted((tmp_path / ".librarian" / "uploads").glob("*/upload.txt"))
    assert len(uploads) == 1
    assert uploads[0].read_text(encoding="utf-8") == "safe text"


def test_api_upload_rejects_unsupported_file_and_cleans_upload(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/documents",
            files={"file": ("bad.bin", b"not supported", "application/octet-stream")},
        )

    assert response.status_code == 400
    assert "Unsupported file extension" in response.json()["detail"]
    assert not list((tmp_path / ".librarian" / "uploads").glob("*"))


def test_api_config_exposes_operational_controls(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_max_upload_bytes=1234,
        ocr_language="eng",
        universal_timeout_seconds=77,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_max_upload_bytes"] == 1234
    assert payload["ocr_language"] == "eng"
    assert payload["universal_timeout_seconds"] == 77
    assert payload["llm_max_retries"] == settings.llm_max_retries


def test_api_delete_removes_raw_blob_and_owned_upload(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        upload = client.post(
            "/documents",
            files={"file": ("notes.txt", b"Horse private source text", "text/plain")},
        )
        assert upload.status_code == 200
        document_id = upload.json()["id"]
        assert list((tmp_path / ".librarian" / "uploads").glob("*/notes.txt"))

        deleted = client.delete(f"/documents/{document_id}")

    assert deleted.status_code == 200
    container = asyncio.run(build_container(settings))
    with pytest.raises(KeyError):
        asyncio.run(container.repository.get_text(raw_text_key(DocumentId(document_id))))
    assert not list((tmp_path / ".librarian" / "uploads").glob("*/notes.txt"))


def test_api_document_pagination(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        for index in range(3):
            response = client.post(
                "/documents",
                files={"file": (f"notes-{index}.txt", f"Text {index}".encode(), "text/plain")},
            )
            assert response.status_code == 200

        page = client.get("/documents?limit=2&offset=1")

        assert page.status_code == 200
        assert page.json()["total"] == 3
        assert page.json()["limit"] == 2
        assert page.json()["offset"] == 1
        assert len(page.json()["documents"]) == 2


def test_api_queued_run_processed_by_worker(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        job_backend="sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    with TestClient(create_app(settings)) as client:
        upload = client.post(
            "/documents",
            files={"file": ("notes.txt", b"Horse transcript.", "text/plain")},
        )
        document_id = upload.json()["id"]
        run = client.post("/runs", json={"document_id": document_id})
        assert run.status_code == 200
        run_id = run.json()["id"]

    async def execute_worker() -> None:
        database = SQLiteDatabase(settings.database_path)
        await database.initialize()
        container = await build_container(settings)
        worker = QueueWorker(
            queue=SQLiteRunQueue(database),
            processor=container.process_document.execute_existing,
            worker_id="test-worker",
        )
        assert await worker.run_once()

    import asyncio

    asyncio.run(execute_worker())

    with TestClient(create_app(settings)) as client:
        status = client.get(f"/runs/{run_id}")
        assert status.json()["status"] == "succeeded"
        cancel = client.post(f"/runs/{run_id}/cancel")
        assert cancel.status_code == 400
        assert "terminal" in cancel.json()["detail"]


def test_api_import_endpoint_and_run_controls(tmp_path: Path) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "notes.txt").write_text("Horse import transcript", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=tmp_path,
    )
    with TestClient(create_app(settings)) as client:
        imported = client.post(
            "/imports",
            json={
                "source_dir": str(source_dir),
                "format": "md",
                "processing_mode": "none",
            },
        )
        assert imported.status_code == 200
        assert imported.json()["ingested"] == 1
        document_id = imported.json()["items"][0]["document_id"]

        reprocess = client.post(f"/documents/{document_id}/reprocess")
        assert reprocess.status_code == 200
        cancel = client.post(f"/runs/{reprocess.json()['id']}/cancel")
        assert cancel.status_code == 200
        assert cancel.json()["status"] == "canceled"
        runs = client.get("/runs?limit=10")
        assert runs.status_code == 200
        assert runs.json()["runs"]

        deleted = client.delete(f"/documents/{document_id}")
        assert deleted.status_code == 200


def test_api_import_default_processes_without_external_worker(tmp_path: Path) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "notes.txt").write_text("Horse import transcript", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=tmp_path,
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    with TestClient(create_app(settings)) as client:
        imported = client.post(
            "/imports",
            json={
                "source_dir": str(source_dir),
                "format": "md",
            },
        )

    assert imported.status_code == 200
    assert imported.json()["processed"] == 1
    assert imported.json()["queued"] == 0


def test_api_import_rejects_unrunnable_queue_mode(tmp_path: Path) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "notes.txt").write_text("Horse import transcript", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=tmp_path,
        job_backend="in-process",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/imports",
            json={
                "source_dir": str(source_dir),
                "processing_mode": "queue",
            },
        )

    assert response.status_code == 400
    assert "JOB_BACKEND=sqlite" in response.json()["detail"]


def test_api_import_new_directory_requires_output_dir(tmp_path: Path) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "notes.txt").write_text("Horse import transcript", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=tmp_path,
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.post(
            "/imports",
            json={
                "source_dir": str(source_dir),
                "output_mode": "new-directory",
                "processing_mode": "none",
            },
        )

    assert response.status_code == 400
    assert "output_dir" in response.json()["detail"]


def test_api_import_rejects_missing_or_non_directory_source(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    file_source = import_root / "notes.txt"
    file_source.write_text("Horse import transcript", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=import_root,
    )
    with TestClient(create_app(settings)) as client:
        missing = client.post(
            "/imports",
            json={"source_dir": str(import_root / "missing"), "processing_mode": "none"},
        )
        file_response = client.post(
            "/imports",
            json={"source_dir": str(file_source), "processing_mode": "none"},
        )

    assert missing.status_code == 400
    assert file_response.status_code == 400
    assert "existing directory" in missing.json()["detail"]


def test_api_malformed_search_query_returns_400(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/search", json={"query": '"'})

    assert response.status_code == 400


def test_api_search_rejects_out_of_range_limits(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        negative = client.post("/search", json={"query": "horse", "limit": -1})
        huge = client.post("/search", json={"query": "horse", "limit": 100_000})

    assert negative.status_code == 422
    assert huge.status_code == 422


def _wait_for_run(client: TestClient, run_id: str):
    response = client.get(f"/runs/{run_id}")
    for _ in range(40):
        if response.json()["status"] in {"succeeded", "failed", "canceled"}:
            return response
        sleep(0.05)
        response = client.get(f"/runs/{run_id}")
    return response
