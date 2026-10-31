"""API tests for the Workshop 6 health endpoint."""
from fastapi.testclient import TestClient

from nebula.healthcheck.app import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    """GET /health must report an ok status."""
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "nebula"
    assert body["version"] == "9.9.9"  # fallo intencional
