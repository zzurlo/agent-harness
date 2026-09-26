"""ASGI-to-SDK runtime checks: real verifier, no model/credential network."""
import asyncio
from types import SimpleNamespace

import httpx
from api import verification
from fastapi import FastAPI
from harness.providers import foundry
from openai import AsyncOpenAI


async def test_runtime_uses_cached_provider_client(monkeypatch):
    requests, clients = [], []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "test", "object": "chat.completion",
            "created": 0, "model": "test", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})
    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    def create(**kwargs):
        client = AsyncOpenAI(http_client=http, **kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(foundry, "AsyncOpenAI", create)
    monkeypatch.setattr(foundry, "settings", SimpleNamespace(
        model_endpoint="https://model.example/openai/v1", project_endpoint="", api_key="runtime-key"))
    await foundry.close_client()
    app = FastAPI()
    app.include_router(verification.router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            for _ in range(2):
                response = await api.post("/admin/verify-models", json={"routes": ["fast"]})
                assert response.status_code == 200
                assert response.json()["ready"] is True
        assert clients == [foundry.get_client()]
        assert len(requests) == 2
        assert all(r.headers["authorization"] == "Bearer runtime-key" for r in requests)
        assert all(str(r.url) == "https://model.example/openai/v1/chat/completions" for r in requests)
        assert not http.is_closed  # verification must not close chat's shared transport
        assert clients[0].max_retries == 2  # verification options must not mutate chat
    finally:
        await foundry.close_client()
    assert http.is_closed


async def test_runtime_total_budget_is_a_200_failure(monkeypatch):
    async def respond(request):
        await asyncio.sleep(10)
    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(foundry, "AsyncOpenAI", lambda **kw: AsyncOpenAI(http_client=http, **kw))
    monkeypatch.setattr(foundry, "settings", SimpleNamespace(
        model_endpoint="https://model.example/openai/v1", project_endpoint="", api_key="fake"))
    monkeypatch.setattr(verification, "VERIFICATION_TIMEOUT", 0.02)
    await foundry.close_client()
    app = FastAPI()
    app.include_router(verification.router)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            response = await asyncio.wait_for(api.post("/admin/verify-models"), 1)
        assert response.status_code == 200
        payload = response.json()
        assert payload["ready"] is False
        assert len(payload["results"]) == len(verification.ROUTES)
        assert all("time budget" in r["errors"][0] for r in payload["results"])
        assert not verification._verification_lock.locked()
    finally:
        await foundry.close_client()
