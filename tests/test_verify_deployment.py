import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "a-long-random-production-token-0123456789"


def helper():
    path = ROOT / "scripts/verify_deployment.py"
    assert path.exists(), "bounded runtime verifier is missing"
    spec = importlib.util.spec_from_file_location("verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("ready", [True, False])
def test_runtime_verifier_checks_health_gate_and_all_routes(ready):
    d = helper()
    calls = []
    def request(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/health"):
            return 200, {"ok": True}
        if not kwargs.get("token"):
            return 401, {}
        return 200, {"ready": ready, "results": [{"route": "fast"}]}
    if ready:
        d.verify("https://api.example.com", TOKEN, request=request)
    else:
        with pytest.raises(RuntimeError, match="model"):
            d.verify("https://api.example.com", TOKEN, request=request)
    assert calls[0][0].endswith("/health")
    assert any(url.endswith("/threads") and not kw.get("token") for url, kw in calls)
    url, kwargs = calls[-1]
    assert url.endswith("/admin/verify-models")
    assert kwargs["method"] == "POST"
    assert kwargs["token"] == TOKEN
    assert kwargs["timeout"] == 900
    assert not kwargs.get("data")  # No route filter: server probes all configured routes.


@pytest.mark.parametrize("status", [200, 404, 500])
def test_missing_auth_gate_is_fatal(status):
    d = helper()
    with pytest.raises(RuntimeError, match="auth"):
        d.verify("https://api.example.com", TOKEN,
                 request=lambda url, **kw: (200 if url.endswith('/health') else status, {}))


def test_curl_is_bounded_and_token_is_never_in_argv(monkeypatch):
    d = helper()
    def run(argv, **kwargs):
        assert "--connect-timeout" in argv and "--max-time" in argv
        assert TOKEN not in str(argv)
        assert TOKEN in kwargs["input"]
        assert kwargs["timeout"] == 915
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ready": True}) + "\n200", "")
    monkeypatch.setattr(d.subprocess, "run", run)
    assert d.curl("https://api.example.com/admin/verify-models", token=TOKEN,
                  method="POST", timeout=900) == (200, {"ready": True})


def test_health_retry_is_bounded(monkeypatch):
    d = helper()
    calls = []
    monkeypatch.setattr(d.time, "sleep", lambda seconds: None)
    def unavailable(*args, **kwargs):
        calls.append(kwargs)
        return 503, {}
    with pytest.raises(RuntimeError, match="health"):
        d.verify("https://api.example.com", TOKEN, request=unavailable)
    assert len(calls) == 12
    assert all(c["timeout"] == 20 for c in calls)
