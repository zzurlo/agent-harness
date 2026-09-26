"""Tool registry: decorator-based registration with JSON-schema generation.

Design notes
------------
* Tool failures are returned to the model as structured, *recoverable* payloads
  rather than raised. A model that sees {"error": ..., "retryable": true} can
  adapt; a model that sees a stack trace usually flails.
* Tools may be marked ``dangerous=True``, which pauses the loop and emits an
  approval event to the UI before execution.
"""
from __future__ import annotations

import asyncio
import inspect
import time
import typing as t
from dataclasses import dataclass

_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: t.Callable
    dangerous: bool = False

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, *, dangerous: bool = False, name: str | None = None):
        """Decorator. Infers the JSON schema from type hints + docstring."""

        def deco(fn: t.Callable) -> t.Callable:
            tool_name = name or fn.__name__
            self._tools[tool_name] = Tool(
                name=tool_name,
                description=inspect.getdoc(fn) or "",
                parameters=_build_schema(fn),
                fn=fn,
                dangerous=dangerous,
            )
            return fn

        return deco

    def schemas(self) -> list[dict]:
        return [tool.schema for tool in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __len__(self) -> int:
        return len(self._tools)

    async def dispatch(self, name: str, args: dict, *, timeout: float) -> dict:
        """Execute a tool. Never raises -- always returns a model-readable dict."""
        tool = self.get(name)
        if tool is None:
            return {
                "error": f"Unknown tool '{name}'. Available: {list(self._tools)}",
                "retryable": False,
            }

        started = time.perf_counter()
        try:
            if inspect.iscoroutinefunction(tool.fn):
                result = await asyncio.wait_for(tool.fn(**args), timeout=timeout)
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(tool.fn, **args), timeout=timeout
                )
            return {"ok": True, "result": result, "elapsed": round(time.perf_counter() - started, 3)}
        except TimeoutError:
            return {
                "error": f"Tool '{name}' exceeded {timeout}s and was cancelled.",
                "retryable": True,
            }
        except TypeError as exc:
            # Almost always bad arguments from the model -- it can fix these.
            return {"error": f"Invalid arguments for '{name}': {exc}", "retryable": True}
        except Exception as exc:  # noqa: BLE001 - deliberately broad
            return {
                "error": f"{type(exc).__name__}: {exc}",
                "retryable": True,
            }


def _build_schema(fn: t.Callable) -> dict:
    """Derive a JSON schema from a function signature."""
    sig = inspect.signature(fn)
    hints = t.get_type_hints(fn)
    props: dict[str, dict] = {}
    required: list[str] = []

    for pname, param in sig.parameters.items():
        if pname in {"self", "cls"}:
            continue
        hint = hints.get(pname, str)
        origin = t.get_origin(hint)

        if origin is t.Literal:
            props[pname] = {"type": "string", "enum": list(t.get_args(hint))}
        elif origin in (list, list):
            props[pname] = {"type": "array", "items": {"type": "string"}}
        else:
            # Unwrap Optional[X] -> X
            if origin is t.Union:
                non_none = [a for a in t.get_args(hint) if a is not type(None)]
                hint = non_none[0] if non_none else str
            props[pname] = {"type": _PY_TO_JSON.get(hint, "string")}

        if param.default is inspect.Parameter.empty:
            required.append(pname)

    return {"type": "object", "properties": props, "required": required}


registry = ToolRegistry()
