"""Tests for the pure logic: routing, context accounting, tool registry.

No network. These run in CI without a Foundry deployment.
"""
from __future__ import annotations

import pytest

from harness.config import ROUTES
from harness.context import Conversation, count_tokens
from harness.router import select_route
from harness.tools.registry import ToolRegistry


# --- routing ---------------------------------------------------------------
def test_trivial_turns_use_cheapest_route():
    name, route = select_route("hey")
    assert name == "fast"
    assert route.input_per_1m <= ROUTES["chat"].input_per_1m


def test_default_is_non_reasoning_chat():
    name, route = select_route("Can you explain how SSE differs from websockets?")
    assert name == "chat"
    assert route.reasoning is False, "default chat route must not burn thinking tokens"


def test_tool_turns_pick_a_tool_capable_model():
    name, route = select_route(
        "What time is it in Tokyo right now?", tools_available=True
    )
    assert name == "tools"
    assert route.supports_tools is True


def test_explicit_escalation():
    name, _ = select_route("think hard about this proof")
    assert name == "think"


def test_forced_route_wins():
    name, _ = select_route("hey", forced="max")
    assert name == "max"


def test_huge_history_routes_to_long_context():
    name, _ = select_route("summarize", history_tokens=250_000)
    assert name == "think"


def test_every_tool_route_is_tool_capable():
    assert ROUTES["tools"].supports_tools, "tools route must support function calling"


# --- context ---------------------------------------------------------------
def test_compaction_triggers_past_threshold():
    conv = Conversation()
    assert not conv.needs_compaction()
    for _ in range(400):
        conv.add("user", "x" * 400)
    assert conv.needs_compaction()


def test_compaction_never_orphans_tool_messages():
    conv = Conversation()
    conv.add("user", "hello")
    conv.add("assistant", None, tool_calls=[{"id": "c1", "function": {"name": "t"}}])
    conv.add("tool", '{"ok": true}', tool_call_id="c1")
    conv.add("user", "thanks")
    _, kept = conv.split_for_compaction()
    roles = [m["role"] for m in kept]
    if "tool" in roles:
        assert "assistant" in roles, "tool result kept without its parent call"


def test_token_counting_is_monotonic():
    assert count_tokens("hello world") > 0
    assert count_tokens("a" * 1000) > count_tokens("a" * 10)


# --- tools -----------------------------------------------------------------
async def test_unknown_tool_returns_structured_error():
    reg = ToolRegistry()
    result = await reg.dispatch("nope", {}, timeout=1)
    assert "error" in result and result["retryable"] is False


async def test_tool_exception_is_recoverable_not_raised():
    reg = ToolRegistry()

    @reg.register()
    def boom(x: str) -> str:
        """Always fails."""
        raise RuntimeError("kaboom")

    result = await reg.dispatch("boom", {"x": "1"}, timeout=5)
    assert result["retryable"] is True
    assert "kaboom" in result["error"]


async def test_bad_arguments_are_recoverable():
    reg = ToolRegistry()

    @reg.register()
    def needs_arg(required: str) -> str:
        """Needs an arg."""
        return required

    result = await reg.dispatch("needs_arg", {"wrong": "x"}, timeout=5)
    assert result["retryable"] is True


async def test_tool_timeout_is_enforced():
    import asyncio

    reg = ToolRegistry()

    @reg.register()
    async def slow() -> str:
        """Too slow."""
        await asyncio.sleep(5)
        return "done"

    result = await reg.dispatch("slow", {}, timeout=0.1)
    assert result["retryable"] is True
    assert "exceeded" in result["error"]


def test_schema_generation_marks_required_params():
    reg = ToolRegistry()

    @reg.register()
    def sample(a: str, b: int = 3) -> str:
        """Sample tool."""
        return a

    schema = reg.get("sample").schema["function"]
    assert schema["parameters"]["required"] == ["a"]
    assert schema["parameters"]["properties"]["b"]["type"] == "integer"
    assert schema["description"] == "Sample tool."


def test_dangerous_flag_survives_registration():
    reg = ToolRegistry()

    @reg.register(dangerous=True)
    def risky() -> str:
        """Risky."""
        return "ok"

    assert reg.get("risky").dangerous is True
