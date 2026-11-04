"""Injected run assembly for application wiring.

This transitional seam executes a caller-selected catalog and implementations;
no built-in catalog or tool selection occurs here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.api._session import _create_session as create_session
from vibesys.api.assembly import (
    NativeProfilerPreflight,
    ProfilerPreflight,
    SessionAgents,
    SessionImplementations,
)
from vibesys.api.reader import open_run_reader
from vibesys.composition import AgentToolContext
from vibesys.orchestration.skill_selection import platform_skill_selection
from vibesys.run.evaluation_socket import prepare_agent_tool_socket
from vibesys.run.integration import AuxiliaryResources, RunResources
from vibesys.run.skill_sources import chat_skill_dirs

if TYPE_CHECKING:
    from pathlib import Path


def agent_tool_socket(resources: AuxiliaryResources, name: str) -> Path:
    """Where a host service named ``name`` listens for the run's agents' tool servers.

    The path is in the run's private socket directory, which every agent container mounts at
    the same path, so one path is valid on both sides. The directory exists when this returns.
    """
    return prepare_agent_tool_socket(resources.project.root, resources.run_id, name)


__all__ = [
    "AgentToolContext",
    "AuxiliaryResources",
    "NativeProfilerPreflight",
    "ProfilerPreflight",
    "RunResources",
    "SessionAgents",
    "SessionImplementations",
    "agent_tool_socket",
    "chat_skill_dirs",
    "create_session",
    "open_run_reader",
    "platform_skill_selection",
]
