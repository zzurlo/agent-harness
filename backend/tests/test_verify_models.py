"""Tests for scripts/verify_models.py.

Each failure mode is simulated with a fake client, so the script's diagnosis
logic is verified without any Foundry deployment.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import verify_models as vm  # noqa: E402


def _resp(content="ok", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def _call(name="get_weather", arguments='{"city": "Chicago"}'):
    return SimpleNamespace(
        id="c1", function=SimpleNamespace(name=name, arguments=arguments)
    )


class FakeClient:
    """Returns queued responses, or raises queued exceptions, in order."""

    def with_options(self, **kwargs):
        self.options = kwargs
        return self

    def __init__(self, *outcomes):
        self._outcomes = list(outcomes)
        self.calls = []

        async def create(**kwargs):
            self.calls.append(kwargs)
            out = self._outcomes.pop(0) if self._outcomes else _resp()
            if isinstance(out, Exception):
                raise out
            return out

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


def _install(monkeypatch, client):
    monkeypatch.setattr(vm, "get_client", lambda: client)


async def test_healthy_tool_route_passes(monkeypatch):
    _install(monkeypatch, FakeClient(_resp("ok"), _resp(None, [_call()])))
    res = await vm.probe_route("tools", timeout=5, skip_tools=False)

    assert res.reachable is True
    assert res.tools_work is True
    assert res.errors == []
    assert res.latency_ms is not None


async def test_model_that_ignores_tools_is_caught(monkeypatch):
    """The quiet killer: accepts the schema, never calls anything."""
    _install(monkeypatch, FakeClient(_resp("ok"), _resp("It is sunny.", None)))
    res = await vm.probe_route("tools", timeout=5, skip_tools=False)

    assert res.reachable is True
    assert res.tools_work is False
    assert res.failed
    assert "silently never use tools" in res.errors[0]


async def test_missing_deployment_is_diagnosed(monkeypatch):
    exc = Exception("DeploymentNotFound: the deployment does not exist")
    exc.status_code = 404
    _install(monkeypatch, FakeClient(exc))

    res = await vm.probe_route("chat", timeout=5, skip_tools=False)
    assert res.reachable is False
    assert "deployment not found" in res.errors[0]


async def test_auth_failure_mentions_the_role(monkeypatch):
    exc = Exception("PermissionDenied")
    exc.status_code = 403
    _install(monkeypatch, FakeClient(exc))

    res = await vm.probe_route("chat", timeout=5, skip_tools=False)
    assert "Foundry User" in res.errors[0]


async def test_rate_limit_is_diagnosed(monkeypatch):
    exc = Exception("Too many requests")
    exc.status_code = 429
    _install(monkeypatch, FakeClient(exc))

    res = await vm.probe_route("chat", timeout=5, skip_tools=False)
    assert "rate limited" in res.errors[0]


async def test_malformed_tool_arguments_are_caught(monkeypatch):
    _install(
        monkeypatch,
        FakeClient(_resp("ok"), _resp(None, [_call(arguments="{not json")])),
    )
    res = await vm.probe_route("tools", timeout=5, skip_tools=False)
    assert any("malformed JSON" in e for e in res.errors)


async def test_missing_required_arg_fails(monkeypatch):
    _install(
        monkeypatch, FakeClient(_resp("ok"), _resp(None, [_call(arguments="{}")]))
    )
    res = await vm.probe_route("tools", timeout=5, skip_tools=False)
    assert res.tools_work is False
    assert any("'city'" in e for e in res.errors)
    assert res.failed


async def test_empty_content_fails_about_thinking_budget(monkeypatch):
    _install(monkeypatch, FakeClient(_resp("")))
    res = await vm.probe_route("think", timeout=5, skip_tools=True)
    assert res.reachable is True
    assert res.failed
    assert any("thinking tokens" in e for e in res.errors)


async def test_non_tool_route_skips_the_tool_probe(monkeypatch):
    client = FakeClient(_resp("ok"))
    _install(monkeypatch, client)

    res = await vm.probe_route("fast", timeout=5, skip_tools=False)
    assert res.tools_work is None
    assert len(client.calls) == 1, "should not probe tools on a non-tool route"


async def test_skip_tools_flag_is_respected(monkeypatch):
    client = FakeClient(_resp("ok"))
    _install(monkeypatch, client)

    res = await vm.probe_route("tools", timeout=5, skip_tools=True)
    assert res.tools_work is None
    assert len(client.calls) == 1


async def test_timeout_is_reported(monkeypatch):
    import asyncio

    class SlowClient(FakeClient):
        def __init__(self):
            async def create(**kwargs):
                await asyncio.sleep(10)

            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))

    _install(monkeypatch, SlowClient())
    res = await vm.probe_route("chat", timeout=0.1, skip_tools=True)
    assert res.failed
    assert "no response" in res.errors[0]


def test_result_serializes_to_json():
    res = vm.RouteResult(route="chat", deployment="m", expects_tools=True)
    res.errors.append("boom")
    payload = json.dumps(res.as_dict())
    assert json.loads(payload)["errors"] == ["boom"]


def test_probe_tool_schema_is_valid():
    fn = vm.PROBE_TOOL["function"]
    assert fn["parameters"]["required"] == ["city"]
    assert "city" in fn["parameters"]["properties"]
