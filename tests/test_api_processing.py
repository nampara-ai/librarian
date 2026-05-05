from pathlib import Path

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
    client = TestClient(create_app(settings))

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

    run_status = client.get(f"/runs/{run_id}")
    assert run_status.status_code == 200
    assert run_status.json()["status"] == "succeeded"

    content = client.get(f"/documents/{document_id}/content")
    assert content.status_code == 200
    assert "Horse transcript" in content.json()["text"]

    events = client.get(f"/runs/{run_id}/events")
    assert events.status_code == 200
    assert any("processing complete" in event for event in events.json()["events"])
