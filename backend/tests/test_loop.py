"""End-to-end loop test with a mocked model.

Exercises the real agent loop: streaming, tool-call accumulation across chunks,
dispatch, result feedback, and the second model turn. No network.
"""
from __future__ import annotations

from types import SimpleNamespace

from harness.context import Conversation
from harness.loop import Harness
from harness.providers import foundry


def _chunk(content=None, tool_calls=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=None)
    return SimpleNamespace(choices=[choice], usage=usage)


def _tc(index, cid=None, name=None, args=None):
    return SimpleNamespace(
        index=index,
        id=cid,
        function=SimpleNamespace(name=name, arguments=args),
    )


async def test_plain_turn_streams_tokens(monkeypatch):
    async def fake_stream(**kwargs):
        for word in ["Hello", " there", "!"]:
            yield _chunk(content=word)
        yield _chunk(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3))

    monkeypatch.setattr(foundry, "stream_completion", fake_stream)

    h = Harness(Conversation())
    events = [e async for e in h.run("Explain SSE briefly", enable_tools=False)]
    kinds = [e["type"] for e in events]

    assert "route" in kinds
    assert "".join(e["text"] for e in events if e["type"] == "token") == "Hello there!"

    done = events[-1]
    assert done["type"] == "done"
    assert done["stats"]["prompt_tokens"] == 10
    assert done["stats"]["cost_usd"] > 0
    assert h.conv.messages[-1]["role"] == "assistant"


async def test_tool_call_round_trip(monkeypatch):
    """Model requests a tool, harness runs it, model answers with the result."""
    calls = {"n": 0}

    async def fake_stream(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            # Tool call arrives fragmented across chunks -- the accumulator
            # must stitch id/name/arguments back together.
            yield _chunk(tool_calls=[_tc(0, cid="call_1", name="calcu")])
            yield _chunk(tool_calls=[_tc(0, name="late", args='{"expr')])
            yield _chunk(tool_calls=[_tc(0, args='ession": "2+2"}')])
            yield _chunk(usage=SimpleNamespace(prompt_tokens=50, completion_tokens=12))
        else:
            yield _chunk(content="The answer is 4.")
            yield _chunk(usage=SimpleNamespace(prompt_tokens=80, completion_tokens=5))

    monkeypatch.setattr(foundry, "stream_completion", fake_stream)

    h = Harness(Conversation())
    events = [e async for e in h.run("what is 2+2", forced_route="tools")]
    kinds = [e["type"] for e in events]

    assert "tool_start" in kinds and "tool_end" in kinds
    assert next(e for e in events if e["type"] == "tool_end")["ok"] is True
    assert "The answer is 4." in "".join(
        e["text"] for e in events if e["type"] == "token"
    )

    roles = [m["role"] for m in h.conv.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert h.conv.messages[2]["tool_call_id"] == "call_1"
    assert events[-1]["stats"]["tool_calls"] == 1


async def test_tool_call_budget_stops_runaway_loop(monkeypatch):
    """A model that calls tools forever must be stopped by the budget."""

    async def fake_stream(**kwargs):
        yield _chunk(
            tool_calls=[_tc(0, cid="c", name="calculate", args='{"expression":"1+1"}')]
        )
        yield _chunk(usage=SimpleNamespace(prompt_tokens=5, completion_tokens=5))

    monkeypatch.setattr(foundry, "stream_completion", fake_stream)

    h = Harness(Conversation())
    events = [e async for e in h.run("loop forever", forced_route="tools")]

    exceeded = [e for e in events if e["type"] == "budget_exceeded"]
    assert exceeded, "runaway tool loop was not stopped"
    assert events[-1]["type"] == "done"


async def test_model_failure_surfaces_as_error_event(monkeypatch):
    async def fake_stream(**kwargs):
        raise RuntimeError("upstream 503")
        yield  # pragma: no cover

    monkeypatch.setattr(foundry, "stream_completion", fake_stream)

    h = Harness(Conversation())
    events = [e async for e in h.run("hello there friend", enable_tools=False)]

    assert any(e["type"] == "error" for e in events)
    assert events[-1]["type"] == "done", "loop must still close cleanly"


async def test_non_tool_route_does_not_advertise_tools(monkeypatch):
    """Reasoning models without tool support must not receive tool schemas."""
    seen = {}

    async def fake_stream(**kwargs):
        seen["tools"] = kwargs.get("tools")
        yield _chunk(content="ok")

    monkeypatch.setattr(foundry, "stream_completion", fake_stream)

    h = Harness(Conversation())
    _ = [e async for e in h.run("think hard about this", enable_tools=True)]
    assert seen["tools"] is None
