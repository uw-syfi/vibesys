"""What the light host commands may not import: shared by the static and real guards.

``tests/architecture/test_light_entrypoints.py`` checks the static import
closure of each light entrypoint; ``tests/e2e/test_light_host_commands.py``
checks what a real process loads. Both name the heavy layers from this one list.
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
    "server.instances",
)
"""Modules (and everything under them) that load the run engine or a transport."""


def is_heavy(name: str) -> bool:
    """Whether module ``name`` is, or is under, one of ``DENIED_HEAVY``."""
    return any(name == denied or name.startswith(f"{denied}.") for denied in DENIED_HEAVY)
