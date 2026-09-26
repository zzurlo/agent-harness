"""Deployment state transitions, with Azure calls replaced at the process boundary."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("deploy", ROOT / "scripts/deploy.py")


def helper():
    assert SPEC.origin and Path(SPEC.origin).exists(), "deployment helper is missing"
    module = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(module)
    return module


TOKEN = "a-long-random-production-token-0123456789"
ENV = {
    "APP_ACCESS_TOKEN": TOKEN,
    "FOUNDRY_MODEL_ENDPOINT": "https://example.openai.azure.com/openai/v1",
    **{f"MODEL_{route}": f"deployment-{route.lower()}" for route in
       ("FAST", "CHAT", "TOOLS", "THINK", "MAX")},
}


def live(image="mcr.microsoft.com/k8se/quickstart:latest"):
    return {"location": "eastus2", "properties": {
        "configuration": {"registries": [], "ingress": {"fqdn": "api.example.com"}},
        "template": {"containers": [{"name": "api", "image": image,
            "env": [{"name": "CUSTOM_SETTING", "value": "keep-me"}]}]},
    }}


def test_bootstrap_is_public_port_80_and_location_is_forwarded():
    d = helper()
    p = d.parameters(None, [], {}, "eastus2")
    assert p["containerImage"] == d.PLACEHOLDER
    assert p["useAcrImage"] is False
    assert p["location"] == "eastus2"
    assert p["runtimeEnv"] == []
    assert p["appAccessToken"] == ""


def test_real_promotion_and_infra_rerun_preserve_every_runtime_setting():
    d = helper()
    p = d.parameters(live(), [], ENV, "eastus2", "example.azurecr.io/api:sha")
    assert p["useAcrImage"] is True
    assert p["appAccessToken"] == TOKEN
    env = {e["name"]: e for e in p["runtimeEnv"]}
    assert env["CUSTOM_SETTING"]["value"] == "keep-me"
    assert env["MODEL_TOOLS"]["value"] == "deployment-tools"
    app = live(p["containerImage"])
    app["properties"]["template"]["containers"][0]["env"] = p["runtimeEnv"] + [
        {"name": "CORS_ORIGINS", "value": "https://ui.example.com"},
        {"name": "APP_ACCESS_TOKEN", "secretRef": "app-access-token"}]
    app["properties"]["configuration"]["registries"] = [
        {"server": "example.azurecr.io", "identity": "system"}]
    secrets = [{"name": "app-access-token", "value": TOKEN},
               {"name": "other-secret", "value": "retained"}]
    again = d.parameters(app, secrets, {}, "eastus2")
    assert again["containerImage"] == p["containerImage"]
    assert again["useAcrImage"] is True
    assert again["appAccessToken"] == TOKEN
    assert again["existingRegistries"] == app["properties"]["configuration"]["registries"]
    assert again["additionalSecrets"] == [{"name": "other-secret", "value": "retained"}]
    assert {e["name"]: e for e in again["runtimeEnv"]}["CORS_ORIGINS"]["value"] == "https://ui.example.com"


@pytest.mark.parametrize("key,value", [
    ("APP_ACCESS_TOKEN", "short"), ("FOUNDRY_MODEL_ENDPOINT", ""),
    ("FOUNDRY_MODEL_ENDPOINT", "https://example.com/api/projects/test"),
    ("FOUNDRY_MODEL_ENDPOINT", "http://example.com/openai/v1"),
    ("MODEL_TOOLS", ""), ("CORS_ORIGINS", "*"),
])
def test_production_fails_closed_for_invalid_configuration(key, value):
    d = helper()
    with pytest.raises(ValueError):
        d.parameters(live(), [], {**ENV, key: value}, "eastus2", "example.azurecr.io/api:sha")


def test_secure_parameter_file_is_private_and_removed_on_failure():
    d = helper()
    filename = None
    with pytest.raises(RuntimeError):
        with d.private_json({"secret": TOKEN}) as filename:
            assert os.stat(filename).st_mode & 0o777 == 0o600
            assert json.loads(Path(filename).read_text())["secret"] == TOKEN
            raise RuntimeError("simulated Azure failure")
    assert filename and not Path(filename).exists()


def test_image_update_binds_system_identity_before_patch_and_uses_private_body(monkeypatch):
    d = helper()
    monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "test-subscription")
    calls = []
    state = live()
    def az(args):
        calls.append(args)
        if args[:2] == ["containerapp", "show"]:
            return state
        if "listSecrets" in " ".join(args):
            return {"value": [{"name": "tables-connection", "value": "tables"}]}
        if "--body" in args:
            filename = args[args.index("--body") + 1][1:]
            payload = json.loads(Path(filename).read_text())
            assert os.stat(filename).st_mode & 0o777 == 0o600
            config = payload["properties"]["configuration"]
            assert config["ingress"]["targetPort"] == 8000
            assert config["activeRevisionsMode"] == "Single"
            assert payload["properties"]["template"]["scale"]["maxReplicas"] == 1
            state["properties"]["latestReadyRevisionName"] = "ca-api--" + payload["properties"]["template"]["revisionSuffix"]
        return {}
    d.deploy_backend("rg", "ca-api", "example.azurecr.io/api:sha", ENV, az=az)
    binding = next(i for i, a in enumerate(calls) if a[:3] == ["containerapp", "registry", "set"])
    patch = next(i for i, a in enumerate(calls) if "patch" in a)
    assert binding < patch
    assert "system" in calls[binding]
    assert TOKEN not in str(calls)
