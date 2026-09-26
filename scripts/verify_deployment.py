#!/usr/bin/env python3
"""Bounded health, authorization and managed-identity runtime model checks.

The protected server endpoint probes ALL configured routes sequentially. This
never obtains a Foundry token from the GitHub/Azure CLI deployment identity.
"""
import argparse
import json
import os
import subprocess
import time
from urllib.parse import urlsplit


def curl(url, *, method="GET", token=None, timeout=20):
    config = ""
    if token:
        config = "header = " + json.dumps("Authorization: Bearer " + token) + "\n"
    args = ["curl", "--silent", "--show-error", "--connect-timeout", "10",
            "--max-time", str(timeout), "--request", method, "--config", "-",
            "--write-out", "\n%{http_code}", url]
    try:
        result = subprocess.run(args, input=config, capture_output=True,
                                text=True, timeout=timeout + 15, check=False)
    except subprocess.TimeoutExpired:
        return 0, {}
    if result.returncode:
        return 0, {}
    body, status = result.stdout.rsplit("\n", 1)
    try:
        payload = json.loads(body)
    except ValueError:
        payload = {}
    return int(status), payload


def verify(base, token, *, model_timeout=180, request=curl):
    parsed = urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise ValueError("API URL must be a public HTTPS origin without credentials, path or query")
    if len(token) < 32 or any(c.isspace() for c in token):
        raise ValueError("APP_ACCESS_TOKEN must contain at least 32 non-whitespace characters")
    base = base.rstrip("/")
    for attempt in range(12):
        status, _ = request(base + "/health", timeout=20)
        if status == 200:
            break
        if attempt < 11:
            time.sleep(10)
    else:
        raise RuntimeError("health check failed after 12 bounded attempts")
    status, _ = request(base + "/threads", timeout=20)
    if status not in (401, 403):
        raise RuntimeError("auth gate failed: unauthenticated /threads must be denied")
    status, payload = request(base + "/admin/verify-models", method="POST", token=token,
                              timeout=model_timeout)
    if (status != 200 or not isinstance(payload, dict) or payload.get("ready") is not True
            or not isinstance(payload.get("results"), list) or not payload["results"]):
        raise RuntimeError("runtime model verification failed or timed out; deployment is NOT verified ready")
    print("Verified health, auth gate, and all configured runtime model routes.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--model-timeout", type=int, default=180,
                        help="Seconds for the server's bounded all-route probe (below the ingress request budget)")
    args = parser.parse_args()
    if not 1 <= args.model_timeout <= 230:
        parser.error("--model-timeout must be between 1 and 230 (ingress timeout safety)")
    verify(args.url, os.environ.get("APP_ACCESS_TOKEN", ""), model_timeout=args.model_timeout)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
