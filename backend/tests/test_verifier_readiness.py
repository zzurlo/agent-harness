"""Hard readiness, machine output and bounded offline verification."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from azure.core.exceptions import ClientAuthenticationError
from openai import AsyncOpenAI

from tests.test_verify_models import FakeClient, _call, _install, _resp, vm


@pytest.mark.parametrize("call", [_call(name="wrong"), _call(arguments="{}"),
    _call(arguments='{"city": 1}'), _call(arguments='{"city": ""}'),
    _call(arguments="null"), _call(arguments="[]"), _call(arguments="{bad")])
async def test_invalid_tool_calls_fail(monkeypatch, call):
    _install(monkeypatch, FakeClient(_resp(), _resp(tool_calls=[_call(), call])))
    result = await vm.probe_route("tools", 1, False)
    assert result.failed
    assert result.tools_work is False


async def test_reasoning_probe_uses_route_budget(monkeypatch):
    client = FakeClient(_resp(), _resp(tool_calls=[_call()]))
    _install(monkeypatch, client)
    await vm.probe_route("tools", 1, False)
    assert all(c["max_tokens"] == vm.ROUTES["tools"].max_output_tokens for c in client.calls)


async def test_client_configuration_exception_is_a_result(monkeypatch):
    def broken():
        raise ValueError("Set FOUNDRY_MODEL_ENDPOINT to https://resource/openai/v1")
    monkeypatch.setattr(vm, "get_client", broken)
    res = await vm.probe_route("chat", 1, False)
    assert res.failed and res.configuration_error
    assert "FOUNDRY_MODEL_ENDPOINT" in res.errors[0]


async def test_credential_failure_emits_json_exit2(monkeypatch, capsys):
    _install(monkeypatch, FakeClient(ClientAuthenticationError("private credential details")))
    monkeypatch.setattr(sys, "argv", ["verify_models", "--json", "--route", "chat"])
    code = await vm.main()
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload[0]["route"] == "chat"
    assert "credential" in payload[0]["errors"][0].lower()
    assert "private credential details" not in str(payload)


@pytest.mark.parametrize("env", [{"FOUNDRY_MODEL_ENDPOINT": "https://bad/api/projects/p"},
    {"MAX_TOOL_CALLS": "not-an-integer"}, {}])
def test_cli_configuration_json_without_network(env):
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("FOUNDRY_", "MAX_TOOL_CALLS"))}
    clean.update(env)
    command = [sys.executable, "scripts/verify_models.py", "--json"]
    process = subprocess.run(command, cwd=Path(__file__).parents[1], env=clean,
        capture_output=True, text=True, timeout=10)
    assert process.returncode == 2
    assert json.loads(process.stdout)[0]["errors"]
    assert "Traceback" not in process.stderr


async def test_sdk_rate_limit_is_not_retried(monkeypatch):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(429, json={"error": {"message": "quota"}},
            headers={"retry-after": "60"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = AsyncOpenAI(api_key="fake", base_url="https://example/openai/v1", http_client=http)
        _install(monkeypatch, client)
        result = await asyncio.wait_for(vm.probe_route("fast", 0.1, False), 1)
    assert result.failed
    assert "rate limited" in result.errors[0]
    assert len(requests) == 1


async def test_sequential_total_budget_returns_all_routes(monkeypatch):
    running, seen = 0, []
    async def probe(name, timeout, skip_tools):
        nonlocal running
        running += 1
        assert running == 1
        seen.append(name)
        try:
            if name == "chat":
                await asyncio.sleep(10)
            return vm.RouteResult(name, "fake", False, reachable=True)
        finally:
            running -= 1
    monkeypatch.setattr(vm, "probe_route", probe)
    results = await vm.verify_routes(["fast", "chat", "tools"], total_timeout=0.02)
    assert [r.route for r in results] == ["fast", "chat", "tools"]
    assert not results[0].failed
    assert results[1].failed and results[2].failed
    assert seen == ["fast", "chat"]
