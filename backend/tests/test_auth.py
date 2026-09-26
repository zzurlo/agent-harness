"""Global gate integration tests: no provider/network calls required."""
import pytest
from fastapi.testclient import TestClient

from api.main import app

KEY = "private-owner-test-key-0123456789abcdef"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("APP_ACCESS_TOKEN", KEY)
    return TestClient(app)


@pytest.mark.parametrize("method,path", [
    ("GET", "/routes"), ("GET", "/tools"), ("GET", "/threads"),
    ("GET", "/threads/private"), ("DELETE", "/threads/private"),
    ("POST", "/chat"), ("POST", "/approve"), ("GET", "/docs"),
    ("GET", "/openapi.json"), ("GET", "/redoc"), ("GET", "/unknown"),
    ("POST", "/health"), ("HEAD", "/warmup"), ("OPTIONS", "/chat"),
])
def test_all_paths_guarded(client, method, path):
    response = client.request(method, path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("header", ["Bearer wrong", "Basic " + KEY, KEY,
    "Bearer", "Bearer " + KEY + " extra", "Bearer short"])
def test_invalid_credentials(client, header):
    assert client.get("/routes", headers={"Authorization": header}).status_code == 401


def test_no_query_or_trusted_header_bypass(client):
    assert client.get("/routes", params={"access_token": KEY},
                      headers={"X-Access-Token": KEY}).status_code == 401


@pytest.mark.parametrize("key", [None, "", "short", "x" * 31])
def test_unconfigured_fails_closed(client, monkeypatch, key):
    if key is None:
        monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    else:
        monkeypatch.setenv("APP_ACCESS_TOKEN", key)
    assert client.get("/routes", headers={"Authorization": "Bearer " + KEY}).status_code == 503
    assert client.get("/health").status_code == 200
    assert client.get("/warmup").status_code == 200


def test_correct_key(client):
    assert client.get("/routes", headers={"Authorization": "Bearer " + KEY}).status_code == 200
    assert client.get("/openapi.json", headers={"Authorization": "bearer " + KEY}).status_code == 200


def test_unauthorized_never_calls_business_handler(client, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("business handler reached")
    monkeypatch.setattr("api.main.store.list_threads", forbidden)
    monkeypatch.setattr("api.main.store.delete", forbidden)
    assert client.get("/threads").status_code == 401
    assert client.delete("/threads/private").status_code == 401


def test_malformed_unicode_and_duplicate_headers(client):
    for raw in [b"Bearer \xff\xfe", "Bearer 😀".encode()]:
        assert client.get("/routes", headers=[(b"authorization", raw)]).status_code == 401
    assert client.get("/routes", headers=[("authorization", "Bearer " + KEY),
        ("authorization", "Bearer wrong")]).status_code == 401


def test_cors_preflight(client):
    from harness.config import settings
    origin = settings.cors_origins[0]
    good = {"Origin": origin, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type"}
    response = client.options("/chat", headers=good)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] in [origin, "*"]
    bad = {**good, "Origin": "https://untrusted.invalid"}
    assert client.options("/chat", headers=bad).status_code in [400, 401]
    assert client.options("/chat", headers={"Origin": origin}).status_code == 401
    assert client.options("/chat", headers={**good, "Access-Control-Request-Method": "INVALID"}).status_code in [400, 401]
    response = client.get("/routes", headers={"Origin": origin})
    assert response.status_code == 401
    assert "access-control-allow-origin" in response.headers


def test_exact_minimum_key_length(client, monkeypatch):
    monkeypatch.setenv("APP_ACCESS_TOKEN", "a" * 32)
    assert client.get("/routes", headers={"Authorization": "Bearer " + "a" * 32}).status_code == 200


def test_guard_does_not_buffer_sse(monkeypatch):
    import asyncio

    from api.auth import OwnerAuthMiddleware

    monkeypatch.setenv("APP_ACCESS_TOKEN", KEY)
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        pytest.fail("auth middleware must not consume the request body")

    async def streaming_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"data: first\n\n", "more_body": True})
        # Must already have reached the transport before the next chunk exists.
        assert sent[-1]["body"] == b"data: first\n\n"
        await send({"type": "http.response.body", "body": b"data: last\n\n", "more_body": False})

    scope = {"type": "http", "method": "POST", "path": "/chat",
             "headers": [(b"authorization", ("Bearer " + KEY).encode())]}
    asyncio.run(OwnerAuthMiddleware(streaming_app)(scope, receive, send))
    assert len(sent) == 3
