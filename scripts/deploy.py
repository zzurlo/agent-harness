#!/usr/bin/env python3
"""State-preserving deployment; Azure output and secret-bearing errors stay private.

Only this helper should deploy main.bicep. Direct deployment with its bootstrap
parameters would replace a live application. No secret values belong in argv.
"""
import argparse
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.parse import urlsplit

PLACEHOLDER = "mcr.microsoft.com/k8se/quickstart:latest"
API_VERSION = "2024-03-01"
RUNTIME_KEYS = ("FOUNDRY_MODEL_ENDPOINT", "FOUNDRY_PROJECT_ENDPOINT", "CORS_ORIGINS",
                "MODEL_FAST", "MODEL_CHAT", "MODEL_TOOLS", "MODEL_THINK", "MODEL_MAX")


@contextmanager
def private_json(value):
    fd, filename = tempfile.mkstemp(prefix="harness-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
        yield filename
    finally:
        os.unlink(filename)


def azure(args):
    result = subprocess.run(["az", *args, "--only-show-errors", "-o", "json"],
                            capture_output=True, text=True, timeout=1800)
    if result.returncode:
        # Azure errors may include request bodies. Never relay stderr/stdout.
        raise RuntimeError("Azure operation failed; inspect the Azure activity log (output withheld)")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def parameters(live, secrets, overrides, location, image=None):
    props = live["properties"] if live else {}
    containers = props.get("template", {}).get("containers", [])
    if containers and (len(containers) != 1 or containers[0]["name"] != "api"):
        raise ValueError("Expected exactly one api container; refusing to discard other containers")
    current = containers[0] if containers else {}
    image = image or current.get("image", PLACEHOLDER)
    real = image != PLACEHOLDER
    values = {e["name"]: deepcopy(e) for e in current.get("env", [])}
    for key in RUNTIME_KEYS:
        if key in overrides:
            values[key] = {"name": key, "value": overrides[key]}
    secret_values = {s["name"]: s for s in secrets}
    token_ref = values.get("APP_ACCESS_TOKEN", {}).get("secretRef", "app-access-token")
    token = overrides.get("APP_ACCESS_TOKEN", secret_values.get(token_ref, {}).get("value", ""))
    if not token:
        token = values.get("APP_ACCESS_TOKEN", {}).get("value", "")
    if real:
        if len(token) < 32 or any(c.isspace() for c in token):
            raise ValueError("APP_ACCESS_TOKEN must be a strong random token of at least 32 characters")
        endpoint = values.get("FOUNDRY_MODEL_ENDPOINT", {}).get("value", "")
        url = urlsplit(endpoint)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path.rstrip("/") != "/openai/v1"):
            raise ValueError("FOUNDRY_MODEL_ENDPOINT must be an explicit HTTPS resource /openai/v1 endpoint")
        for key in RUNTIME_KEYS:
            if key.startswith("MODEL_") and not values.get(key, {}).get("value", "").strip():
                raise ValueError(f"{key} must name an explicitly configured deployment")
    cors = values.get("CORS_ORIGINS", {}).get("value", "")
    if "*" in cors:
        raise ValueError("CORS_ORIGINS must contain explicit origins, not wildcards")
    values.pop("APP_ACCESS_TOKEN", None)
    values.pop("TABLES_CONNECTION_STRING", None)
    return {"location": location, "containerImage": image, "useAcrImage": real,
            "runtimeEnv": list(values.values()), "appAccessToken": token,
            "additionalSecrets": [deepcopy(s) for s in secrets
                                  if s["name"] not in ("tables-connection", "app-access-token")],
            "existingRegistries": deepcopy(props.get("configuration", {}).get("registries", []))}


def arm_parameters(params):
    params = deepcopy(params)
    params["runtimeConfig"] = {
        "env": params.pop("runtimeEnv"), "secrets": params.pop("additionalSecrets"),
        "registries": params.pop("existingRegistries")}
    return {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
            "contentVersion": "1.0.0.0", "parameters": {k: {"value": v} for k, v in params.items()}}


def app_url(group, app):
    return (f"/subscriptions/{os.environ['AZURE_SUBSCRIPTION_ID']}/resourceGroups/{group}"
            f"/providers/Microsoft.App/containerApps/{app}")


