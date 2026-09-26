"""Context management: token accounting and compaction.

Compaction is the single highest-leverage feature in a chat harness. When the
conversation crosses a ratio of the working window, the oldest messages are
summarized by the cheap ``fast`` route into one synthetic message, and the
recent tail is kept verbatim.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .config import ContextPolicy, settings

log = logging.getLogger(__name__)

try:  # tiktoken is optional; fall back to a heuristic if unavailable.
    import tiktoken

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # noqa: BLE001
    _ENC = None


def count_tokens(text: str) -> int:
    """Best-effort token count. Heuristic fallback is ~4 chars/token."""
    if not text:
        return 0
    if _ENC is not None:
        return len(_ENC.encode(text))
    return max(1, len(text) // 4)


def count_message_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, list):  # multimodal content blocks
            content = " ".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        total += count_tokens(str(content)) + 4  # per-message overhead
        for call in m.get("tool_calls") or []:
            total += count_tokens(str(call))
    return total


@dataclass
class Conversation:
    """Message history for one thread, with compaction built in."""

    system_prompt: str = field(default_factory=lambda: settings.system_prompt)
    messages: list[dict[str, Any]] = field(default_factory=list)
    policy: ContextPolicy = field(default_factory=lambda: settings.context)
    compactions: int = 0

    def add(self, role: str, content: Any, **extra: Any) -> None:
        msg: dict[str, Any] = {"role": role, "content": content}
        msg.update(extra)
        self.messages.append(msg)

    def to_payload(self) -> list[dict[str, Any]]:
        """Full message list including the system prompt.

        The system prompt is kept first and stable so prompt caching can hit.
        """
        return [{"role": "system", "content": self.system_prompt}, *self.messages]

    @property
    def token_count(self) -> int:
        return count_message_tokens(self.to_payload())

    def needs_compaction(self) -> bool:
        threshold = self.policy.working_window_tokens * self.policy.compact_at_ratio
        return self.token_count > threshold

    def split_for_compaction(self) -> tuple[list[dict], list[dict]]:
        """Return (to_summarize, to_keep).

        Never splits in the middle of an assistant tool_calls -> tool results
        group, which would leave orphaned tool messages the API rejects.
        """
        keep_n = self.policy.keep_recent_messages
        if len(self.messages) <= keep_n:
            return [], self.messages

        cut = len(self.messages) - keep_n
        # Walk the cut backwards past any tool messages and their parent call.
        while cut > 0 and self.messages[cut].get("role") == "tool":
            cut -= 1
        if cut > 0 and self.messages[cut - 1].get("tool_calls"):
            cut -= 1

        if cut <= 0:
            return [], self.messages
        return self.messages[:cut], self.messages[cut:]

    def apply_compaction(self, summary: str, kept: list[dict]) -> None:
        self.messages = [
            {
                "role": "assistant",
                "content": (
                    "[Summary of earlier conversation -- "
                    f"compaction #{self.compactions + 1}]\n{summary}"
                ),
            },
            *kept,
        ]
        self.compactions += 1
        log.info(
            "compacted conversation: now %d messages / %d tokens",
            len(self.messages),
            self.token_count,
        )


COMPACTION_PROMPT = (
    "Summarize the conversation below. Preserve: the user's goals and "
    "constraints, decisions made, facts established, and any unresolved "
    "questions. Drop pleasantries and redundant detail. Write in third person, "
    "under 400 words. Output only the summary."
)


def render_for_summary(messages: list[dict[str, Any]]) -> str:
    lines = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content") or ""
        if m.get("tool_calls"):
            names = [
                c.get("function", {}).get("name", "?") for c in m["tool_calls"]
            ]
            content = f"{content} [called tools: {', '.join(names)}]"
        lines.append(f"{role.upper()}: {content}")
    return "\n".join(lines)
