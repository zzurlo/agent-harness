"""Example tools.

Kept deliberately small -- these exist to prove the registry, the dispatch
path, the structured-error contract, and the approval gate.
"""
from __future__ import annotations

import datetime as _dt
import json
import zoneinfo

import httpx

from .registry import registry


@registry.register()
def current_time(timezone: str = "America/Chicago") -> str:
    """Get the current date and time in an IANA timezone (e.g. 'America/Chicago')."""
    try:
        tz = zoneinfo.ZoneInfo(timezone)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Unknown timezone '{timezone}': {exc}") from exc
    return _dt.datetime.now(tz).strftime("%A, %B %d %Y at %I:%M %p %Z")


@registry.register()
async def web_fetch(url: str) -> str:
    """Fetch a URL and return its text content, truncated to 8000 characters."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    async with httpx.AsyncClient(follow_redirects=True, timeout=20) as client:
        resp = await client.get(url, headers={"User-Agent": "agent-harness/0.1"})
        resp.raise_for_status()
        text = resp.text
    return text[:8000]


@registry.register()
def calculate(expression: str) -> str:
    """Evaluate an arithmetic expression, e.g. '(1200 * 0.19) / 1e6'."""
    allowed = set("0123456789+-*/(). eE%")
    if not set(expression) <= allowed:
        raise ValueError("expression contains unsupported characters")
    try:
        return str(eval(expression, {"__builtins__": {}}, {}))  # noqa: S307
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"could not evaluate: {exc}") from exc


@registry.register(dangerous=True)
def echo_dangerous(payload: str) -> str:
    """Demo tool marked dangerous -- the loop will request UI approval first."""
    return json.dumps({"echoed": payload})
