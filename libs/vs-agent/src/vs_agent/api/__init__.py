"""The public API of ``vs_agent``. Import from here, not from submodules.

Eager exports are light: value types, contracts, agent identity/selection,
progress and event-sink value types, provider/session policy, and the generic
subprocess-hosted MCP tool bridge. The agent execution composition
(``AgentClient``, ``build_agent_client``, ``agent_driver_supports_mcp_servers``)
is exposed lazily via module ``__getattr__`` so importing :mod:`vs_agent.api`
never pulls in ``agentshim`` or ``omnigent``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vs_agent.base import ResponseFallback
from vs_agent.catalog import DriverInfo, agent_catalog
from vs_agent.cli_docker import (
    DOCKER_PROVIDER_ENV,
    auth_bind_mounts,
    auth_copy_paths,
    auth_env_passthrough,
    auth_env_vars,
    auth_paths,
)
from vs_agent.contracts import (
    AgentCapabilities,
    AgentClientProtocol,
    AgentEvent,
    AgentEventKind,
    AgentExecutionPolicy,
    AgentUsage,
    MCPServerSpec,
)
from vs_agent.events import (
    AgentOutputChannel,
    AgentStatusData,
    CommandResultPayload,
    JsonResultPayload,
    TodoItemData,
    ToolResultPayload,
)
from vs_agent.host_resource_declarations import task_agent_host_resources
from vs_agent.mcp_server import register_tool, serve_stdio
from vs_agent.progress import AgentProgress, CandidateProgress, RoundProgress
from vs_agent.provider_policy import (
    CLI_VERSIONS,
    DEFAULT_CLI_PROVIDER,
    GO_TOOLCHAIN_VERSION,
    NODE_VERSION,
    RUST_TOOLCHAIN_VERSION,
    SHIPPED_PROVIDERS,
    cli_skill_dirs,
)
from vs_agent.selection import AgentSelection
from vs_agent.session_key import AgentSessionKey, SessionScope
from vs_agent.session_store import (
    AgentSessionState,
    DurableSessionStore,
    NullSessionStore,
    SessionStore,
)
from vs_agent.sink import NULL_AGENT_EVENT_SINK, AgentEventSink, NullAgentEventSink
from vs_agent.skills import NULL_SKILL_SELECTION, SkillSelection
from vs_agent.spec import AgentBackend, AgentSpec, Driver
from vs_agent.todos import todos_from_tool_call
from vs_agent.tools import StdioServerDescriptor, ToolSpec, expose_as_tools

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path
    from typing import TextIO

    from vs_agent.client import AgentClient
    from vs_agent.factory import agent_driver_supports_mcp_servers
    from vs_agent.fake_response import AgentResponseScenario
    from vs_sandbox.api import HostResource, ProjectPathPolicy

__all__ = [
    "CLI_VERSIONS",
    "DEFAULT_CLI_PROVIDER",
    "DOCKER_PROVIDER_ENV",
    "GO_TOOLCHAIN_VERSION",
    "NODE_VERSION",
    "NULL_AGENT_EVENT_SINK",
    "NULL_SKILL_SELECTION",
    "RUST_TOOLCHAIN_VERSION",
    "SHIPPED_PROVIDERS",
    "AgentBackend",
    "AgentCapabilities",
    "AgentClient",
    "AgentClientProtocol",
    "AgentEvent",
    "AgentEventKind",
    "AgentEventSink",
    "AgentExecutionPolicy",
    "AgentOutputChannel",
    "AgentProgress",
    "AgentSelection",
    "AgentSessionKey",
    "AgentSessionState",
    "AgentSpec",
    "AgentStatusData",
    "AgentUsage",
    "CandidateProgress",
    "CommandResultPayload",
    "Driver",
    "DriverInfo",
    "DurableSessionStore",
    "JsonResultPayload",
    "MCPServerSpec",
    "NullAgentEventSink",
    "NullSessionStore",
    "ResponseFallback",
    "RoundProgress",
    "SessionScope",
    "SessionStore",
    "SkillSelection",
    "StdioServerDescriptor",
    "TodoItemData",
    "ToolResultPayload",
    "ToolSpec",
    "agent_catalog",
    "agent_driver_supports_mcp_servers",
    "auth_bind_mounts",
    "auth_copy_paths",
    "auth_env_passthrough",
    "auth_env_vars",
    "auth_paths",
    "build_agent_client",
    "cli_skill_dirs",
    "expose_as_tools",
    "register_tool",
    "serve_stdio",
    "task_agent_host_resources",
    "todos_from_tool_call",
]


def __getattr__(name: str) -> object:
    """Expose the heavy composition lazily so imports cannot form a cycle."""
    if name == "AgentClient":
        from vs_agent.client import (  # noqa: PLC0415  # lint-waiver: LW-010114 [PLC0415]; Keep AgentClient lazy in __getattr__ so unused providers and import cycles stay unloaded.
            AgentClient,
        )

        return AgentClient
    if name == "agent_driver_supports_mcp_servers":
        from vs_agent.factory import (  # noqa: PLC0415  # lint-waiver: LW-010115 [PLC0415]; Keep this dependency lazy in __getattr__ so unused providers and import cycles stay unloaded.
            agent_driver_supports_mcp_servers,
        )

        return agent_driver_supports_mcp_servers
    message = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(message)


def build_agent_client(  # noqa: PLR0913  # lint-waiver: LW-011100 [PLR0913]; preserve the public keyword parameters shared with the factory so callers can configure each agent setting directly.
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
    response_scenario: AgentResponseScenario | None = None,
) -> AgentClientProtocol:
    """Build an agent service through the application composition module.

    ``response_scenario`` supplies deterministic structured responses to the
    stub backend. Other backends execute their configured agent driver.
    """
    from vs_agent.factory import (  # noqa: PLC0415  # lint-waiver: LW-010116 [PLC0415]; Keep build_agent_client as build lazy in build_agent_client so unused providers and import cycles stay unloaded.
        build_agent_client as build,
    )

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
        response_scenario=response_scenario,
    )
