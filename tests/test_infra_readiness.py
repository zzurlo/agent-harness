"""Infra reruns must not bypass the backend runtime readiness gate (offline)."""
import json
import subprocess
from pathlib import Path

import pytest
from test_deployment import ENV, TOKEN, helper, live


@pytest.mark.parametrize("action,real", [("what-if", False), ("what-if", True), ("deploy", False), ("deploy", True)])
@pytest.mark.parametrize("returncode", [0, 1])
@pytest.mark.parametrize("rotate", [False, True])
def test_infra_runtime_gate(monkeypatch, capsys, action, real, returncode, rotate):
    d = helper()
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "offline")
    monkeypatch.setenv("APP_ACCESS_TOKEN", "stale-environment-token")
    current = live("example.azurecr.io/api:sha" if real else d.PLACEHOLDER)
    current["name"] = "ca-agentharness-api"
    current["properties"]["template"]["containers"][0]["env"] += [
        {"name": k, "value": v} for k, v in ENV.items() if k != "APP_ACCESS_TOKEN"]
    token = "rotated-" + TOKEN if rotate else TOKEN
    overrides = {"MODEL_TOOLS": "nonexistent-deployment"}
    if rotate:
        overrides["APP_ACCESS_TOKEN"] = token
    events = []

    def az(args):
        if args[:2] == ["group", "exists"]:
            return True
        if args[:2] == ["containerapp", "list"]:
            return [current]
        if args[:2] == ["containerapp", "show"]:
            return current
        if "listSecrets" in " ".join(args):
            return {"value": [{"name": "app-access-token", "value": TOKEN}]}
        if args[:2] == ["deployment", "group"]:
            events.append("deploy")
            p = json.loads(Path(args[args.index("-p") + 1][1:]).read_text())["parameters"]
            suffix = p["revisionSuffix"]["value"]
            current["properties"]["latestReadyRevisionName"] = f"ca-agentharness-api--{suffix}"
            return {"properties": {"outputs": {"apiUrl": {"value": "https://api.example.com"}}}}
        raise AssertionError(args)

    def run(args, **kwargs):
        assert events == ["deploy"]
        events.append("verify")
        assert Path(args[1]).name == "verify_deployment.py"
        assert args[args.index("--url") + 1] == "https://api.example.com"
        assert kwargs["env"]["APP_ACCESS_TOKEN"] == token
        assert token not in str(args)
        assert kwargs["capture_output"] is True
        return subprocess.CompletedProcess(args, returncode, token, token)

    monkeypatch.setattr(d.subprocess, "run", run)
    if action == "deploy" and real and returncode:
        with pytest.raises(RuntimeError, match="verification") as error:
            d.deploy_infra("rg", "agentharness", "eastus2", action, overrides, az=az)
        assert token not in str(error.value)
    else:
        d.deploy_infra("rg", "agentharness", "eastus2", action, overrides, az=az)
    assert events == (["deploy", "verify"] if action == "deploy" and real else ["deploy"])
    assert token not in str(capsys.readouterr())


@pytest.mark.parametrize("mode", ["pending", "failed", "rollout-timeout", "probe-timeout"])
def test_infra_waits_for_expected_revision_and_fails_closed(monkeypatch, mode):
    d = helper()
    state = live()["properties"]
    state["latestReadyRevisionName"] = "old-revision"
    events = []
    ticks = iter([0, 0, 601] if mode == "rollout-timeout" else [0, 0, 1])
    monkeypatch.setattr(d.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(d.time, "sleep", lambda seconds: events.append("wait"))

    def az(args):
        events.append("show")
        if mode == "failed":
            state["provisioningState"] = "Failed"
        if mode == "probe-timeout" or events.count("show") == 2:
            state["latestReadyRevisionName"] = "app--new"
        return {"properties": state}

    def run(args, **kwargs):
        events.append("verify")
        assert state["latestReadyRevisionName"] == "app--new"
        if mode == "probe-timeout":
            raise subprocess.TimeoutExpired(args, 600, output=TOKEN, stderr=TOKEN)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(d.subprocess, "run", run)
    params = {"revisionSuffix": "new", "appAccessToken": TOKEN}
    if mode == "pending":
        d.verify_infra("rg", "app", params, az)
        assert events == ["show", "wait", "show", "verify"]
    else:
        with pytest.raises(RuntimeError) as error:
            d.verify_infra("rg", "app", params, az)
        assert TOKEN not in str(error.value)
        assert ("verify" in events) == (mode == "probe-timeout")


def test_backend_prerequisite_runs_root_helpers_without_bicep():
    import yaml
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/backend.yml").read_text())
    assert workflow["jobs"]["deploy"]["needs"] == "test"
    steps = workflow["jobs"]["test"]["steps"]
    assert any("pyyaml" in step.get("run", "") for step in steps)
    assert any("pytest tests" in step.get("run", "") and
               "--ignore=tests/test_infra_registries.py" in step["run"] and
               step.get("working-directory", ".") == "." for step in steps)
