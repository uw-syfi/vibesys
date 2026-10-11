"""What the light host commands may not import: shared by the static and real guards.

``tests/architecture/test_light_entrypoints.py`` checks the static import
closure of each light entrypoint; ``tests/e2e/test_light_host_commands.py``
checks what a real process loads (as does ``tests/e2e/test_host_helper.py``).
They name the heavy layers from this one list.
"""

from __future__ import annotations

DENIED_HEAVY = (
    "vibesys",
    "vs_runtime",
    "vs_agent",
    "agentshim",
    "mcp",
    "websockets",
    "server.api",
    "server.events",
    "server.chat",
    "server.runtime",
    "server.transport",
)
"""Modules (and everything under them) that load the run engine or a transport."""


def is_heavy(name: str) -> bool:
    """Whether module ``name`` is, or is under, one of ``DENIED_HEAVY``."""
    return any(name == denied or name.startswith(f"{denied}.") for denied in DENIED_HEAVY)


BUDGET_MS = 300
"""The start-up budget for a light host command on a development machine."""


def loaded_modules(importtime_stderr: str) -> list[str]:
    """Module names from ``-X importtime`` lines (``import time: self | cumulative | name``)."""
    return [
        line.rsplit("|", 1)[1].strip()
        for line in importtime_stderr.splitlines()
        if line.startswith("import time:") and "|" in line and "imported package" not in line
    ]
