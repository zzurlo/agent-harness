"""The agent loop.

    model -> parse tool calls -> execute -> feed results back -> repeat

Everything around that inner cycle is what makes this a *harness* rather than
an API wrapper: route selection, context compaction, budgets, approval gates,
structured error recovery, and cost accounting.

Emits typed events so the transport layer (SSE) stays dumb.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .config import ROUTES, settings
from .context import COMPACTION_PROMPT, Conversation, render_for_summary
from .providers import foundry
from .router import select_route
from .tools.registry import registry

log = logging.getLogger(__name__)


@dataclass
class TurnStats:
    route: str = ""
    deployment: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_calls: int = 0
    compactions: int = 0
    cost_usd: float = 0.0
    elapsed: float = 0.0

    def as_dict(self) -> dict:
        return {
            "route": self.route,
            "deployment": self.deployment,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "tool_calls": self.tool_calls,
            "compactions": self.compactions,
            "cost_usd": round(self.cost_usd, 6),
            "elapsed": round(self.elapsed, 2),
        }


def _event(kind: str, **data: Any) -> dict:
    return {"type": kind, **data}


class ApprovalBroker:
    """Bridges the loop and the UI for dangerous-tool approval gates."""

    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Future[bool]] = {}

    def create(self, call_id: str) -> asyncio.Future[bool]:
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._waiters[call_id] = fut
        return fut

    def resolve(self, call_id: str, approved: bool) -> bool:
        fut = self._waiters.pop(call_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(approved)
        return True


approvals = ApprovalBroker()


class Harness:
    def __init__(self, conversation: Conversation | None = None) -> None:
        self.conv = conversation or Conversation()

    # -- compaction ------------------------------------------------------
    async def maybe_compact(self) -> bool:
        if not self.conv.needs_compaction():
            return False
        to_summarize, kept = self.conv.split_for_compaction()
        if not to_summarize:
            return False

        fast = ROUTES["fast"]
        try:
            summary = await foundry.complete(
                model=fast.deployment,
                messages=[
                    {"role": "system", "content": COMPACTION_PROMPT},
                    {"role": "user", "content": render_for_summary(to_summarize)},
                ],
                max_output_tokens=600,
            )
        except Exception as exc:  # noqa: BLE001
            # Never fail a turn because compaction failed; drop oldest instead.
            log.warning("compaction failed (%s); truncating instead", exc)
            self.conv.messages = kept
            return False

        self.conv.apply_compaction(summary, kept)
        return True

    # -- main loop -------------------------------------------------------
    async def run(
        self,
        user_message: str,
        *,
        forced_route: str | None = None,
        enable_tools: bool = True,
    ) -> AsyncIterator[dict]:
        started = time.perf_counter()
        budgets = settings.budgets
        stats = TurnStats()

        if await self.maybe_compact():
            stats.compactions += 1
            yield _event("compacted", messages=len(self.conv.messages))

        route_name, route = select_route(
            user_message,
            tools_available=enable_tools and len(registry) > 0,
            forced=forced_route,
            history_tokens=self.conv.token_count,
        )
        stats.route, stats.deployment = route_name, route.deployment
        yield _event("route", route=route_name, deployment=route.deployment)

        self.conv.add("user", user_message)

        # Only advertise tools to models that can actually call them.
        tool_schemas = (
            registry.schemas() if (enable_tools and route.supports_tools) else None
        )

        for step in range(budgets.max_tool_calls_per_turn + 1):
            if time.perf_counter() - started > budgets.max_wall_clock_seconds:
                yield _event("budget_exceeded", reason="wall_clock")
                break
            if stats.prompt_tokens + stats.completion_tokens > budgets.max_total_tokens_per_turn:
                yield _event("budget_exceeded", reason="tokens")
                break

            text_parts: list[str] = []
            pending: dict[int, dict] = {}

            try:
                async for chunk in foundry.stream_completion(
                    model=route.deployment,
                    messages=self.conv.to_payload(),
                    tools=tool_schemas,
                    max_output_tokens=route.max_output_tokens,
                ):
                    if getattr(chunk, "usage", None):
                        stats.prompt_tokens += chunk.usage.prompt_tokens or 0
                        stats.completion_tokens += chunk.usage.completion_tokens or 0

                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    if getattr(delta, "content", None):
                        text_parts.append(delta.content)
                        yield _event("token", text=delta.content)

                    for tc in getattr(delta, "tool_calls", None) or []:
                        slot = pending.setdefault(
                            tc.index,
                            {"id": "", "type": "function",
                             "function": {"name": "", "arguments": ""}},
                        )
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:
                            slot["function"]["name"] += tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["function"]["arguments"] += tc.function.arguments
            except Exception as exc:  # noqa: BLE001
                log.exception("model call failed")
                yield _event("error", message=f"Model call failed: {exc}")
                break

            assistant_text = "".join(text_parts)
            calls = [pending[i] for i in sorted(pending)]

            if not calls:
                self.conv.add("assistant", assistant_text)
                break

            self.conv.add("assistant", assistant_text or None, tool_calls=calls)

            for call in calls:
                name = call["function"]["name"]
                raw_args = call["function"]["arguments"] or "{}"
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError as exc:
                    self._add_tool_result(
                        call["id"],
                        {"error": f"Arguments were not valid JSON: {exc}",
                         "retryable": True},
                    )
                    continue

                tool = registry.get(name)
                if tool is not None and tool.dangerous:
                    yield _event(
                        "approval_required",
                        call_id=call["id"], tool=name, arguments=args,
                    )
                    fut = approvals.create(call["id"])
                    try:
                        approved = await asyncio.wait_for(fut, timeout=120)
                    except asyncio.TimeoutError:
                        approved = False
                    if not approved:
                        self._add_tool_result(
                            call["id"],
                            {"error": "User denied execution of this tool.",
                             "retryable": False},
                        )
                        yield _event("tool_denied", tool=name)
                        continue

                yield _event("tool_start", tool=name, arguments=args)
                result = await registry.dispatch(
                    name, args, timeout=budgets.max_tool_seconds
                )
                stats.tool_calls += 1
                self._add_tool_result(call["id"], result)
                yield _event("tool_end", tool=name, ok=bool(result.get("ok")))
        else:
            yield _event("budget_exceeded", reason="max_tool_calls")

        stats.elapsed = time.perf_counter() - started
        stats.cost_usd = route.cost(stats.prompt_tokens, stats.completion_tokens)
        yield _event("done", stats=stats.as_dict())

    def _add_tool_result(self, call_id: str, payload: dict) -> None:
        self.conv.add("tool", json.dumps(payload), tool_call_id=call_id)


async def generate_title(first_message: str) -> str:
    """Cheap-route conversation title. Never worth a big model."""
    fast = ROUTES["fast"]
    try:
        title = await foundry.complete(
            model=fast.deployment,
            messages=[
                {"role": "system",
                 "content": "Write a 3-6 word title for this conversation. "
                            "Output only the title, no quotes."},
                {"role": "user", "content": first_message[:1000]},
            ],
            max_output_tokens=24,
        )
        return title.strip().strip('"')[:80] or "New conversation"
    except Exception:  # noqa: BLE001
        return "New conversation"
