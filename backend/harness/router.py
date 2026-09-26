"""Route selection: pick the cheapest model that can do the job.

This is the main cost lever. Most conversational turns are trivial and should
never touch a reasoning model -- hidden thinking tokens bill at the (expensive)
output rate and add seconds of latency.
"""
from __future__ import annotations

import re

from .config import DEFAULT_ROUTE, ROUTES, ModelRoute

# Explicit user escalation, e.g. "think hard about this"
_THINK_PAT = re.compile(
    r"\b(think (hard|carefully|step.?by.?step)|reason through|work through|"
    r"prove|derive|analy[sz]e deeply|deep dive)\b",
    re.I,
)
_MAX_PAT = re.compile(r"\b(use max|maximum effort|best model|hardest)\b", re.I)

# Short social turns that deserve the cheapest possible model.
_TRIVIAL_PAT = re.compile(
    r"^(hi|hey|hello|yo|thanks|thank you|ty|ok|okay|got it|cool|nice|sure|"
    r"yes|no|yep|nope|bye)[\s!.?]*$",
    re.I,
)


def select_route(
    user_message: str,
    *,
    tools_available: bool = False,
    forced: str | None = None,
    history_tokens: int = 0,
) -> tuple[str, ModelRoute]:
    """Return ``(route_name, ModelRoute)`` for this turn.

    Precedence: explicit override > explicit user escalation > trivial >
    tools > default chat.
    """
    if forced and forced in ROUTES:
        return forced, ROUTES[forced]

    text = (user_message or "").strip()

    if _MAX_PAT.search(text):
        return "max", ROUTES["max"]

    if _THINK_PAT.search(text):
        return "think", ROUTES["think"]

    # Very long context is what `think` (1M ctx) is for.
    if history_tokens > 100_000:
        return "think", ROUTES["think"]

    if _TRIVIAL_PAT.match(text) or (len(text) < 24 and "?" not in text):
        return "fast", ROUTES["fast"]

    if tools_available:
        # Tool turns need a model that reliably emits well-formed calls.
        return "tools", ROUTES["tools"]

    return DEFAULT_ROUTE, ROUTES[DEFAULT_ROUTE]


def route_table() -> list[dict]:
    """Introspection payload for the UI / ``/routes`` endpoint."""
    return [
        {
            "route": name,
            "deployment": r.deployment,
            "input_per_1m": r.input_per_1m,
            "output_per_1m": r.output_per_1m,
            "supports_tools": r.supports_tools,
            "reasoning": r.reasoning,
        }
        for name, r in ROUTES.items()
    ]
