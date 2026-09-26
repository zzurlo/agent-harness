from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.safe_load((ROOT / f".github/workflows/{name}.yml").read_text())


def test_infrastructure_mutation_is_manual_main_only_and_preview_is_default():
    w = workflow("infra")
    triggers = w.get("on", w.get(True))
    assert triggers["workflow_dispatch"]["inputs"]["action"]["default"] == "what-if"
    job = w["jobs"]["deploy"]
    assert "github.event_name == 'workflow_dispatch'" in job["if"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert "preview" not in w["jobs"]
    assert w["permissions"] == {"contents": "read"}
    assert job["permissions"]["id-token"] == "write"


def test_infra_and_backend_serialize_without_cancelling_a_deployment():
    infra = workflow("infra")["jobs"]["deploy"]["concurrency"]
    backend = workflow("backend")["jobs"]["deploy"]["concurrency"]
    assert infra == backend
    assert infra["cancel-in-progress"] is False


def test_backend_requires_runtime_verification_and_never_probes_with_ci_identity():
    text = (ROOT / ".github/workflows/backend.yml").read_text()
    assert "continue-on-error" not in text
    assert "backend/scripts/verify_models" not in text
    assert "scripts/verify_deployment.py" in text
    assert "APP_ACCESS_TOKEN: ${{ secrets.APP_ACCESS_TOKEN }}" in text
    assert "FOUNDRY_MODEL_ENDPOINT:" in text
    assert "|| true" not in text


def test_frontend_only_deploys_main_prebuilt_dist_with_public_url():
    w = workflow("frontend")
    assert "github.ref == 'refs/heads/main'" in w["jobs"]["deploy"]["if"]
    assert "close_pr" not in w["jobs"]
    step = next(s for s in w["jobs"]["deploy"]["steps"] if s.get("uses", "").startswith("Azure/static-web-apps-deploy"))
    assert step["with"]["app_location"] == "frontend/dist"
    assert step["with"]["output_location"] == ""
    assert step["with"]["skip_app_build"] is True
    assert "secrets.VITE_API_BASE" not in str(w)
    assert "APP_ACCESS_TOKEN" not in str(w)


def test_app_deployments_require_explicit_dispatch_during_bootstrap():
    for name in ("backend", "frontend"):
        condition = workflow(name)["jobs"]["deploy"]["if"]
        assert "github.event_name == 'workflow_dispatch'" in condition


def test_storage_account_name_respects_azure_length_limit():
    text = (ROOT / "infra/main.bicep").read_text()
    # Prefix is bounded but keeps the entire resource-group hash for uniqueness.
    assert "name: 'st${take(name, 9)}${uniq}'" in text
    assert len("st" + "agentharness"[:9] + "x" * 13) <= 24


def test_bicep_bootstrap_probe_scaling_cors_and_auth_contract():
    text = (ROOT / "infra/main.bicep").read_text()
    assert "targetPort: useAcrImage ? 8000 : 80" in text
    assert "path: useAcrImage ? '/health' : '/'" in text
    assert "port: useAcrImage ? 8000 : 80" in text
    assert "activeRevisionsMode: 'Single'" in text
    assert "maxReplicas: 1" in text
    assert "minReplicas: 0" in text
    assert "corsPolicy:" not in text
    assert "swa.properties.defaultHostname" in text
    assert "param appAccessToken string" in text
    assert "secretRef: 'app-access-token'" in text
