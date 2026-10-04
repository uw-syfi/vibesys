"""Injected run assembly for application wiring.

This transitional seam executes a caller-selected catalog and implementations;
no built-in catalog or tool selection occurs here.
"""

from vibesys.api._session import _create_session as create_session
from vibesys.api.assembly import SessionAgents, SessionImplementations
from vibesys.composition import AgentToolContext
from vibesys.orchestration.skill_selection import platform_skill_selection
from vibesys.run.integration import RunResources

__all__ = [
    "AgentToolContext",
    "RunResources",
    "SessionAgents",
    "SessionImplementations",
    "create_session",
    "platform_skill_selection",
]
