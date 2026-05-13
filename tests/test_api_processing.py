import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import sleep

import pytest
from fastapi.testclient import TestClient

import librarian.api.app as api_app
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
        assert content.json()["offset"] == 0
        assert content.json()["limit"] == settings.api_max_content_chars
        assert content.json()["total_chars"] >= len(content.json()["text"])
        assert content.json()["truncated"] is False
        content_page = client.get(f"/documents/{document_id}/content", params={"limit": 5})
        assert content_page.status_code == 200
        assert content_page.json()["limit"] == 5
        assert len(content_page.json()["text"]) == 5

        search = client.post("/search/results", json={"query": "Horse transcript"})
        assert search.status_code == 200
        assert search.json()["total"] == 1
        assert search.json()["limit"] == 20
        assert search.json()["offset"] == 0
        assert search.json()["results"][0]["document_id"] == document_id
        assert search.json()["results"][0]["source"] == "cleaned"
        assert search.json()["results"][0]["run_id"] == run_id
        assert search.json()["results"][0]["filename"] == "notes.txt"
        assert search.json()["results"][0]["document_status"] == "ready"
        assert search.json()["results"][0]["classification_code"] == "636.1"
        assert "<mark>Horse</mark>" in search.json()["results"][0]["snippet"]
        filtered = client.post(
            "/search",
            json={
                "query": "Horse transcript",
                "classification_code": "636.1",
                "document_status": "ready",
                "filename_contains": "notes",
            },
        )
        assert filtered.status_code == 200
        assert filtered.json()["total"] == 1
        assert filtered.json()["limit"] == 20
        assert filtered.json()["offset"] == 0
        assert filtered.json()["document_ids"] == [document_id]
        paged_out = client.post(
            "/search",
            json={"query": "Horse transcript", "limit": 1, "offset": 1},
        )
        assert paged_out.status_code == 200
        assert paged_out.json()["total"] == 1
        assert paged_out.json()["document_ids"] == []
        future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        date_filtered = client.post(
            "/search/results",
            json={"query": "Horse transcript", "created_before": future},
        )
        assert date_filtered.status_code == 200
        assert date_filtered.json()["total"] == 1
        assert date_filtered.json()["results"][0]["document_id"] == document_id
        wrong_filter = client.post(
            "/search",
            json={"query": "Horse transcript", "classification_code": "000.0"},
        )
        assert wrong_filter.status_code == 200
        assert wrong_filter.json()["total"] == 0
        assert wrong_filter.json()["document_ids"] == []
        raw_search = client.post(
            "/search/results",
            json={"query": "rough um", "scope": "raw"},
        )
        assert raw_search.status_code == 200
        assert raw_search.json()["total"] == 1
        assert raw_search.json()["results"][0]["document_id"] == document_id
        assert raw_search.json()["results"][0]["source"] == "raw"
        assert raw_search.json()["results"][0]["run_id"] is None
        assert "<mark>rough</mark>" in raw_search.json()["results"][0]["snippet"]
        facets = client.post("/search/facets", json={"query": "Horse transcript"})
        assert facets.status_code == 200
        assert facets.json()["classifications"][0] == {
            "value": "636.1",
            "count": 1,
            "label": "Horses & Equines",
        }
        assert facets.json()["statuses"][0]["value"] == "ready"
        assert facets.json()["sources"][0] == {"value": "cleaned", "count": 1, "label": None}
        raw_facets = client.post("/search/facets", json={"query": "rough um", "scope": "raw"})
        assert raw_facets.status_code == 200
        assert raw_facets.json()["sources"][0] == {"value": "raw", "count": 1, "label": None}
        filtered_facets = client.post(
            "/search/facets",
            json={
                "query": "Horse transcript",
                "classification_code": "000.0",
                "document_status": "ready",
                "filename_contains": "notes",
            },
        )
        assert filtered_facets.status_code == 200
        assert filtered_facets.json()["sources"][0]["count"] == 0

        events = client.get(f"/runs/{run_id}/events")
        assert events.status_code == 200
        assert events.json()["limit"] == 500
        assert events.json()["offset"] == 0
        assert any("processing complete" in event for event in events.json()["events"])
        event_page = client.get(f"/runs/{run_id}/events", params={"limit": 1, "offset": 1})
        assert event_page.status_code == 200
        assert event_page.json()["limit"] == 1
        assert event_page.json()["offset"] == 1
        assert len(event_page.json()["events"]) == 1

        event_records = client.get(f"/runs/{run_id}/events/records")
        assert event_records.status_code == 200
        assert event_records.json()["limit"] == 500
        assert event_records.json()["offset"] == 0
        complete_records = [
            event
            for event in event_records.json()["events"]
            if event["stage"] == "complete" and event["message"] == "processing complete"
        ]
        assert complete_records
        assert complete_records[0]["sequence"] > 0
        assert "T" in complete_records[0]["created_at"]
        record_page = client.get(
            f"/runs/{run_id}/events/records",
            params={"limit": 1, "offset": 1},
        )
        assert record_page.status_code == 200
        assert record_page.json()["limit"] == 1
        assert record_page.json()["offset"] == 1
        assert len(record_page.json()["events"]) == 1

        stream = client.get(f"/runs/{run_id}/events/stream")
        assert stream.status_code == 200
        assert stream.headers["x-content-type-options"] == "nosniff"
        assert stream.headers["cache-control"] == "no-store"
        assert "event: done" in stream.text
        record_stream = client.get(f"/runs/{run_id}/events/records/stream")
        assert record_stream.status_code == 200
        assert record_stream.headers["x-content-type-options"] == "nosniff"
        assert record_stream.headers["cache-control"] == "no-store"
        assert "event: run-event" in record_stream.text
        assert '"stage":"complete"' in record_stream.text
        assert '"message":"processing complete"' in record_stream.text
        assert "event: done" in record_stream.text

        exported = client.get(f"/documents/{document_id}/export")
        assert exported.status_code == 200
        assert exported.json()["classification"] == "636.1 - Horses & Equines"

        exported_md = client.get(f"/documents/{document_id}/export?format=md")
        assert exported_md.status_code == 200
        assert exported_md.headers["x-content-type-options"] == "nosniff"
        assert exported_md.headers["cache-control"] == "no-store"
        assert exported_md.headers["content-disposition"] == (
            'attachment; filename="notes.md"; filename*=UTF-8\'\'notes.md'
        )
        assert "# notes" in exported_md.text


