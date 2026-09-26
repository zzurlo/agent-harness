"""Runtime verification is mounted behind the application's global auth gate."""
import asyncio
import importlib

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse


@pytest.fixture
def runtime():
    return importlib.import_module("api.verification")


@pytest.fixture
def app(runtime):
    app = FastAPI()
    # The actual auth implementation is owned by the main application. This gate
    # checks the router participates in global middleware, not bespoke route auth.
    @app.middleware("http")
    async def auth(request, call_next):
        if request.url.path != "/health" and request.headers.get("authorization") != "Bearer test":
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)
    @app.get("/health")
    async def health():
        return {"status": "ok"}
    app.include_router(runtime.router)
    return app


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                base_url="http://test") as client:
        yield client


async def test_router_is_available():
    import importlib.util
    assert importlib.util.find_spec("api.verification") is not None


async def test_auth_required_before_any_probe(client, runtime, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("unauthenticated model request")
    monkeypatch.setattr(runtime, "verify_routes", forbidden)
    response = await client.post("/admin/verify-models")
    assert response.status_code == 401


async def test_default_all_routes_ready(client, runtime, monkeypatch):
    seen = []
    async def verify(names, **kwargs):
        seen.extend(names)
        return [runtime.RouteResult(n, "test", True, reachable=True, tools_work=True) for n in names]
    monkeypatch.setattr(runtime, "verify_routes", verify)
    response = await client.post("/admin/verify-models", headers={"authorization": "Bearer test"})
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert seen == list(runtime.ROUTES)
    assert [r["route"] for r in response.json()["results"]] == seen


@pytest.mark.parametrize("payload", [{"routes": []}, {"routes": ["unknown"]},
    {"routes": ["chat"], "skip_tools": True}, {"routes": ["chat"] * 6}])
async def test_invalid_request_is_rejected(client, payload):
    response = await client.post("/admin/verify-models", json=payload,
                                 headers={"authorization": "Bearer test"})
    assert response.status_code == 422


async def test_failure_is_200_not_ready(client, runtime, monkeypatch):
    async def verify(names, **kwargs):
        assert names == ["tools"]
        return [runtime.RouteResult("tools", "test", True, reachable=True,
                                    tools_work=False, errors=["no tool call"])]
    monkeypatch.setattr(runtime, "verify_routes", verify)
    response = await client.post("/admin/verify-models", json={"routes": ["tools"]},
                                 headers={"authorization": "Bearer test"})
    assert response.status_code == 200
    assert response.json()["ready"] is False


async def test_busy_rejected_health_responsive_lock_released(client, runtime, monkeypatch):
    started, finish = asyncio.Event(), asyncio.Event()
    async def verify(names, **kwargs):
        assert kwargs["total_timeout"] == runtime.VERIFICATION_TIMEOUT
        started.set()
        await finish.wait()
        return [runtime.RouteResult("chat", "test", True, reachable=True, tools_work=True)]
    monkeypatch.setattr(runtime, "verify_routes", verify)
    headers = {"authorization": "Bearer test"}
    first = asyncio.create_task(client.post("/admin/verify-models", headers=headers))
    try:
        await asyncio.wait_for(started.wait(), 1)
        busy = await asyncio.wait_for(client.post("/admin/verify-models", headers=headers), 1)
        assert busy.status_code == 429
        assert busy.headers["retry-after"]
        health = await asyncio.wait_for(client.get("/health"), 1)
        assert health.status_code == 200
    finally:
        finish.set()
        await first
    assert (await client.post("/admin/verify-models", headers=headers)).status_code == 200


async def test_cancelled_probe_releases_lock(runtime, monkeypatch):
    started = asyncio.Event()
    async def verify(*args, **kwargs):
        started.set()
        await asyncio.sleep(10)
    monkeypatch.setattr(runtime, "verify_routes", verify)
    task = asyncio.create_task(runtime.verify_models())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not runtime._verification_lock.locked()
