from pathlib import Path
from time import sleep

from fastapi.testclient import TestClient

from librarian.api.app import create_app
from librarian.config import Settings


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


def _wait_for_run(client: TestClient, run_id: str):
    response = client.get(f"/runs/{run_id}")
    for _ in range(40):
        if response.json()["status"] in {"succeeded", "failed", "canceled"}:
            return response
        sleep(0.05)
        response = client.get(f"/runs/{run_id}")
    return response
