"""Foundry model provider.

Uses the direct model /openai/v1 endpoint, NOT an Agent Service project URL.
The cached async client refreshes Entra tokens per request via azure-identity.

Auth order:
  1. ``FOUNDRY_API_KEY`` if set (easy local dev)
  2. Managed identity / ``az login`` via DefaultAzureCredential (production)
"""
from __future__ import annotations

import logging
import warnings
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from openai import AsyncOpenAI

from ..config import settings

log = logging.getLogger(__name__)

_SCOPE = "https://ai.azure.com/.default"


_credential = None
_client: AsyncOpenAI | None = None


def _base_url() -> str:
    explicit = settings.model_endpoint
    ep = (explicit or settings.project_endpoint).rstrip("/")
    message = (
        "Set FOUNDRY_MODEL_ENDPOINT to the HTTPS model endpoint, e.g. "
        "https://<resource>.openai.azure.com/openai/v1. "
        "Agent Service /api/projects/... URLs cannot serve Chat Completions; "
        "copy the model endpoint from Foundry Models + Endpoints, not the project URL."
    )
    try:
        url = urlsplit(ep)
        valid = (
            url.scheme == "https" and url.hostname and url.path == "/openai/v1"
            and not url.query and not url.fragment and not url.username
            and not url.password and not any(c.isspace() for c in ep)
        )
        _ = url.port  # reject malformed ports, too
    except ValueError:
        raise ValueError(message) from None
    if not valid:
        raise ValueError(message)
    if not explicit:
        warnings.warn(
            "FOUNDRY_PROJECT_ENDPOINT is deprecated; use FOUNDRY_MODEL_ENDPOINT "
            "with the same /openai/v1 model URL.", DeprecationWarning, stacklevel=2,
        )
        log.warning("FOUNDRY_PROJECT_ENDPOINT is deprecated; use FOUNDRY_MODEL_ENDPOINT")
    return ep


@lru_cache(maxsize=1)
def get_client() -> AsyncOpenAI:
    """Shared runtime client; token acquisition/refresh is asynchronous per request."""
    global _credential, _client
    base_url = _base_url()  # validate before creating any credential
    key = settings.api_key
    if not key:
        from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider

        if _credential is None:
            _credential = DefaultAzureCredential()
        key = get_bearer_token_provider(_credential, _SCOPE)
    _client = AsyncOpenAI(api_key=key, base_url=base_url)
    return _client


async def close_client() -> None:
    """Close cached HTTP/credential resources during lifespan shutdown; idempotent."""
    global _credential, _client
    client, credential = _client, _credential
    _client = _credential = None
    get_client.cache_clear()
    try:
        if client is not None:
            await client.close()
    finally:
        if credential is not None:
            await credential.close()


async def stream_completion(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict] | None = None,
    max_output_tokens: int = 2048,
    temperature: float = 0.7,
) -> AsyncIterator[Any]:
    """Yield raw streaming chunks from the model."""
    client = get_client()
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_output_tokens,
        "temperature": temperature,
        "stream": True,
        # Ask for usage on the final chunk so we can do cost accounting.
        "stream_options": {"include_usage": True},
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    stream = await client.chat.completions.create(**kwargs)
    async for chunk in stream:
        yield chunk


async def complete(
    *,
    model: str,
    messages: list[dict[str, Any]],
    max_output_tokens: int = 512,
    temperature: float = 0.3,
) -> str:
    """Non-streaming single-shot call. Used for compaction and titles."""
    client = get_client()
    resp = await client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_output_tokens,
        temperature=temperature,
    )
    return (resp.choices[0].message.content or "").strip()