def deploy_backend(group, app, image, overrides, az=azure):
    live = az(["containerapp", "show", "-g", group, "-n", app])
    secrets = read_secrets(group, app, az)
    p = parameters(live, secrets, overrides, live["location"], image)
    server = image.split("/")[0]
    if not server.endswith(".azurecr.io"):
        raise ValueError("The production image must be hosted in ACR")
    registries = [r for r in p["existingRegistries"] if r["server"] != server]
    registries.append({"server": server, "identity": "system"})
    # AcrPull was assigned AFTER bootstrap by Bicep. Bind BEFORE changing image.
    az(["containerapp", "registry", "set", "-g", group, "-n", app,
        "--server", server, "--identity", "system"])
    template = deepcopy(live["properties"]["template"])
    container = template["containers"][0]
    env = p["runtimeEnv"] + [{"name": "APP_ACCESS_TOKEN", "secretRef": "app-access-token"},
                              {"name": "TABLES_CONNECTION_STRING", "secretRef": "tables-connection"}]
    container.update(image=image, env=env, probes=[{
        "type": "Readiness", "httpGet": {"path": "/health", "port": 8000},
        "initialDelaySeconds": 3, "periodSeconds": 5}])
    template.setdefault("scale", {}).update(minReplicas=0, maxReplicas=1)
    # Always force a fresh revision, including token-only rotations.
    template["revisionSuffix"] = f"deploy-{time.time_ns()}"
    ingress = deepcopy(live["properties"]["configuration"].get("ingress", {}))
    ingress.pop("fqdn", None)
    ingress["corsPolicy"] = None  # Remove legacy edge CORS; backend is authoritative.
    ingress.update(targetPort=8000, external=True, allowInsecure=False)
    kept = [s for s in secrets if s["name"] != "app-access-token"]
    kept.append({"name": "app-access-token", "value": p["appAccessToken"]})
    payload = {"properties": {"template": template, "configuration": {
        "activeRevisionsMode": "Single", "ingress": ingress,
        "registries": registries, "secrets": kept}}}
    with private_json(payload) as filename:
        az(["rest", "--method", "patch", "--url", f"{app_url(group, app)}?api-version={API_VERSION}",
            "--body", "@" + filename])
    deadline = time.monotonic() + 600
    expected = f"{app}--{template['revisionSuffix']}"
    while time.monotonic() < deadline:
        state = az(["containerapp", "show", "-g", group, "-n", app])["properties"]
        if state.get("provisioningState") == "Failed":
            raise RuntimeError("Container App revision provisioning failed")
        if state.get("latestReadyRevisionName") == expected:
            return
        time.sleep(5)
    raise RuntimeError("New revision did not become ready within 600 seconds")


def deploy_infra(group, name, location, action, overrides, az=azure):
    # List, rather than catch show errors: authorization/network failures must
    # never be interpreted as permission to restore the bootstrap image.
    exists = az(["group", "exists", "-n", group])
    apps = az(["containerapp", "list", "-g", group]) if exists else []
    app = f"ca-{name}-api"
    match = next((a for a in apps if a["name"] == app), None)
    live = az(["containerapp", "show", "-g", group, "-n", app]) if match else None
    secrets = read_secrets(group, app, az) if live else []
    p = parameters(live, secrets, overrides, location)
    p["name"] = name
    if not exists:
        if action == "what-if":
            raise ValueError("Resource group absent: what-if does not create it; explicitly choose deploy to bootstrap")
        az(["group", "create", "-n", group, "-l", location])
    with private_json(arm_parameters(p)) as filename:
        args = ["deployment", "group", "create" if action == "deploy" else "what-if",
                "-g", group, "-f", str(Path(__file__).resolve().parents[1] / "infra/main.bicep"),
                "-p", "@" + filename]
        if action == "what-if":
            args += ["--no-pretty-print", "--result-format", "ResourceIdOnly"]
        result = az(args)
    if action == "what-if":
        for change in result.get("changes", []):
            print(change.get("changeType"), change.get("resourceId"))
    else:
        for key, value in result.get("properties", {}).get("outputs", {}).items():
            if key in ("apiUrl", "acrLoginServer", "swaHostname", "apiPrincipalId"):
                print(f"{key}={value['value']}")


def read_secrets(group, app, az):
    # Metadata from containerapp show lacks inline values; sending it back would
    # erase secrets. Capture listSecrets only in memory, never stdout/artifacts.
    return az(["rest", "--method", "post", "--url",
               f"{app_url(group, app)}/listSecrets?api-version={API_VERSION}"])["value"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("infra", "backend"))
    parser.add_argument("--action", choices=("what-if", "deploy"), default="what-if")
    parser.add_argument("--resource-group", default="rg-agent-harness")
    parser.add_argument("--name", default="agentharness")
    parser.add_argument("--location", default="centralus")
    parser.add_argument("--image")
    args = parser.parse_args()
    # Unset/empty GitHub variables mean preserve live settings, not erase them.
    overrides = {k: os.environ[k] for k in (*RUNTIME_KEYS, "APP_ACCESS_TOKEN") if os.environ.get(k)}
    if args.mode == "infra":
        deploy_infra(args.resource_group, args.name, args.location, args.action, overrides)
    else:
        if not args.image:
            parser.error("backend requires --image")
        deploy_backend(args.resource_group, f"ca-{args.name}-api", args.image, overrides)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        # Do not show subprocess exception text: it can contain captured secrets.
        message = str(error) if isinstance(error, (ValueError, RuntimeError)) else "Azure operation timed out"
        raise SystemExit(message) from None

