"""Failure-path and complete workflow orchestration regressions."""
import json
import subprocess
from pathlib import Path

import pytest
from test_deployment import ENV, TOKEN, helper, live


@pytest.mark.parametrize("action", ["deploy", "what-if"])
def test_infra_rerun_submits_live_image_and_secure_runtime_params(monkeypatch, action):
    d = helper()
    monkeypatch.setattr(d, "verify_infra", lambda *args: None)
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "test-subscription")
    current = live("example.azurecr.io/api:real-sha")
    current["name"] = "ca-agentharness-api"
    current["properties"]["template"]["revisionSuffix"] = "current"
    current["properties"]["template"]["containers"][0]["env"] += [
        {"name": k, "value": v} for k, v in ENV.items() if k != "APP_ACCESS_TOKEN"]
    current["properties"]["template"]["containers"][0]["env"].append(
        {"name": "APP_ACCESS_TOKEN", "secretRef": "app-access-token"})
    current["properties"]["configuration"]["registries"] = [{"server": "example.azurecr.io", "identity": "system"}]
    calls, files = [], []
    def az(args):
        calls.append(args)
        if args[:2] == ["group", "exists"]:
            return True
        if args[:2] == ["containerapp", "list"]:
            return [current]
        if args[:2] == ["containerapp", "show"]:
            return current
        if "listSecrets" in " ".join(args):
            return {"value": [{"name": "app-access-token", "value": TOKEN}]}
        if args[:2] == ["deployment", "group"]:
            filename = args[args.index("-p") + 1][1:]
            files.append(filename)
            p = json.loads(Path(filename).read_text())["parameters"]
            assert p["containerImage"]["value"] == "example.azurecr.io/api:real-sha"
            assert p["location"]["value"] == "eastus2"
            assert p["appAccessToken"]["value"] == TOKEN
            assert p["revisionSuffix"]["value"].startswith("infra-") if action == "deploy" else p["revisionSuffix"]["value"] == "current"
            assert p["runtimeConfig"]["value"]["registries"][0]["identity"] == "system"
            if action == "what-if":
                assert "ResourceIdOnly" in args
        return {}
    d.deploy_infra("rg", "agentharness", "eastus2", action, {}, az=az)
    assert TOKEN not in str(calls)
    assert files and not Path(files[0]).exists()
    assert not any(a[:2] == ["group", "create"] for a in calls)


def test_missing_group_preview_is_read_only_and_bootstrap_forwards_location():
    d = helper()
    calls = []
    def az(args):
        calls.append(args)
        if args[:2] == ["group", "exists"]:
            return False
        if args[:2] == ["deployment", "group"]:
            p = json.loads(Path(args[args.index("-p") + 1][1:]).read_text())["parameters"]
            assert p["containerImage"]["value"] == d.PLACEHOLDER
            assert p["useAcrImage"]["value"] is False
        return {}
    with pytest.raises(ValueError, match="what-if does not create"):
        d.deploy_infra("rg", "agentharness", "eastus2", "what-if", {}, az=az)
    assert len(calls) == 1
    d.deploy_infra("rg", "agentharness", "eastus2", "deploy", {}, az=az)
    assert ["group", "create", "-n", "rg", "-l", "eastus2"] in calls


def test_azure_failure_never_prints_secret_response(monkeypatch, capsys):
    d = helper()
    monkeypatch.setattr(d.subprocess, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 1, TOKEN, TOKEN))
    with pytest.raises(RuntimeError) as error:
        d.azure(["rest", "--method", "patch"])
    assert TOKEN not in str(error.value)
    assert TOKEN not in str(capsys.readouterr())


@pytest.mark.parametrize("state", ["missing-container", "pending-revision", "wrong-region"])
def test_ambiguous_live_state_never_silently_bootstraps(state):
    d = helper()
    app = live()
    location = "eastus2"
    if state == "missing-container":
        app["properties"]["template"]["containers"] = []
    elif state == "pending-revision":
        app["properties"].update(latestRevisionName="bad-placeholder", latestReadyRevisionName="real-image")
    else:
        location = "centralus"
    with pytest.raises(ValueError):
        d.parameters(app, [], {}, location)


def test_existing_table_binding_is_not_discarded():
    d = helper()
    app = live()
    binding = {"name": "TABLES_CONNECTION_STRING", "secretRef": "custom-table-secret"}
    app["properties"]["template"]["containers"][0]["env"].append(binding)
    p = d.parameters(app, [], {}, "eastus2")
    assert binding in p["runtimeEnv"]
