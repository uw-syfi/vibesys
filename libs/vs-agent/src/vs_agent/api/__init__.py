"""The public API of ``vs_agent``. Import from here, not from submodules.

Eager exports are light: value types, contracts, agent identity/selection,
progress and event-sink value types, provider/session policy, and generic
subprocess-hosted tools. The agent execution composition
(``AgentClient``, ``build_agent_client``, ``agent_driver_supports_tool_servers``)
is exposed lazily via module ``__getattr__`` so importing :mod:`vs_agent.api`
never pulls in ``agentshim`` or ``omnigent``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
    AgentObserver,
    AgentOutputSchemaError,
    AgentSessionSpec,
    AgentSpawnError,
    AgentTurnRequest,
    AgentTurnResult,
    AgentTurnTimeoutError,
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
from vs_agent.host_resource_declarations import (
    declare_provider_state_resources,
    task_agent_host_resources,
)
from vs_agent.mcp_server import register_tool, serve_stdio
from vs_agent.progress import AgentProgress, CandidateProgress, RoundProgress
from vs_agent.provider_policy import (
    CLI_VERSIONS,
    DEFAULT_CLI_PROVIDER,
    GO_TOOLCHAIN_VERSION,
    NODE_VERSION,
    RUST_TOOLCHAIN_VERSION,
    SHIPPED_PROVIDERS,
    cli_mcp_config_files,
    cli_skill_dirs,
)
from vs_agent.runner import describe_validation_error, parse_typed_response
from vs_agent.selection import AgentSelection
from vs_agent.session_environment import (
    BASE_ENV_ALLOWLIST,
    session_env_allowlist,
    session_environment,
    validate_env_names,
)
from vs_agent.session_errors import (
    InvocationConflictError,
    SessionConfigurationError,
    SessionPersistenceError,
    SessionResumeError,
)
from vs_agent.session_key import AgentSessionKey, SessionScope
from vs_agent.session_store import (
    AgentSessionState,
    DurableSessionStore,
    NullSessionStore,
    SessionStore,
)
from vs_agent.sessions import (
    AgentInvocationRecord,
    AgentInvocationState,
    AgentInvocationStore,
    AgentSessionCheckpoint,
    AgentSessions,
    AgentTurnExecutor,
    ClientAgentSessions,
    Completed,
    InvalidResponse,
    InvocationOutcome,
    Pending,
    Unknown,
    inspect_invocation_journal,
)
from vs_agent.sink import NULL_AGENT_EVENT_SINK, AgentEventSink, NullAgentEventSink
from vs_agent.skills import NULL_SKILL_SELECTION, SkillSelection
from vs_agent.spec import AgentBackend, AgentSpec, Driver
from vs_agent.todos import todos_from_tool_call
from vs_agent.tools import StdioServerDescriptor, ToolServerDescriptor, ToolSpec, expose_as_tools

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path
    from typing import TextIO

    from vs_agent.client import AgentClient
    from vs_agent.factory import agent_driver_supports_tool_servers
    from vs_sandbox.api import HostResource, ProjectPathPolicy

__all__ = [
    "BASE_ENV_ALLOWLIST",
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
    "AgentInvocationRecord",
    "AgentInvocationState",
    "AgentInvocationStore",
    "AgentObserver",
    "AgentOutputChannel",
    "AgentOutputSchemaError",
    "AgentProgress",
    "AgentSelection",
    "AgentSessionCheckpoint",
    "AgentSessionKey",
    "AgentSessionSpec",
    "AgentSessionState",
    "AgentSessions",
    "AgentSpawnError",
    "AgentSpec",
    "AgentStatusData",
    "AgentTurnExecutor",
    "AgentTurnRequest",
    "AgentTurnResult",
    "AgentTurnTimeoutError",
    "AgentUsage",
    "CandidateProgress",
    "ClientAgentSessions",
    "CommandResultPayload",
    "Completed",
    "Driver",
    "DriverInfo",
    "DurableSessionStore",
    "InvalidResponse",
    "InvocationConflictError",
    "InvocationOutcome",
    "JsonResultPayload",
    "MCPServerSpec",
    "NullAgentEventSink",
    "NullSessionStore",
    "Pending",
    "RoundProgress",
    "SessionConfigurationError",
    "SessionPersistenceError",
    "SessionResumeError",
    "SessionScope",
    "SessionStore",
    "SkillSelection",
    "StdioServerDescriptor",
    "TodoItemData",
    "ToolResultPayload",
    "ToolServerDescriptor",
    "ToolSpec",
    "Unknown",
    "agent_catalog",
    "agent_driver_supports_tool_servers",
    "auth_bind_mounts",
    "auth_copy_paths",
    "auth_env_passthrough",
    "auth_env_vars",
    "auth_paths",
    "build_agent_client",
    "cli_mcp_config_files",
    "cli_skill_dirs",
    "declare_provider_state_resources",
    "describe_validation_error",
    "expose_as_tools",
    "inspect_invocation_journal",
    "parse_typed_response",
    "register_tool",
    "serve_stdio",
    "session_env_allowlist",
    "session_environment",
    "task_agent_host_resources",
    "todos_from_tool_call",
    "validate_env_names",
]


def __getattr__(name: str) -> object:
    """Expose the heavy composition lazily so imports cannot form a cycle."""
    if name == "AgentClient":
        from vs_agent.client import (  # noqa: PLC0415  # lint-waiver: LW-010114 [PLC0415]; Keep AgentClient lazy in __getattr__ so unused providers and import cycles stay unloaded.
            AgentClient,
        )

        return AgentClient
    if name == "agent_driver_supports_tool_servers":
        from vs_agent.factory import (  # noqa: PLC0415  # lint-waiver: LW-010115 [PLC0415]; Keep this dependency lazy in __getattr__ so unused providers and import cycles stay unloaded.
            agent_driver_supports_tool_servers,
        )

        return agent_driver_supports_tool_servers
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
    agent_homes_dir: Path | None = None,
) -> AgentClientProtocol:
    """Build an agent service through the application composition module."""
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
        agent_homes_dir=agent_homes_dir,
    )
