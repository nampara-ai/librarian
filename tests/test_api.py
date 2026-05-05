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
