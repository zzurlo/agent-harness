"""Verify real app wiring, not a separately constructed test router."""
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from api.main import app
from scripts.verify_models import RouteResult

KEY = "integration-test-owner-key-0123456789abcdef"


def test_runtime_probe_is_mounted_and_inherits_owner_gate(monkeypatch):
    from api import verification

    probe = AsyncMock(return_value=[RouteResult(
        route="chat", deployment="test", expects_tools=False, reachable=True,
    )])
    monkeypatch.setattr(verification, "verify_routes", probe)
    monkeypatch.setenv("APP_ACCESS_TOKEN", KEY)
    with TestClient(app) as client:
        assert client.post("/admin/verify-models").status_code == 401
        probe.assert_not_awaited()
        response = client.post("/admin/verify-models", json={"routes": ["chat"]},
                               headers={"Authorization": "Bearer " + KEY})
        assert response.status_code == 200
        assert response.json()["ready"] is True
        probe.assert_awaited_once()


def test_lifespan_closes_model_resources(monkeypatch):
    from harness.providers import foundry

    close = AsyncMock()
    monkeypatch.setattr(foundry, "close_client", close)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    close.assert_awaited_once()
