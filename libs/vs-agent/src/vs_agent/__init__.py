"""Public API of the ``vs_agent`` library.

Eager exports are light: value types, agent identity/selection, progress and
event-sink value types, and the generic subprocess-hosted MCP tool bridge
(:mod:`vs_agent.tools`, :mod:`vs_agent.mcp_server`). The agent execution
contracts and composition (``AgentClient``, ``build_agent_client``) are exposed
lazily so importing :mod:`vs_agent` never pulls in ``agentshim``/``omnigent``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vs_agent.base import ResponseFallback
from vs_agent.mcp_server import register_tool, serve_stdio
from vs_agent.progress import AgentProgress, CandidateProgress, RoundProgress
from vs_agent.selection import AgentSelection
from vs_agent.session_key import AgentSessionKey, SessionScope
from vs_agent.sink import NULL_AGENT_EVENT_SINK
from vs_agent.skills import NULL_SKILL_SELECTION
from vs_agent.tools import StdioServerDescriptor, ToolSpec, expose_as_tools

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path
    from typing import TextIO

    from vs_agent.client import AgentClient
    from vs_agent.contracts import AgentClientProtocol
    from vs_agent.session_store import SessionStore
    from vs_agent.sink import AgentEventSink
    from vs_agent.skills import SkillSelection
    from vs_agent.spec import AgentSpec
    from vs_sandbox import HostResource, ProjectPathPolicy

__all__ = [
    "AgentClient",
    "AgentClientProtocol",
    "AgentProgress",
    "AgentSelection",
    "AgentSessionKey",
    "CandidateProgress",
    "ResponseFallback",
    "RoundProgress",
    "SessionScope",
    "StdioServerDescriptor",
    "ToolSpec",
    "build_agent_client",
    "expose_as_tools",
    "register_tool",
    "serve_stdio",
]


def __getattr__(name: str) -> Any:  # noqa: ANN401
    """Expose the heavy composition lazily so imports cannot form a cycle."""
    if name == "AgentClient":
        from vs_agent.client import AgentClient  # noqa: PLC0415

        return AgentClient
    if name == "AgentClientProtocol":
        from vs_agent.contracts import AgentClientProtocol  # noqa: PLC0415

        return AgentClientProtocol
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")  # noqa: TRY003


def build_agent_client(  # noqa: PLR0913
    *,
    spec: AgentSpec,
    backends: dict[str, Any] | None,
    skill_source_dirs: list[Path],
    skill_selection: SkillSelection = NULL_SKILL_SELECTION,
    run_log_file: TextIO | None,
    use_docker: bool,
    log_dir: Path | None = None,
    host_resources: Iterable[HostResource] = (),
    project_path_policy: ProjectPathPolicy | None = None,
    require_host_sandbox: bool = False,
    session_store: SessionStore | None = None,
    events: AgentEventSink = NULL_AGENT_EVENT_SINK,
) -> AgentClientProtocol:
    """Build an agent service through the application composition module."""
    from vs_agent.factory import build_agent_client as build  # noqa: PLC0415

    return build(
        spec=spec,
        backends=backends,
        skill_source_dirs=skill_source_dirs,
        skill_selection=skill_selection,
        run_log_file=run_log_file,
        use_docker=use_docker,
        log_dir=log_dir,
        host_resources=host_resources,
        project_path_policy=project_path_policy,
        require_host_sandbox=require_host_sandbox,
        session_store=session_store,
        events=events,
    )
