"""Configuration: model routes, budgets, endpoints.

Everything here is env-overridable so you can swap models without code changes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

RouteName = Literal["fast", "chat", "tools", "think", "max"]


@dataclass(frozen=True)
class ModelRoute:
    """A single tier in the routing table.

    Prices are USD per 1M tokens and are used only for local cost accounting --
    they do not affect billing. Verify against the Foundry catalog for your
    region at deploy time; serverless pricing varies by region and changes.
    """

    deployment: str
    input_per_1m: float
    output_per_1m: float
    supports_tools: bool = False
    reasoning: bool = False
    max_output_tokens: int = 2048

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens / 1_000_000 * self.input_per_1m
            + completion_tokens / 1_000_000 * self.output_per_1m
        )


def _route(name: str, default_deployment: str, **kw) -> ModelRoute:
    """Build a route, letting env vars override the deployment name."""
    return ModelRoute(
        deployment=os.getenv(f"MODEL_{name.upper()}", default_deployment), **kw
    )


# ---------------------------------------------------------------------------
# Routing table
# ---------------------------------------------------------------------------
# Rationale (see README):
#   fast  -> tiny non-reasoning model for titles/compaction/classification
#   chat  -> NON-reasoning instruct model. This is the big cost lever: most
#            conversational turns do not deserve hidden thinking tokens.
#   tools -> must be tool-calling capable and reliable. Grok 4.1 Fast Reasoning
#            is the cheap option explicitly tagged for tool use.
#   think -> explicit hard reasoning / long context. V4 Flash is 1M ctx.
#   max   -> opt-in escalation only, never automatic.
ROUTES: dict[str, ModelRoute] = {
    "fast": _route(
        "fast",
        "Phi-4-mini-reasoning",
        input_per_1m=0.08,
        output_per_1m=0.32,
        max_output_tokens=512,
    ),
    "chat": _route(
        "chat",
        "Llama-3.3-70B-Instruct",
        input_per_1m=0.20,
        output_per_1m=0.60,
        supports_tools=True,
        max_output_tokens=2048,
    ),
    "tools": _route(
        "tools",
        "grok-4.1-fast-reasoning",
        input_per_1m=0.20,
        output_per_1m=0.50,
        supports_tools=True,
        reasoning=True,
        max_output_tokens=2048,
    ),
    "think": _route(
        "think",
        "DeepSeek-V4-Flash",
        input_per_1m=0.19,
        output_per_1m=0.51,
        reasoning=True,
        max_output_tokens=4096,
    ),
    "max": _route(
        "max",
        "DeepSeek-V3.2",
        input_per_1m=0.58,
        output_per_1m=1.68,
        supports_tools=True,
        reasoning=True,
        max_output_tokens=4096,
    ),
}

DEFAULT_ROUTE = "chat"


@dataclass(frozen=True)
class Budgets:
    """Hard stops. The harness aborts gracefully rather than looping forever."""

    max_tool_calls_per_turn: int = int(os.getenv("MAX_TOOL_CALLS", "8"))
    max_wall_clock_seconds: float = float(os.getenv("MAX_WALL_CLOCK", "120"))
    max_total_tokens_per_turn: int = int(os.getenv("MAX_TURN_TOKENS", "60000"))
    max_tool_seconds: float = float(os.getenv("MAX_TOOL_SECONDS", "30"))


@dataclass(frozen=True)
class ContextPolicy:
    """When to compact. Compaction is the highest-leverage feature in a chat harness."""

    # Model context windows vary wildly (V4 Flash is 1M). Keep a conservative
    # working budget rather than trusting the model max.
    working_window_tokens: int = int(os.getenv("WORKING_WINDOW", "24000"))
    compact_at_ratio: float = float(os.getenv("COMPACT_AT_RATIO", "0.70"))
    # How many of the most recent messages are never compacted.
    keep_recent_messages: int = int(os.getenv("KEEP_RECENT", "6"))


@dataclass(frozen=True)
class Settings:
    project_endpoint: str = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "")
    api_key: str | None = os.getenv("FOUNDRY_API_KEY") or None
    api_version: str = os.getenv("FOUNDRY_API_VERSION", "2024-10-21")
    system_prompt: str = os.getenv(
        "SYSTEM_PROMPT",
        "You are a helpful, direct assistant. Be concise unless asked otherwise.",
    )
    cors_origins: list[str] = field(
        default_factory=lambda: [
            o.strip()
            for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
            if o.strip()
        ]
    )
    budgets: Budgets = field(default_factory=Budgets)
    context: ContextPolicy = field(default_factory=ContextPolicy)
    # Enables prompt caching hints where the backing model supports it.
    enable_prompt_cache: bool = os.getenv("ENABLE_PROMPT_CACHE", "1") == "1"


settings = Settings()
