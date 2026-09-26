"""Foundry model provider.

Uses the OpenAI-compatible surface exposed by the Microsoft Foundry project
endpoint (the Responses/Chat Completions v1 API), which is what Microsoft Agent
Framework itself sits on. We talk to it directly so the harness owns the loop --
that is the whole point of this repo.

Auth order:
  1. ``FOUNDRY_API_KEY`` if set (easy local dev)
  2. Managed identity / ``az login`` via DefaultAzureCredential (production)
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

from openai import AsyncOpenAI

from ..config import settings

log = logging.getLogger(__name__)

_SCOPE = "https://ai.azure.com/.default"


def _base_url() -> str:
    ep = settings.project_endpoint.rstrip("/")
    if not ep:
        raise RuntimeError(
            "FOUNDRY_PROJECT_ENDPOINT is not set. Expected e.g. "
            "https://<resource>.services.ai.azure.com/api/projects/<project>"
        )
    return ep if ep.endswith("/v1") else f"{ep}/v1"


@lru_cache(maxsize=1)
def _token_provider():
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    return get_bearer_token_provider(DefaultAzureCredential(), _SCOPE)


@lru_cache(maxsize=1)
def get_client() -> AsyncOpenAI:
    """Cached OpenAI-compatible client pointed at the Foundry project."""
    if settings.api_key:
        return AsyncOpenAI(api_key=settings.api_key, base_url=_base_url())

    # Entra ID path. The token provider is sync; refresh per-client construction
    # is handled by azure-identity's internal caching.
    provider = _token_provider()
    return AsyncOpenAI(api_key=provider(), base_url=_base_url())


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
