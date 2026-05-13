import sqlite3
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

import librarian.api.app as api_app
from librarian.api.app import create_app
from librarian.config import Settings
from librarian.storage.sqlite import SQLiteDatabase


def test_api_health() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_api_ready_verifies_database(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["database"] == "ok"
    assert payload["storage"] == "ok"
    assert payload["applied_migrations"] >= 1


def test_api_ready_returns_503_for_database_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / ".librarian" / "librarian.sqlite"
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=database_path,
    )

    async def fail_verify(self: SQLiteDatabase) -> object:
        del self
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(SQLiteDatabase, "verify", fail_verify)

    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["code"] == "service_unavailable"


def test_api_ready_returns_503_for_unwritable_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )

    def fail_storage(settings: Settings) -> None:
        del settings
        raise OSError("data_dir is not writable")

    monkeypatch.setattr(api_app, "_verify_writable_data_dir", fail_storage)

    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["code"] == "service_unavailable"
    assert "data_dir is not writable" in response.json()["detail"]


def test_api_classifications() -> None:
    client = TestClient(create_app())

    response = client.get("/classifications")

    assert response.status_code == 200
    assert response.json()["classifications"]["636.1"] == "Horses & Equines"


def test_api_metrics_and_request_id() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health", headers={"x-request-id": "req_test"})
        metrics = client.get("/metrics")
        prometheus = client.get("/metrics/prometheus")

        assert response.headers["x-request-id"] == "req_test"
        assert metrics.status_code == 200
        assert metrics.json()["requests_total"] >= 1
        assert metrics.json()["conversion_failures_total"] == 0
        assert metrics.json()["ocr_pages_total"] == 0
        assert metrics.json()["ocr_pages_per_second"] == 0
        assert metrics.json()["llm_tokens_total"] == 0
        assert metrics.json()["llm_estimated_cost_usd_total"] == 0
        assert metrics.json()["llm_tokens_by_model"] == {}
        assert metrics.json()["llm_estimated_cost_usd_by_model"] == {}
        assert prometheus.status_code == 200
        assert prometheus.headers["content-type"].startswith("text/plain")
        assert prometheus.headers["x-content-type-options"] == "nosniff"
        assert prometheus.headers["cache-control"] == "no-store"
        assert "librarian_requests_total" in prometheus.text
        assert "librarian_conversion_failures_total" in prometheus.text
        assert "librarian_ocr_pages_total" in prometheus.text
        assert "librarian_llm_tokens_total" in prometheus.text
        assert 'librarian_http_responses_total{status_code="200"}' in prometheus.text


def test_api_responses_include_security_headers() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_api_streaming_body_limit_rejects_without_content_length(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            data_dir=tmp_path / ".librarian",
            database_path=tmp_path / ".librarian" / "librarian.sqlite",
            api_max_request_bytes=4,
        )
    )
    messages: list[Message] = [
        {"type": "http.request", "body": b'{"qu', "more_body": True},
        {"type": "http.request", "body": b'ery":"horse"}', "more_body": False},
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return messages.pop(0)

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/search",
        "raw_path": b"/search",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "root_path": "",
    }
    await app(
        scope,
        receive,
        send,
    )

    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 413
    headers = dict(cast(list[tuple[bytes, bytes]], start["headers"]))
    assert headers[b"x-content-type-options"] == b"nosniff"