def test_api_export_uses_safe_content_disposition_filename(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    with TestClient(create_app(settings)) as client:
        upload = client.post(
            "/documents",
            files={
                "file": (
                    '../"bad;name"\r\n.txt',
                    b"Horse transcript about saddle fit.",
                    "text/plain",
                )
            },
        )
        assert upload.status_code == 200
        document_id = upload.json()["id"]

        run = client.post("/runs", json={"document_id": document_id})
        assert run.status_code == 200
        _wait_for_run(client, run.json()["id"])

        exported = client.get(f"/documents/{document_id}/export", params={"format": "txt"})

    assert exported.status_code == 200
    assert exported.headers["content-disposition"] == (
        'attachment; filename="_bad_name_.txt"; '
        "filename*=UTF-8''%22bad%3Bname%22.txt"
    )
    assert "\r" not in exported.headers["content-disposition"]
    assert "\n" not in exported.headers["content-disposition"]


def test_api_key_auth(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_key="secret",
    )
    caplog.set_level("WARNING", logger="librarian.api")
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        rejected = client.get("/documents", headers={"x-api-key": "wrong-secret"})
        assert rejected.status_code == 401
        assert rejected.json()["code"] == "invalid_api_key"
        assert rejected.headers["x-content-type-options"] == "nosniff"
        assert rejected.headers["cache-control"] == "no-store"
        assert client.get("/documents", headers={"x-api-key": "secret"}).status_code == 200
    auth_events = [record for record in caplog.records if record.message == "api_auth_failed"]
    assert len(auth_events) == 1
    assert auth_events[0].__dict__["credential_present"] is True
    assert auth_events[0].__dict__["path"] == "/documents"
    assert "wrong-secret" not in caplog.text
    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute(
            """
            SELECT event, method, path, credential_present, credential_scope,
                   retry_after_seconds
            FROM api_audit_events
            """
        ).fetchone()
    assert row == ("api_auth_failed", "GET", "/documents", 1, None, None)
    with sqlite3.connect(settings.database_path) as connection:
        persisted = "\n".join(
            str(value)
            for row in connection.execute("SELECT * FROM api_audit_events").fetchall()
            for value in row
        )
    assert "wrong-secret" not in persisted


def test_api_liveness_endpoints_remain_public_with_auth(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_key="secret",
    )
    with TestClient(create_app(settings)) as client:
        health = client.get("/health")
        ready = client.get("/ready")
        version = client.get("/version")
        rejected = client.get("/documents")

    assert health.status_code == 200
    assert ready.status_code == 200
    assert version.status_code == 200
    assert rejected.status_code == 401


def test_api_key_auth_accepts_bearer_token(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_key="secret",
    )
    with TestClient(create_app(settings)) as client:
        accepted = client.get("/documents", headers={"authorization": "Bearer secret"})
        rejected = client.get("/documents", headers={"authorization": "Basic secret"})

    assert accepted.status_code == 200
    assert rejected.status_code == 401
    assert rejected.json()["code"] == "invalid_api_key"


def test_api_accepts_rotated_api_keys(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_key="old-secret",
        api_keys="new-secret, break-glass ",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/documents", headers={"x-api-key": "old-secret"}).status_code == 200
        assert client.get("/documents", headers={"x-api-key": "new-secret"}).status_code == 200
        assert client.get("/documents", headers={"x-api-key": "break-glass"}).status_code == 200
        rejected = client.get("/documents", headers={"x-api-key": "wrong"})

    assert rejected.status_code == 401
    assert rejected.json()["code"] == "invalid_api_key"


def test_api_accepts_scoped_read_only_keys(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_keys="read:reader,write:writer",
    )
    caplog.set_level("WARNING", logger="librarian.api")
    with TestClient(create_app(settings)) as client:
        read_list = client.get("/documents", headers={"x-api-key": "reader"})
        read_search = client.post(
            "/search",
            headers={"x-api-key": "reader"},
            json={"query": "horse"},
        )
        rejected_write = client.post(
            "/documents",
            headers={"x-api-key": "reader"},
            files={"file": ("notes.txt", b"Horse scoped key.", "text/plain")},
        )
        accepted_write = client.post(
            "/documents",
            headers={"x-api-key": "writer"},
            files={"file": ("notes.txt", b"Horse scoped key.", "text/plain")},
        )

    assert read_list.status_code == 200
    assert read_search.status_code == 200
    assert rejected_write.status_code == 403
    assert rejected_write.json() == {
        "detail": "API key scope does not allow this operation",
        "code": "insufficient_scope",
    }
    assert accepted_write.status_code == 200
    scope_events = [record for record in caplog.records if record.message == "api_scope_denied"]
    assert len(scope_events) == 1
    assert scope_events[0].__dict__["credential_scope"] == "read"
    assert scope_events[0].__dict__["path"] == "/documents"
    assert "reader" not in caplog.text
    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute(
            """
            SELECT event, method, path, credential_present, credential_scope,
                   retry_after_seconds
            FROM api_audit_events
            """
        ).fetchone()
    assert row == ("api_scope_denied", "POST", "/documents", 0, "read", None)


def test_api_read_scoped_keys_cannot_read_operational_endpoints(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_keys="read:reader,write:writer",
    )
    with TestClient(create_app(settings)) as client:
        rejected_config = client.get("/config", headers={"x-api-key": "reader"})
        rejected_metrics = client.get("/metrics", headers={"x-api-key": "reader"})
        rejected_prometheus = client.get("/metrics/prometheus", headers={"x-api-key": "reader"})
        accepted_config = client.get("/config", headers={"x-api-key": "writer"})
        accepted_metrics = client.get("/metrics", headers={"x-api-key": "writer"})
        accepted_prometheus = client.get("/metrics/prometheus", headers={"x-api-key": "writer"})

    assert rejected_config.status_code == 403
    assert rejected_config.json()["code"] == "insufficient_scope"
    assert rejected_metrics.status_code == 403
    assert rejected_metrics.json()["code"] == "insufficient_scope"
    assert rejected_prometheus.status_code == 403
    assert rejected_prometheus.json()["code"] == "insufficient_scope"
    assert accepted_config.status_code == 200
    assert accepted_metrics.status_code == 200
    assert accepted_prometheus.status_code == 200


def test_api_accepts_hashed_scoped_keys(tmp_path: Path) -> None:
    reader_hash = hashlib.sha256(b"hashed-reader").hexdigest()
    writer_hash = hashlib.sha256(b"hashed-writer").hexdigest()
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_key_hashes=f"read:{reader_hash},write:{writer_hash}",
    )
    with TestClient(create_app(settings)) as client:
        read_list = client.get("/documents", headers={"x-api-key": "hashed-reader"})
        rejected_write = client.post(
            "/documents",
            headers={"x-api-key": "hashed-reader"},
            files={"file": ("notes.txt", b"Horse hashed key.", "text/plain")},
        )
        accepted_write = client.post(
            "/documents",
            headers={"x-api-key": "hashed-writer"},
            files={"file": ("notes.txt", b"Horse hashed key.", "text/plain")},
        )
        rejected_plain_hash = client.get("/documents", headers={"x-api-key": reader_hash})

    assert read_list.status_code == 200
    assert rejected_write.status_code == 403
    assert accepted_write.status_code == 200
    assert rejected_plain_hash.status_code == 401


def test_api_rate_limit_returns_429_with_retry_after(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_rate_limit_per_minute=1,
    )
    caplog.set_level("WARNING", logger="librarian.api")
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        first = client.get("/documents")
        limited = client.get("/documents")

    assert first.status_code == 200
    assert limited.status_code == 429
    assert limited.json() == {"detail": "Rate limit exceeded", "code": "rate_limited"}
    assert int(limited.headers["retry-after"]) > 0
    rate_events = [record for record in caplog.records if record.message == "api_rate_limited"]
    assert len(rate_events) == 1
    assert rate_events[0].__dict__["path"] == "/documents"
    assert int(rate_events[0].__dict__["retry_after_seconds"]) > 0
    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute(
            """
            SELECT event, method, path, credential_present, credential_scope,
                   retry_after_seconds
            FROM api_audit_events
            """
        ).fetchone()
    assert row[:5] == ("api_rate_limited", "GET", "/documents", 0, None)
    assert int(row[5]) > 0


def test_api_rate_limit_ignores_untrusted_x_forwarded_for(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_rate_limit_per_minute=1,
    )
    with TestClient(create_app(settings)) as client:
        first = client.get("/documents", headers={"x-forwarded-for": "198.51.100.1"})
        limited = client.get("/documents", headers={"x-forwarded-for": "198.51.100.2"})

    assert first.status_code == 200
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"


def test_api_rate_limit_uses_x_forwarded_for_from_trusted_proxy(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_rate_limit_per_minute=1,
        api_trusted_proxy_cidrs="10.0.0.0/24",
    )
    with TestClient(create_app(settings), client=("10.0.0.5", 50000)) as client:
        first = client.get("/documents", headers={"x-forwarded-for": "198.51.100.1"})
        second = client.get("/documents", headers={"x-forwarded-for": "198.51.100.2"})
        limited = client.get("/documents", headers={"x-forwarded-for": "198.51.100.1"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"


def test_api_audit_ignores_untrusted_x_forwarded_for(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_key="secret",
    )
    with TestClient(create_app(settings), client=("203.0.113.10", 50000)) as client:
        rejected = client.get(
            "/documents",
            headers={"x-api-key": "wrong-secret", "x-forwarded-for": "198.51.100.99"},
        )

    assert rejected.status_code == 401
    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute("SELECT client_host FROM api_audit_events").fetchone()
    assert row == ("203.0.113.10",)


def test_api_audit_events_prune_expired_rows(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_key="secret",
        api_audit_retention_days=1,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            """
            INSERT INTO api_audit_events (
              event, method, path, client_host, credential_present,
              credential_scope, retry_after_seconds, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "api_auth_failed",
                "GET",
                "/documents",
                "127.0.0.1",
                1,
                None,
                None,
                "2000-01-01T00:00:00+00:00",
            ),
        )

    with TestClient(create_app(settings)) as client:
        rejected = client.get("/documents", headers={"x-api-key": "wrong-secret"})

    assert rejected.status_code == 401
    with sqlite3.connect(settings.database_path) as connection:
        rows = connection.execute(
            "SELECT event, created_at FROM api_audit_events ORDER BY id"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "api_auth_failed"
    assert rows[0][1] != "2000-01-01T00:00:00+00:00"


def test_api_liveness_endpoints_are_rate_limit_exempt(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_rate_limit_per_minute=1,
    )
    with TestClient(create_app(settings)) as client:
        for _ in range(3):
            assert client.get("/health").status_code == 200
            assert client.get("/ready").status_code == 200
            assert client.get("/version").status_code == 200
        first_limited_endpoint = client.get("/documents")
        second_limited_endpoint = client.get("/documents")

    assert first_limited_endpoint.status_code == 200
    assert second_limited_endpoint.status_code == 429


def test_api_rate_limit_is_keyed_by_api_key(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_keys="one,two",
        api_rate_limit_per_minute=1,
    )
    with TestClient(create_app(settings)) as client:
        first = client.get("/documents", headers={"x-api-key": "one"})
        second = client.get("/documents", headers={"x-api-key": "two"})
        limited = client.get("/documents", headers={"x-api-key": "one"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"


def test_api_rate_limit_is_keyed_by_bearer_token(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_keys="one,two",
        api_rate_limit_per_minute=1,
    )
    with TestClient(create_app(settings)) as client:
        first = client.get("/documents", headers={"authorization": "Bearer one"})
        second = client.get("/documents", headers={"authorization": "Bearer two"})
        limited = client.get("/documents", headers={"authorization": "Bearer one"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"


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
    create_app(
        Settings(
            api_host="0.0.0.0",  # noqa: S104
            api_key_hashes=f"write:{hashlib.sha256(b'public').hexdigest()}",
            api_import_root=tmp_path,
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
    assert response.json()["code"] == "invalid_import_path"


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


def test_api_import_skips_symlinked_files_outside_import_root(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    source_dir = import_root / "input"
    source_dir.mkdir(parents=True)
    outside = tmp_path / "private.txt"
    outside.write_text("private outside root", encoding="utf-8")
    link = source_dir / "leak.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not supported on this filesystem")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=import_root,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/imports",
            json={"source_dir": str(source_dir), "processing_mode": "none"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["converted"] == 0
    assert payload["ingested"] == 0
    assert not (source_dir / "librarian-converted" / "leak.md").exists()


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


def test_api_rejects_oversized_request_before_routing(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_max_request_bytes=4,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/search", json={"query": "horse"})

    assert response.status_code == 413
    assert response.json()["code"] == "request_too_large"
    assert response.headers["x-content-type-options"] == "nosniff"


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


def test_api_batch_upload_returns_per_file_results(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/documents/batch",
            files=[
                ("files", ("a.txt", b"Horse batch transcript.", "text/plain")),
                ("files", ("archive.zip", b"PK", "application/zip")),
                ("files", ("b.md", b"# Batch\n\nLibrary science notes.", "text/markdown")),
            ],
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ingested"] == 2
    assert payload["failed"] == 1
    assert payload["documents"][0]["status"] == "ingested"
    assert payload["documents"][0]["document"]["filename"] == "a.txt"
    assert payload["documents"][1]["status"] == "failed"
    assert payload["documents"][1]["error"]["code"] == "archive_not_supported"
    assert payload["documents"][2]["document"]["filename"] == "b.md"


def test_api_batch_upload_sanitizes_failed_item_filenames(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/documents/batch",
            files=[
                ("files", ("../bad\x00name\n.zip", b"PK", "application/zip")),
            ],
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ingested"] == 0
    assert payload["failed"] == 1
    assert payload["documents"][0]["filename"] == "badname.zip"
    assert payload["documents"][0]["error"]["code"] == "archive_not_supported"


def test_api_batch_upload_rejects_too_many_files_before_ingest(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_max_batch_files=1,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/documents/batch",
            files=[
                ("files", ("a.txt", b"first", "text/plain")),
                ("files", ("b.txt", b"second", "text/plain")),
            ],
        )

    assert response.status_code == 413
    assert response.json()["code"] == "batch_too_large"
    assert not list((tmp_path / ".librarian" / "uploads").glob("*"))


def test_api_batch_upload_rejects_too_many_bytes_before_ingest(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_max_batch_bytes=4,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/documents/batch",
            files=[
                ("files", ("a.txt", b"first", "text/plain")),
                ("files", ("b.txt", b"second", "text/plain")),
            ],
        )

    assert response.status_code == 413
    assert response.json()["code"] == "batch_too_large"
    assert not list((tmp_path / ".librarian" / "uploads").glob("*"))


def test_api_document_list_uses_paginated_response_metadata(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        for index in range(3):
            response = client.post(
                "/documents",
                files={"file": (f"note-{index}.txt", f"text {index}".encode(), "text/plain")},
            )
            assert response.status_code == 200

        page = client.get("/documents?limit=1&offset=1")

    assert page.status_code == 200
    payload = page.json()
    assert payload["total"] == 3
    assert payload["limit"] == 1
    assert payload["offset"] == 1
    assert len(payload["documents"]) == 1


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


def test_api_upload_sanitizes_control_chars_and_path_separators(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/documents",
            files={"file": ("../bad\x00name\n.txt", b"safe text", "text/plain")},
        )

    assert response.status_code == 200
    assert response.json()["filename"] == "badname.txt"
    uploads = sorted((tmp_path / ".librarian" / "uploads").glob("*/badname.txt"))
    assert len(uploads) == 1
    assert uploads[0].read_text(encoding="utf-8") == "safe text"


def test_api_upload_truncates_overlong_filename_and_preserves_extension(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    long_name = f"{'a' * 400}.txt"
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/documents",
            files={"file": (long_name, b"safe text", "text/plain")},
        )

    assert response.status_code == 200
    filename = response.json()["filename"]
    assert filename.endswith(".txt")
    assert len(filename.encode("utf-8")) <= 255
    uploads = sorted((tmp_path / ".librarian" / "uploads").glob(f"*/{filename}"))
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
    assert response.json()["code"] == "unsupported_file_type"
    assert not list((tmp_path / ".librarian" / "uploads").glob("*"))


def test_api_upload_rejects_archives_with_explicit_policy(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/documents",
            files={"file": ("archive.zip", b"PK", "application/zip")},
        )

    assert response.status_code == 400
    assert "Archive inputs are not supported" in response.json()["detail"]
    assert response.json()["code"] == "archive_not_supported"
    assert not (tmp_path / ".librarian" / "uploads").exists()
    assert not list((tmp_path / ".librarian" / "uploads").glob("*"))


def test_api_upload_rejects_renamed_archive_signature_before_persisting(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/documents",
            files={"file": ("notes.txt", b"PK\x03\x04renamed zip", "text/plain")},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "archive_not_supported"
    assert not list((tmp_path / ".librarian" / "uploads").glob("*/*"))


def test_api_upload_allows_supported_zip_container_documents(tmp_path: Path) -> None:
    from docx import Document

    source = tmp_path / "fixture.docx"
    document = Document()
    document.add_paragraph("DOCX upload fixture text")
    document.save(str(source))
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/documents",
            files={
                "file": (
                    "fixture.docx",
                    source.read_bytes(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )

    assert response.status_code == 200
    assert response.json()["filename"] == "fixture.docx"


def test_api_upload_ingest_does_not_require_llm_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LIBRARIAN_TEST_MISSING_API_KEY", raising=False)
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        llm_provider="openai-compatible",
        llm_api_key_env="LIBRARIAN_TEST_MISSING_API_KEY",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/documents",
            files={"file": ("notes.txt", b"Ingest only text.", "text/plain")},
        )

    assert response.status_code == 200
    assert response.json()["filename"] == "notes.txt"


def test_api_import_none_does_not_require_llm_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LIBRARIAN_TEST_MISSING_API_KEY", raising=False)
    import_root = tmp_path / "imports"
    source_dir = import_root / "input"
    source_dir.mkdir(parents=True)
    (source_dir / "notes.txt").write_text("Import without processing.", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=import_root,
        llm_provider="openai-compatible",
        llm_api_key_env="LIBRARIAN_TEST_MISSING_API_KEY",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/imports",
            json={"source_dir": str(source_dir), "processing_mode": "none"},
        )

    assert response.status_code == 200
    assert response.json()["ingested"] == 1
    assert response.json()["processed"] == 0


def test_api_content_uses_configured_default_page_cap(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_max_content_chars=4,
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    with TestClient(create_app(settings)) as client:
        upload = client.post(
            "/documents",
            files={"file": ("notes.txt", b"Horse transcript content page.", "text/plain")},
        )
        assert upload.status_code == 200
        document_id = upload.json()["id"]
        run = client.post("/runs", json={"document_id": document_id})
        assert run.status_code == 200
        _wait_for_run(client, run.json()["id"])

        content = client.get(f"/documents/{document_id}/content")

    assert content.status_code == 200
    payload = content.json()
    assert payload["limit"] == 4
    assert len(payload["text"]) == 4
    assert payload["total_chars"] > 4
    assert payload["truncated"] is True


def test_api_config_exposes_operational_controls(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_max_request_bytes=9999,
        api_max_upload_bytes=1234,
        api_max_batch_files=12,
        api_max_batch_bytes=12345,
        api_max_import_files=44,
        api_max_import_bytes=4321,
        api_max_import_manifest_bytes=3210,
        api_max_content_chars=123,
        api_rate_limit_per_minute=60,
        api_trusted_proxy_cidrs="10.0.0.0/24,192.0.2.10",
        api_audit_retention_days=14,
        llm_max_prompt_chars=87654,
        llm_max_response_chars=98765,
        ocr_language="eng",
        ocr_preprocess_mode="threshold",
        ocr_threshold=160,
        ocr_preserve_page_images=True,
        otel_service_name="librarian-test",
        universal_timeout_seconds=77,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/config")

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_max_request_bytes"] == 9999
    assert payload["api_max_upload_bytes"] == 1234
    assert payload["api_max_batch_files"] == 12
    assert payload["api_max_batch_bytes"] == 12345
    assert payload["api_max_import_files"] == 44
    assert payload["api_max_import_bytes"] == 4321
    assert payload["api_max_import_manifest_bytes"] == 3210
    assert payload["api_max_content_chars"] == 123
    assert payload["api_rate_limit_per_minute"] == 60
    assert payload["api_trusted_proxy_cidrs"] == "10.0.0.0/24,192.0.2.10/32"
    assert payload["api_audit_retention_days"] == 14
    assert payload["llm_max_prompt_chars"] == 87654
    assert payload["llm_max_response_chars"] == 98765
    assert payload["ocr_language"] == "eng"
    assert payload["ocr_preprocess_mode"] == "threshold"
    assert payload["ocr_threshold"] == 160
    assert payload["ocr_preserve_page_images"] is True
    assert payload["cleaning_prompt_version"] == "cmos_v2"
    assert payload["classification_prompt_version"] == "dewey_v2"
    assert payload["universal_timeout_seconds"] == 77
    assert payload["llm_max_retries"] == settings.llm_max_retries
    assert payload["api_auth_keys_configured"] == 0
    assert payload["otel_enabled"] is False
    assert payload["otel_service_name"] == "librarian-test"
    assert payload["otel_endpoint"] is None


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


def test_api_upload_rejects_symlinked_upload_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / ".librarian"
    data_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (data_dir / "uploads").symlink_to(outside)
    settings = Settings(
        data_dir=data_dir,
        database_path=data_dir / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/documents",
            files={"file": ("notes.txt", b"Horse unsafe upload root", "text/plain")},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_upload_path"
    assert not list(outside.rglob("*"))


def test_api_upload_rejects_symlinked_data_dir(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    data_dir = tmp_path / ".librarian"
    data_dir.symlink_to(outside, target_is_directory=True)
    settings = Settings(
        data_dir=data_dir,
        database_path=outside / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/documents",
            files={"file": ("notes.txt", b"Horse unsafe data dir", "text/plain")},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_upload_path"
    assert not list(outside.rglob("uploads"))


def test_api_upload_rejects_symlinked_data_dir_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)
    settings = Settings(
        data_dir=linked_parent / ".librarian",
        database_path=outside / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/documents",
            files={"file": ("notes.txt", b"Horse unsafe data dir parent", "text/plain")},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_upload_path"
    assert not list(outside.rglob("uploads"))


def test_api_delete_removes_cleaned_chunk_cache_text(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    with TestClient(create_app(settings)) as client:
        upload = client.post(
            "/documents",
            files={"file": ("notes.txt", b"Horse private cache deletion text.", "text/plain")},
        )
        assert upload.status_code == 200
        document_id = upload.json()["id"]
        run = client.post("/runs", json={"document_id": document_id})
        assert run.status_code == 200
        assert _wait_for_run(client, run.json()["id"]).json()["status"] == "succeeded"

        deleted = client.delete(f"/documents/{document_id}")

    assert deleted.status_code == 200
    with sqlite3.connect(settings.database_path) as connection:
        remaining = connection.execute("SELECT text FROM cleaned_chunk_cache").fetchall()
    assert remaining == []


def test_api_delete_removes_all_document_scoped_records(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    with TestClient(create_app(settings)) as client:
        upload = client.post(
            "/documents",
            files={"file": ("notes.txt", b"Horse private deletion transcript.", "text/plain")},
        )
        assert upload.status_code == 200
        document_id = upload.json()["id"]
        run = client.post("/runs", json={"document_id": document_id})
        assert run.status_code == 200
        run_id = run.json()["id"]
        assert _wait_for_run(client, run_id).json()["status"] == "succeeded"

        deleted = client.delete(f"/documents/{document_id}")

    assert deleted.status_code == 200
    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        counts = {
            table: connection.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE {column} = ?",  # noqa: S608
                (document_id if column == "document_id" else run_id,),
            ).fetchone()["count"]
            for table, column in {
                "documents": "id",
                "chunks": "document_id",
                "runs": "document_id",
                "cleaned_chunks": "run_id",
                "cleaned_outputs": "document_id",
                "classifications": "document_id",
                "cleaned_outputs_fts": "document_id",
                "raw_content_fts": "document_id",
                "run_queue": "run_id",
                "run_events": "run_id",
            }.items()
        }
        counts["content_blobs"] = connection.execute(
            "SELECT COUNT(*) AS count FROM content_blobs WHERE key = ?",
            (raw_text_key(DocumentId(document_id)),),
        ).fetchone()["count"]
    assert counts == {
        "documents": 0,
        "chunks": 0,
        "runs": 0,
        "cleaned_chunks": 0,
        "cleaned_outputs": 0,
        "classifications": 0,
        "cleaned_outputs_fts": 0,
        "raw_content_fts": 0,
        "run_queue": 0,
        "run_events": 0,
        "content_blobs": 0,
    }


def test_api_duplicate_upload_preserves_ready_status_and_removes_duplicate_file(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        chunk_target_chars=200,
        chunk_overlap_chars=20,
    )
    with TestClient(create_app(settings)) as client:
        first = client.post(
            "/documents",
            files={"file": ("same.txt", b"Horse duplicate stable text.", "text/plain")},
        )
        assert first.status_code == 200
        document_id = first.json()["id"]
        run = client.post("/runs", json={"document_id": document_id})
        assert _wait_for_run(client, run.json()["id"]).json()["status"] == "succeeded"

        second = client.post(
            "/documents",
            files={"file": ("same.txt", b"Horse duplicate stable text.", "text/plain")},
        )
        after_duplicate = client.get(f"/documents/{document_id}")
        deleted = client.delete(f"/documents/{document_id}")

    assert second.status_code == 200
    assert second.json()["status"] == "ready"
    assert after_duplicate.json()["status"] == "ready"
    assert deleted.status_code == 200
    assert not list((tmp_path / ".librarian" / "uploads").glob("*/same.txt"))


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
        job_backend="sqlite",
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
        metrics = client.get("/metrics")
        prometheus = client.get("/metrics/prometheus")

    assert imported.status_code == 200
    assert imported.json()["processed"] == 1
    assert imported.json()["queued"] == 0
    assert metrics.status_code == 200
    assert metrics.json()["runs_completed_total"] == 1
    assert metrics.json()["run_stage_counts"]["clean"] == 1
    assert prometheus.status_code == 200
    assert 'librarian_runs_total{status="succeeded"} 1' in prometheus.text
    assert 'librarian_run_stage_total{stage="clean"} 1' in prometheus.text


def test_api_import_rejects_too_many_files_before_conversion(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    source_dir = import_root / "input"
    source_dir.mkdir(parents=True)
    (source_dir / "one.txt").write_text("first import", encoding="utf-8")
    (source_dir / "two.txt").write_text("second import", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=import_root,
        api_max_import_files=1,
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/imports",
            json={"source_dir": str(source_dir), "processing_mode": "none"},
        )

    assert response.status_code == 413
    assert response.json()["code"] == "import_too_large"
    assert not (source_dir / "librarian-converted").exists()


def test_api_import_rejects_too_many_bytes_before_conversion(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    source_dir = import_root / "input"
    source_dir.mkdir(parents=True)
    (source_dir / "large.txt").write_text("large import body", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=import_root,
        api_max_import_bytes=4,
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/imports",
            json={"source_dir": str(source_dir), "processing_mode": "none"},
        )

    assert response.status_code == 413
    assert response.json()["code"] == "import_too_large"
    assert not (source_dir / "librarian-converted").exists()


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


def test_api_import_rejects_new_directory_at_or_above_source(tmp_path: Path) -> None:
    source_dir = tmp_path / "input"
    source_dir.mkdir()
    (source_dir / "notes.txt").write_text("Horse import transcript", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=tmp_path,
    )
    with TestClient(create_app(settings)) as client:
        same_dir = client.post(
            "/imports",
            json={
                "source_dir": str(source_dir),
                "output_mode": "new-directory",
                "output_dir": str(source_dir),
                "processing_mode": "none",
            },
        )
        ancestor = client.post(
            "/imports",
            json={
                "source_dir": str(source_dir),
                "output_mode": "new-directory",
                "output_dir": str(tmp_path),
                "processing_mode": "none",
            },
        )

    assert same_dir.status_code == 400
    assert ancestor.status_code == 400
    assert "ancestor" in ancestor.json()["detail"]


def test_api_import_rejects_symlinked_new_directory_output(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    source_dir = import_root / "input"
    source_dir.mkdir(parents=True)
    (source_dir / "notes.txt").write_text("Horse import transcript", encoding="utf-8")
    real_output = import_root / "real-output"
    real_output.mkdir()
    linked_output = import_root / "linked-output"
    linked_output.symlink_to(real_output, target_is_directory=True)
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
                "output_mode": "new-directory",
                "output_dir": str(linked_output),
                "processing_mode": "none",
            },
        )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_output_dir"
    assert "output_dir must not be a symlink" in response.json()["detail"]
    assert list(real_output.iterdir()) == []


def test_api_import_rejects_missing_source(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
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

    assert missing.status_code == 400
    assert "must exist" in missing.json()["detail"]


def test_api_import_accepts_single_file(tmp_path: Path) -> None:
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
        response = client.post(
            "/imports",
            json={"source_dir": str(file_source), "processing_mode": "none"},
        )

    assert response.status_code == 200
    assert response.json()["ingested"] == 1
    assert response.json()["items"][0]["source_path"] == str(file_source)
    assert (import_root / "librarian-converted" / "notes.md").exists()


def test_api_import_status_reads_manifest_and_resume_skips_completed_items(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    source_dir = import_root / "input"
    source_dir.mkdir(parents=True)
    source = source_dir / "notes.txt"
    source.write_text("Horse import transcript", encoding="utf-8")
    manifest = import_root / "manifest.json"
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=import_root,
    )
    with TestClient(create_app(settings)) as client:
        first = client.post(
            "/imports",
            json={
                "source_dir": str(source_dir),
                "processing_mode": "none",
                "manifest_path": str(manifest),
            },
        )
        status = client.get(
            "/imports/status",
            params={"manifest_path": str(manifest), "limit": 1, "offset": 0},
        )
        second = client.post(
            "/imports",
            json={
                "source_dir": str(source_dir),
                "processing_mode": "none",
                "manifest_path": str(manifest),
                "resume": True,
            },
        )

    assert first.status_code == 200
    assert first.json()["ingested"] == 1
    assert status.status_code == 200
    assert status.json()["total"] == 1
    assert status.json()["limit"] == 1
    assert status.json()["offset"] == 0
    assert len(status.json()["items"]) == 1
    assert status.json()["ingested"] == 1
    assert second.status_code == 200
    assert second.json()["skipped"] == 1


def test_api_import_page_manifest_reports_pdf_page_progress(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    manifest = import_root / "fixture.md.pages.json"
    image_path = import_root / "fixture.md.pages.page-0002.png"
    manifest.write_text(
        json.dumps(
            {
                "generated_by": "librarian",
                "artifact_type": "pdf-page-extraction-manifest",
                "source_sha256": "abc123",
                "page_count": 3,
                "pages": [
                    {
                        "page_number": 1,
                        "source": "embedded",
                        "status": "succeeded",
                        "chars": 120,
                        "confidence": None,
                        "corrected": False,
                        "attempts": 0,
                        "duration_ms": None,
                        "warnings": [],
                        "error": None,
                    },
                    {
                        "page_number": 2,
                        "source": "ocr",
                        "status": "succeeded",
                        "chars": 80,
                        "confidence": 74.0,
                        "corrected": True,
                        "attempts": 1,
                        "duration_ms": 45.5,
                        "image_path": str(image_path),
                        "warnings": ["low-ocr-confidence"],
                        "error": None,
                    },
                    {
                        "page_number": 3,
                        "source": "ocr",
                        "status": "failed",
                        "chars": 0,
                        "confidence": None,
                        "corrected": False,
                        "attempts": 2,
                        "duration_ms": 12.0,
                        "warnings": ["ocr-page-failed", "missing-ocr-confidence"],
                        "error": "tesseract failed",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=import_root,
    )

    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/imports/page-manifest",
            params={
                "manifest_path": str(manifest),
                "limit": 1,
                "offset": 0,
                "failures_only": True,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["manifest_path"] == str(manifest)
    assert payload["source_sha256"] == "abc123"
    assert payload["page_count"] == 3
    assert payload["statuses"] == {"succeeded": 2, "failed": 1}
    assert payload["sources"] == {"embedded": 1, "ocr": 2}
    assert payload["warnings"] == {
        "low-ocr-confidence": 1,
        "ocr-page-failed": 1,
        "missing-ocr-confidence": 1,
    }
    assert payload["corrected_pages"] == 1
    assert payload["attempts"] == 3
    assert payload["average_confidence"] == 74.0
    assert payload["failures_only"] is True
    assert payload["total"] == 1
    assert payload["pages"] == [
        {
            "page_number": 3,
            "source": "ocr",
            "status": "failed",
            "chars": 0,
            "confidence": None,
            "corrected": False,
            "attempts": 2,
            "duration_ms": 12.0,
            "image_path": None,
            "warnings": ["ocr-page-failed", "missing-ocr-confidence"],
            "error": "tesseract failed",
        }
    ]


def test_api_import_page_manifest_rejects_outside_and_unexpected_json(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    outside = tmp_path / "fixture.md.pages.json"
    outside.write_text("{}", encoding="utf-8")
    unexpected = import_root / "notes.json"
    unexpected.write_text('{"artifact_type":"import-report","pages":[]}', encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=import_root,
    )

    with TestClient(create_app(settings)) as client:
        outside_response = client.get(
            "/imports/page-manifest",
            params={"manifest_path": str(outside)},
        )
        unexpected_response = client.get(
            "/imports/page-manifest",
            params={"manifest_path": str(unexpected)},
        )

    assert outside_response.status_code == 400
    assert outside_response.json()["code"] == "invalid_import_path"
    assert unexpected_response.status_code == 400
    assert unexpected_response.json()["code"] == "invalid_manifest_path"


def test_api_import_page_manifest_rejects_symlinked_manifest_path(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    real_manifest = import_root / "real-pages.json"
    real_manifest.write_text(
        json.dumps(
            {
                "artifact_type": "pdf-page-extraction-manifest",
                "source_sha256": "abc123",
                "page_count": 0,
                "pages": [],
            }
        ),
        encoding="utf-8",
    )
    linked_manifest = import_root / "linked-pages.json"
    linked_manifest.symlink_to(real_manifest)
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=import_root,
    )

    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/imports/page-manifest",
            params={"manifest_path": str(linked_manifest)},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_manifest_path"
    assert "manifest_path must not be a symlink" in response.json()["detail"]


def test_api_import_rejects_manifest_over_unrelated_json(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    source_dir = import_root / "input"
    source_dir.mkdir(parents=True)
    (source_dir / "notes.txt").write_text("Horse import transcript", encoding="utf-8")
    manifest = import_root / "unrelated.json"
    manifest.write_text('{"keep": true}', encoding="utf-8")
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
                "processing_mode": "none",
                "manifest_path": str(manifest),
            },
        )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_manifest_path"
    assert manifest.read_text(encoding="utf-8") == '{"keep": true}'


def test_api_import_rejects_symlinked_manifest_path(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    source_dir = import_root / "input"
    source_dir.mkdir(parents=True)
    (source_dir / "notes.txt").write_text("Horse import transcript", encoding="utf-8")
    real_manifest = import_root / "real-manifest.json"
    real_manifest.write_text("{}", encoding="utf-8")
    linked_manifest = import_root / "manifest.json"
    linked_manifest.symlink_to(real_manifest)
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
                "processing_mode": "none",
                "manifest_path": str(linked_manifest),
            },
        )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_manifest_path"
    assert "manifest_path must not be a symlink" in response.json()["detail"]
    assert real_manifest.read_text(encoding="utf-8") == "{}"


def test_api_import_rejects_oversized_existing_manifest(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    source_dir = import_root / "input"
    source_dir.mkdir(parents=True)
    (source_dir / "notes.txt").write_text("Horse import transcript", encoding="utf-8")
    manifest = import_root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "generated_by": "librarian",
                "artifact_type": "import-report",
                "summary": {},
                "items": [],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=import_root,
        api_max_import_manifest_bytes=4,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/imports",
            json={
                "source_dir": str(source_dir),
                "processing_mode": "none",
                "manifest_path": str(manifest),
            },
        )

    assert response.status_code == 413
    assert response.json()["code"] == "import_manifest_too_large"


def test_api_import_status_rejects_manifest_outside_import_root(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    outside = tmp_path / "manifest.json"
    outside.write_text("{}", encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=import_root,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/imports/status", params={"manifest_path": str(outside)})

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_import_path"


def test_api_import_status_rejects_symlinked_manifest_path(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    real_manifest = import_root / "real-manifest.json"
    real_manifest.write_text('{"summary":{},"items":[]}', encoding="utf-8")
    linked_manifest = import_root / "manifest.json"
    linked_manifest.symlink_to(real_manifest)
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=import_root,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/imports/status", params={"manifest_path": str(linked_manifest)})

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_manifest_path"
    assert "manifest_path must not be a symlink" in response.json()["detail"]


def test_api_import_status_rejects_oversized_manifest(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    manifest = import_root / "manifest.json"
    manifest.write_text('{"summary":{},"items":[]}\n', encoding="utf-8")
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_import_root=import_root,
        api_max_import_manifest_bytes=4,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/imports/status", params={"manifest_path": str(manifest)})

    assert response.status_code == 413
    assert response.json()["code"] == "import_manifest_too_large"


def test_api_malformed_search_query_returns_400(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/search", json={"query": '"'})

    assert response.status_code == 400


def test_api_oversized_search_query_returns_400(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        response = client.post("/search", json={"query": "x" * 4097})

    assert response.status_code == 400
    assert "Search query exceeds configured limit" in response.json()["detail"]


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


def test_api_search_rejects_invalid_document_status_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )

    async def fail_build_container(settings: Settings) -> None:
        raise AssertionError("invalid document_status should not build a container")

    monkeypatch.setattr(api_app, "build_ingest_container", fail_build_container)

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/search",
            json={"query": "horse", "document_status": "unknown"},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_document_status"


def test_api_search_rejects_invalid_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )

    async def fail_build_container(settings: Settings) -> None:
        raise AssertionError("invalid search scope should not build a container")

    monkeypatch.setattr(api_app, "build_ingest_container", fail_build_container)

    with TestClient(create_app(settings)) as client:
        response = client.post("/search", json={"query": "horse", "scope": "all"})

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_search_scope"


def test_api_run_events_return_404_for_missing_run(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        events = client.get("/runs/run_missing/events")
        stream = client.get("/runs/run_missing/events/stream")
        record_stream = client.get("/runs/run_missing/events/records/stream")

    assert events.status_code == 404
    assert stream.status_code == 404
    assert record_stream.status_code == 404


def test_api_sqlite_submission_failure_marks_run_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        job_backend="sqlite",
    )

    async def fail_enqueue(self: object, run_id: object) -> None:
        del self, run_id
        raise RuntimeError("queue insert failed")

    monkeypatch.setattr(SQLiteRunQueue, "enqueue", fail_enqueue)

    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        upload = client.post(
            "/documents",
            files={"file": ("notes.txt", b"Horse transcript.", "text/plain")},
        )
        assert upload.status_code == 200
        response = client.post("/runs", json={"document_id": upload.json()["id"]})

    assert response.status_code == 503
    assert "Run submission failed" in response.json()["detail"]
    container = asyncio.run(build_container(settings))
    runs = asyncio.run(container.repository.list_runs(limit=10))
    assert len(runs) == 1
    assert runs[0].status.value == "failed"
    assert runs[0].error == "submission failed: queue insert failed"


def _wait_for_run(client: TestClient, run_id: str):
    response = client.get(f"/runs/{run_id}")
    for _ in range(40):
        if response.json()["status"] in {"succeeded", "failed", "canceled"}:
            return response
        sleep(0.05)
        response = client.get(f"/runs/{run_id}")
    return response
