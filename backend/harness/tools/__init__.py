"""Tool registry and built-in tools.

Importing this package registers the built-in tools, so the harness is usable
without going through the API layer (tests, scripts, alternate transports).
"""
from . import builtin  # noqa: E402,F401  -- import for registration side effect
from .registry import Tool, ToolRegistry, registry

__all__ = ["Tool", "ToolRegistry", "registry"]