def test_api_openapi_has_response_models_for_json_endpoints() -> None:
    client = TestClient(create_app())

    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert (
        paths["/ready"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ReadinessResponse"
    )
    assert (
        paths["/metrics"]["get"]["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ]
        == "#/components/schemas/MetricsResponse"
    )
    assert "text/plain" in paths["/metrics/prometheus"]["get"]["responses"]["200"]["content"]
    assert (
        paths["/config"]["get"]["responses"]["200"]["content"]["application/json"]["schema"][
            "$ref"
        ]
        == "#/components/schemas/ConfigResponse"
    )
    assert (
        paths["/documents/{document_id}"]["delete"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/DeleteDocumentResponse"
    )
    assert (
        paths["/documents/{document_id}/content"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/ContentResponse"
    )
    export_content = paths["/documents/{document_id}/export"]["get"]["responses"]["200"]["content"]
    assert export_content["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/ExportDocumentResponse"
    )
    assert export_content["text/plain"]["schema"]["type"] == "string"
    assert export_content["text/markdown"]["schema"]["type"] == "string"
    assert (
        paths["/documents/batch"]["post"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        == "#/components/schemas/BatchDocumentsResponse"
    )
    assert (
        paths["/runs/{run_id}/events"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/RunEventsResponse"
    )
    assert (
        paths["/runs/{run_id}/events/records"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/RunEventRecordsResponse"
    )
    assert (
        paths["/runs/{run_id}/events/stream"]["get"]["responses"]["200"]["content"][
            "text/event-stream"
        ]["schema"]["type"]
        == "string"
    )
    assert (
        paths["/search/results"]["post"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        == "#/components/schemas/SearchResultsResponse"
    )
    assert (
        paths["/search/facets"]["post"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        == "#/components/schemas/SearchFacetsResponse"
    )
    assert (
        paths["/imports/status"]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        == "#/components/schemas/ImportStatusResponse"
    )
    error_codes = {"400", "401", "403", "404", "413", "422", "429", "500", "503"}
    for operation in [
        paths["/documents"]["post"],
        paths["/search/results"]["post"],
        paths["/runs/{run_id}/events/stream"]["get"],
    ]:
        for status_code in error_codes:
            assert operation["responses"][status_code]["content"]["application/json"]["schema"][
                "$ref"
            ] == "#/components/schemas/ErrorResponse"
    schemas = response.json()["components"]["schemas"]
    assert set(schemas["ErrorResponse"]["required"]) == {"detail", "code"}
    assert set(schemas["ContentResponse"]["required"]) == {
        "document_id",
        "text",
        "total_chars",
        "offset",
        "limit",
        "truncated",
    }
    assert set(schemas["ExportDocumentResponse"]["required"]) == {
        "document_id",
        "filename",
        "classification",
        "text",
    }
    assert set(schemas["RunEventsResponse"]["required"]) == {"events", "limit", "offset"}
    assert set(schemas["RunEventRecordsResponse"]["required"]) == {
        "events",
        "limit",
        "offset",
    }
    assert set(schemas["SearchResponse"]["required"]) == {
        "document_ids",
        "total",
        "limit",
        "offset",
    }
    assert set(schemas["SearchResultsResponse"]["required"]) == {
        "results",
        "total",
        "limit",
        "offset",
    }
    assert {"items", "total", "limit", "offset"} <= set(
        schemas["ImportStatusResponse"]["required"]
    )


def test_api_validation_errors_include_machine_code() -> None:
    client = TestClient(create_app())

    response = client.post("/search", json={"query": "horses", "limit": 0})

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"
    assert response.json()["detail"][0]["loc"] == ["body", "limit"]


def test_api_unhandled_errors_return_stable_redacted_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
    )

    async def fail_container(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("internal provider token sk-live-secret")

    monkeypatch.setattr(api_app, "build_ingest_container", fail_container)

    caplog.set_level("ERROR", logger="librarian.api")
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.get("/documents")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error", "code": "server_error"}
    assert "sk-live-secret" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert "sk-live-secret" not in caplog.text
    error_messages = [
        cast(str, getattr(record, "error", ""))
        for record in caplog.records
        if record.name == "librarian.api" and record.message == "unhandled_api_exception"
    ]
    assert error_messages == ["internal provider token [REDACTED]"]


def test_search_request_schema_documents_scope_default() -> None:
    schema = create_app().openapi()["components"]["schemas"]["SearchRequest"]

    assert schema["properties"]["scope"]["default"] == "cleaned"
    assert schema["properties"]["phrase"]["default"] is False


def test_openapi_documents_configured_api_auth(tmp_path: Path) -> None:
    schema = create_app(
        Settings(
            data_dir=tmp_path / ".librarian",
            database_path=tmp_path / ".librarian" / "librarian.sqlite",
            api_key="secret",
        )
    ).openapi()

    security_schemes = schema["components"]["securitySchemes"]
    assert security_schemes["ApiKeyAuth"] == {
        "type": "apiKey",
        "in": "header",
        "name": "x-api-key",
    }
    assert security_schemes["BearerAuth"] == {"type": "http", "scheme": "bearer"}
    assert schema["paths"]["/documents"]["get"]["security"] == [
        {"ApiKeyAuth": []},
        {"BearerAuth": []},
    ]
    assert "security" not in schema["paths"]["/health"]["get"]
    assert "security" not in schema["paths"]["/ready"]["get"]
    assert "security" not in schema["paths"]["/version"]["get"]


def test_openapi_endpoint_requires_configured_api_auth(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / ".librarian",
        database_path=tmp_path / ".librarian" / "librarian.sqlite",
        api_key="secret",
    )
    with TestClient(create_app(settings)) as client:
        rejected = client.get("/openapi.json")
        accepted = client.get("/openapi.json", headers={"x-api-key": "secret"})

    assert rejected.status_code == 401
    assert rejected.json()["code"] == "invalid_api_key"
    assert accepted.status_code == 200
    assert accepted.json()["paths"]["/documents"]["get"]["security"] == [
        {"ApiKeyAuth": []},
        {"BearerAuth": []},
    ]
