"""Offline provider regression tests, including real SDK authentication."""
from types import SimpleNamespace

import httpx
import pytest
from azure.core.credentials import AccessToken

from harness.config import Settings
from harness.providers import foundry

ENDPOINT = "https://example.openai.azure.com/openai/v1"


def test_explicit_model_endpoint(monkeypatch):
    monkeypatch.setattr(foundry, "settings", SimpleNamespace(
        model_endpoint=ENDPOINT + "/", project_endpoint="https://bad/api/projects/p"))
    assert foundry._base_url() == ENDPOINT


@pytest.mark.parametrize("endpoint", ["", "http://example/openai/v1",
    "https://example/api/projects/p", "https://example/api/projects/p/openai/v1",
    "https://example/openai/v1?key=secret", "https://user:secret@example/openai/v1",
    "https://example/openai/v1#fragment", "https://example"])
def test_rejects_invalid_model_endpoint(monkeypatch, endpoint):
    monkeypatch.setattr(foundry, "settings", SimpleNamespace(
        model_endpoint=endpoint, project_endpoint=""))
    with pytest.raises(ValueError, match="FOUNDRY_MODEL_ENDPOINT"):
        foundry._base_url()


def test_legacy_model_endpoint_warns(monkeypatch):
    monkeypatch.setattr(foundry, "settings", SimpleNamespace(
        model_endpoint="", project_endpoint=ENDPOINT))
    with pytest.warns(DeprecationWarning, match="FOUNDRY_PROJECT_ENDPOINT"):
        assert foundry._base_url() == ENDPOINT


def test_settings_reads_model_endpoint(monkeypatch):
    monkeypatch.setenv("FOUNDRY_MODEL_ENDPOINT", ENDPOINT)
    assert Settings().model_endpoint == ENDPOINT


async def test_real_sdk_refreshes_bearer_and_closes_resources(monkeypatch):
    import azure.identity.aio
    from openai import AsyncOpenAI

    tokens, requests = [], []

    class Credential:
        closed = False

        async def get_token(self, *scopes, **kwargs):
            assert scopes == ("https://ai.azure.com/.default",)
            token = f"token-{len(tokens)}"
            tokens.append(token)
            # Expired token forces azure-identity's bearer policy to refresh.
            return AccessToken(token, 1)

        async def close(self):
            self.closed = True

    credential = Credential()

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "test", "object": "chat.completion",
            "created": 0, "model": "test", "choices": [{"index": 0,
            "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]})

    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(azure.identity.aio, "DefaultAzureCredential", lambda: credential)
    monkeypatch.setattr(foundry, "settings", SimpleNamespace(
        model_endpoint=ENDPOINT, project_endpoint="", api_key=None))
    monkeypatch.setattr(foundry, "AsyncOpenAI", lambda **kw:
        AsyncOpenAI(http_client=transport_client, **kw))
    foundry.get_client.cache_clear()
    try:
        client = foundry.get_client()
        assert foundry.get_client() is client
        for _ in range(2):
            await client.chat.completions.create(model="test", messages=[])
        assert [r.headers["authorization"] for r in requests] == [
            "Bearer token-0", "Bearer token-1"]
        assert all(str(r.url) == ENDPOINT + "/chat/completions" for r in requests)
    finally:
        await foundry.close_client()
    assert credential.closed
    assert transport_client.is_closed
    assert foundry.get_client.cache_info().currsize == 0
    await foundry.close_client()  # idempotent, no new client/credential


async def test_async_azure_transport_can_open_without_network():
    from azure.core.pipeline.transport import AioHttpTransport

    async with AioHttpTransport() as transport:
        assert transport.session is not None


async def test_api_key_does_not_create_credential(monkeypatch):
    import azure.identity.aio
    monkeypatch.setattr(foundry, "settings", SimpleNamespace(
        model_endpoint=ENDPOINT, project_endpoint="", api_key="local-test-key"))
    def forbidden():
        pytest.fail("API key must not create an Azure credential")
    monkeypatch.setattr(azure.identity.aio, "DefaultAzureCredential", forbidden)
    foundry.get_client.cache_clear()
    try:
        assert foundry.get_client().api_key == "local-test-key"
    finally:
        await foundry.close_client()
