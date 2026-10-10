"""The native CPU profiler mechanism, importable with nothing but the standard library.

The linux-cpu profiler's MCP server runs in the agent container on the
container's own interpreter, which holds only the standard library, ``pydantic``
and ``mcp``, with the framework's source roots on its path. ``vs_runtime.api``
imports the whole run runtime and its third-party dependencies, so the server
reaches its mechanism here instead. This module and the private modules it
re-exports must import nothing outside the standard library (a test pins that).
"""

from __future__ import annotations

from vs_runtime._linux_cpu_profiler import collect as collect_linux_profile
from vs_runtime._linux_cpu_profiler import detect_capability as detect_linux_profiler
from vs_runtime._linux_cpu_profiler import parse_command as parse_profile_command
from vs_runtime._linux_cpu_profiler import summarize as summarize_linux_profile

__all__ = [
    "collect_linux_profile",
    "detect_linux_profiler",
    "parse_profile_command",
    "summarize_linux_profile",
]
