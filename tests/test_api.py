from fastapi.testclient import TestClient

from librarian.api.app import create_app


def test_api_health() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_api_classifications() -> None:
    client = TestClient(create_app())

    response = client.get("/classifications")

    assert response.status_code == 200
    assert response.json()["classifications"]["636.1"] == "Horses & Equines"


def test_api_metrics_and_request_id() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health", headers={"x-request-id": "req_test"})
        metrics = client.get("/metrics")

        assert response.headers["x-request-id"] == "req_test"
        assert metrics.status_code == 200
        assert metrics.json()["requests_total"] >= 1
