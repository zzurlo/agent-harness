#!/usr/bin/env python3
"""Verify that every configured model route is actually usable.

Catches the failure modes that otherwise only surface at runtime, in prod,
mid-conversation:

  1. The deployment name in MODEL_* does not exist in your Foundry project
     (typo, wrong region, never deployed).
  2. The model does not support function calling, but the route is flagged
     supports_tools=True. The model silently ignores tool schemas and your
     agent loop quietly never calls a tool.
  3. Credentials or endpoint are wrong.
  4. The model errors on a trivial request (quota, content filter, capacity).

Exit codes
----------
0  every route usable
1  at least one hard failure
2  configuration/credential problem (could not test anything)

Usage
-----
    python scripts/verify_models.py                 # test every route
    python scripts/verify_models.py --route tools   # just one
    python scripts/verify_models.py --json          # machine-readable
    python scripts/verify_models.py --skip-tools    # no tool-calling probe
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Make the harness importable when run as scripts/verify_models.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from harness.config import ROUTES, settings
    from harness.providers.foundry import get_client
except ImportError as exc:  # pragma: no cover
    print(f"Could not import the harness package: {exc}", file=sys.stderr)
    print("Run from backend/ with dependencies installed:", file=sys.stderr)
    print("    cd backend && pip install -e '.[dev]'", file=sys.stderr)
    sys.exit(2)


GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)
if not sys.stdout.isatty() or os.getenv("NO_COLOR"):
    GREEN = RED = YELLOW = DIM = BOLD = RESET = ""

OK, FAIL, WARN = f"{GREEN}PASS{RESET}", f"{RED}FAIL{RESET}", f"{YELLOW}WARN{RESET}"

# A probe the model can only answer correctly by emitting a tool call.
PROBE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
}


@dataclass
class RouteResult:
    route: str
    deployment: str
    expects_tools: bool
    reachable: bool = False
    tools_work: bool | None = None
    latency_ms: int | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.errors)

    def as_dict(self) -> dict:
        return {
            "route": self.route,
            "deployment": self.deployment,
            "reachable": self.reachable,
            "expects_tools": self.expects_tools,
            "tools_work": self.tools_work,
            "latency_ms": self.latency_ms,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def _explain(exc: Exception) -> str:
    """Turn an SDK exception into something actionable."""
    text = str(exc)
    status = getattr(exc, "status_code", None)

    if status == 404 or "DeploymentNotFound" in text or "does not exist" in text:
        return (
            "deployment not found -- check the name in Foundry portal under "
            "Models + Endpoints, and that it is in this project/region"
        )
    if status == 401 or status == 403:
        return (
            "auth rejected -- managed identity needs the Foundry User (formerly "
            "Azure AI User) role on the project, or set FOUNDRY_API_KEY locally"
        )
    if status == 429:
        return "rate limited / no quota -- raise the deployment TPM or retry"
    if "content_filter" in text or "ResponsibleAI" in text:
        return "blocked by content filter on a trivial prompt -- check filter config"
    if status and status >= 500:
        return f"upstream error {status} -- transient, retry"
    return text[:300]


async def probe_route(name: str, timeout: float, skip_tools: bool) -> RouteResult:
    route = ROUTES[name]
    res = RouteResult(route=name, deployment=route.deployment,
                      expects_tools=route.supports_tools)
    client = get_client()

    # --- 1. reachability -------------------------------------------------
    loop = asyncio.get_running_loop()
    start = loop.time()
    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=route.deployment,
                messages=[{"role": "user", "content": "Reply with the single word: ok"}],
                max_tokens=16,
                temperature=0,
            ),
            timeout=timeout,
        )
        res.reachable = True
        res.latency_ms = int((loop.time() - start) * 1000)

        content = (resp.choices[0].message.content or "").strip()
        if not content:
            res.warnings.append(
                "returned empty content -- reasoning models sometimes spend the "
                "whole budget on thinking tokens; consider a higher max_output_tokens"
            )
    except TimeoutError:
        res.errors.append(f"no response within {timeout:.0f}s")
        return res
    except Exception as exc:  # noqa: BLE001
        res.errors.append(_explain(exc))
        return res

    # --- 2. tool calling -------------------------------------------------
    # Only meaningful for routes the harness will actually hand tools to.
    if skip_tools or not route.supports_tools:
        return res

    try:
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=route.deployment,
                messages=[
                    {"role": "user",
                     "content": "What is the weather in Chicago? Use the tool."}
                ],
                tools=[PROBE_TOOL],
                tool_choice="auto",
                max_tokens=256,
                temperature=0,
            ),
            timeout=timeout,
        )
        calls = resp.choices[0].message.tool_calls or []
        res.tools_work = bool(calls)

        if not calls:
            res.errors.append(
                "accepted the tool schema but emitted no tool call -- this model "
                "will silently never use tools. Route it elsewhere or set "
                "supports_tools=False."
            )
        else:
            fn = calls[0].function
            if fn.name != "get_weather":
                res.warnings.append(f"called unexpected tool '{fn.name}'")
            try:
                args = json.loads(fn.arguments or "{}")
                if "city" not in args:
                    res.warnings.append(
                        f"tool call omitted the required 'city' arg: {fn.arguments!r}"
                    )
            except json.JSONDecodeError:
                res.errors.append(
                    f"emitted malformed JSON arguments: {fn.arguments!r}"
                )
    except Exception as exc:  # noqa: BLE001
        res.tools_work = False
        msg = _explain(exc)
        if "tool" in msg.lower() or "function" in msg.lower():
            res.errors.append(f"rejected the tool schema: {msg}")
        else:
            res.errors.append(f"tool probe failed: {msg}")

    return res


def render(results: list[RouteResult]) -> None:
    print(f"\n{BOLD}Foundry model verification{RESET}")
    print(f"{DIM}endpoint: {settings.project_endpoint or '(unset)'}{RESET}")
    auth = "API key" if settings.api_key else "Entra ID (DefaultAzureCredential)"
    print(f"{DIM}auth:     {auth}{RESET}\n")

    width = max(len(r.route) for r in results) + 2
    for r in results:
        status = FAIL if r.failed else (WARN if r.warnings else OK)
        latency = f"{r.latency_ms}ms" if r.latency_ms is not None else "-"

        tools = ""
        if r.tools_work is True:
            tools = f"{GREEN}tools ok{RESET}"
        elif r.tools_work is False:
            tools = f"{RED}tools broken{RESET}"
        elif r.expects_tools:
            tools = f"{DIM}tools skipped{RESET}"

        print(f"  {status}  {r.route:<{width}} {r.deployment:<34} "
              f"{latency:>8}  {tools}")
        for err in r.errors:
            print(f"        {RED}->{RESET} {err}")
        for warn in r.warnings:
            print(f"        {YELLOW}->{RESET} {warn}")

    failed = [r for r in results if r.failed]
    print()
    if failed:
        print(f"{RED}{len(failed)} of {len(results)} routes unusable.{RESET}")
        print(f"{DIM}Fix the MODEL_* env vars or deploy the missing models, "
              f"then re-run.{RESET}\n")
    else:
        warned = sum(1 for r in results if r.warnings)
        extra = f" ({warned} with warnings)" if warned else ""
        print(f"{GREEN}All {len(results)} routes usable{extra}.{RESET}\n")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", action="append", choices=sorted(ROUTES),
                        help="Only check this route (repeatable).")
    parser.add_argument("--timeout", type=float, default=60.0,
                        help="Per-request timeout in seconds (default: 60).")
    parser.add_argument("--skip-tools", action="store_true",
                        help="Skip the tool-calling probe.")
    parser.add_argument("--json", action="store_true",
                        help="Emit JSON instead of a table.")
    args = parser.parse_args()

    if not settings.project_endpoint:
        print("FOUNDRY_PROJECT_ENDPOINT is not set.", file=sys.stderr)
        print("  export FOUNDRY_PROJECT_ENDPOINT="
              "https://<res>.services.ai.azure.com/api/projects/<proj>",
              file=sys.stderr)
        return 2

    names = args.route or list(ROUTES)

    # Sequential on purpose: parallel probes against a fresh deployment tend to
    # trip rate limits and produce misleading 429 failures.
    results = []
    for name in names:
        results.append(await probe_route(name, args.timeout, args.skip_tools))

    if args.json:
        print(json.dumps([r.as_dict() for r in results], indent=2))
    else:
        render(results)

    return 1 if any(r.failed for r in results) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
