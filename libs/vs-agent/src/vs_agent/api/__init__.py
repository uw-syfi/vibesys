"""The public API of ``vs_agent``. Import from here, not from submodules.

Eager exports are light: value types, contracts, agent identity/selection,
progress and event-sink value types, provider/session policy, and generic
subprocess-hosted tools. The agent execution composition
(``AgentClient``, ``build_agent_client``, ``agent_supports_tool_servers``)
is exposed lazily via module ``__getattr__`` so importing :mod:`vs_agent.api`
never pulls in ``agentshim``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vs_agent.cli_docker import (
    DOCKER_PROVIDER_ENV,
    auth_bind_mounts,
    auth_copy_paths,
    auth_env_passthrough,
    auth_env_vars,
    auth_paths,
)
from vs_agent.container_python import (
    CONTAINER_FRAMEWORK_ROOT,
    CONTAINER_PYTHON,
    CONTAINER_PYTHON_HOME,
    CONTAINER_PYTHON_VERSION,
    CONTAINER_SERVER_PACKAGES,
    container_import_roots,
    containerize_server,
)
from vs_agent.context_budget import CONTEXT_RENEWAL_FRACTION, ContextUse
from vs_agent.contracts import (
    DEFAULT_TURN_TIMEOUT_S,
    AgentCapabilities,
    AgentClientProtocol,
    AgentContextOverflowError,
    AgentEvent,
    AgentEventKind,
    AgentExecutionPolicy,
    AgentModelUsage,
    AgentObserver,
    AgentOutputSchemaError,
    AgentQuotaError,
    AgentRateLimit,
    AgentSessionSpec,
    AgentSpawnError,
    AgentTurnRequest,
    AgentTurnResult,
    AgentTurnTimeoutError,
    AgentUsage,
    Attribution,
    AuthStatus,
    CapacityGate,
    CapacityGated,
    CapacityStops,
    CostBasis,
    MCPServerSpec,
    ProviderNotReadyError,
    ProviderReadiness,
    ProviderSwitch,
    ProviderSwitchReason,
    QuotaCondition,
    QuotaPlan,
    QuotaResumeReason,
    QuotaWaitCancelReason,
    ReadinessProbe,
    ReadinessProblem,
    SchemaViolation,
    SteerableAgentClient,
    SteerOutcome,
    UsageSpeed,
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
    declare_command_host_resources,
    declare_provider_state_resources,
    task_agent_host_resources,
)
from vs_agent.progress import AgentProgress, CandidateProgress, RoundProgress
from vs_agent.provider_policy import (
    ALLOWED_MODELS_SETTING,
    CLAUDE_CLI_MIN_FOR_GENERATION,
    CLI_VERSIONS,
    DEFAULT_CLI_PROVIDER,
    NODE_VERSION,
    SHIPPED_PROVIDERS,
    Harness,
    ModelNotAllowedError,
    UnvettedHarnessError,
    check_allowed_model,
    check_harness,
    cli_mcp_config_files,
    cli_skill_dirs,
    default_model,
    harnesses,
    vetted_models,
)
from vs_agent.provider_profiles import (
    LoginCommand,
    LoginCredential,
    LoginKind,
    LoginState,
    ModelDisplay,
    ModelGenerationFacts,
    ProviderLogin,
    launch_shell_environment,
    model_display,
    provider_binary,
    provider_display_name,
    provider_login,
    provider_login_command,
)
from vs_agent.provider_routing import ProviderRoutedExecutor
from vs_agent.reply_limits import (
    MAX_REPLY_BYTES,
    MAX_WHITESPACE_RUN,
    ReplyBreach,
    ReplyGuard,
    reply_breach,
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
    AgentExecutionCancelledError,
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
from vs_agent.shim_clock import STOP_POLL_S, ThreadsClock
from vs_agent.sink import (
    NULL_AGENT_EVENT_SINK,
    AgentEventSink,
    NullAgentEventSink,
)
from vs_agent.skills import NULL_SKILL_SELECTION, SkillSelection
from vs_agent.spec import AgentBackend, AgentSpec
from vs_agent.todos import todos_from_tool_call
from vs_agent.tool_server_check import CHECK_TIMEOUT_S, ToolServerFailure, check_tool_servers

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path
    from typing import TextIO

    from agentshim import CommandExecutor

    from vs_agent.cli_common import materialize_skills
    from vs_agent.client import AgentClient
    from vs_agent.factory import agent_supports_tool_servers
    from vs_agent.orphans import (
        OrphanReapError,
        reap_orphaned_agents,
        reap_recorded_agents,
        reap_step_agents,
    )
    from vs_agent.scripted_client import ScriptedAgentClient, ScriptedClientSetup, ScriptedTurn
    from vs_agent.shim_turns import native_schema, native_schema_problems
    from vs_sandbox.api import DockerSandbox, HostResource, ProjectPathPolicy
    from vs_sim.api import Clock, Threads

__all__ = [
    "ALLOWED_MODELS_SETTING",
    "BASE_ENV_ALLOWLIST",
    "CHECK_TIMEOUT_S",
    "CLAUDE_CLI_MIN_FOR_GENERATION",
    "CLI_VERSIONS",
    "CONTAINER_FRAMEWORK_ROOT",
    "CONTAINER_PYTHON",
    "CONTAINER_PYTHON_HOME",
    "CONTAINER_PYTHON_VERSION",
    "CONTAINER_SERVER_PACKAGES",
    "CONTEXT_RENEWAL_FRACTION",
    "DEFAULT_CLI_PROVIDER",
    "DEFAULT_TURN_TIMEOUT_S",
    "DOCKER_PROVIDER_ENV",
    "MAX_REPLY_BYTES",
    "MAX_WHITESPACE_RUN",
    "NODE_VERSION",
    "NULL_AGENT_EVENT_SINK",
    "NULL_SKILL_SELECTION",
    "SHIPPED_PROVIDERS",
    "STOP_POLL_S",
    "AgentBackend",
    "AgentCapabilities",
    "AgentClient",
    "AgentClientProtocol",
    "AgentContextOverflowError",
    "AgentEvent",
    "AgentEventKind",
    "AgentEventSink",
    "AgentExecutionCancelledError",
    "AgentExecutionPolicy",
    "AgentInvocationRecord",
    "AgentInvocationState",
    "AgentInvocationStore",
    "AgentModelUsage",
    "AgentObserver",
    "AgentOutputChannel",
    "AgentOutputSchemaError",
    "AgentProgress",
    "AgentQuotaError",
    "AgentRateLimit",
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
    "Attribution",
    "AuthStatus",
    "CandidateProgress",
    "CapacityGate",
    "CapacityGated",
    "CapacityStops",
    "ClientAgentSessions",
    "CommandResultPayload",
    "Completed",
    "ContextUse",
    "CostBasis",
    "DurableSessionStore",
    "Harness",
    "InvalidResponse",
    "InvocationConflictError",
    "InvocationOutcome",
    "JsonResultPayload",
    "LoginCommand",
    "LoginCredential",
    "LoginKind",
    "LoginState",
    "MCPServerSpec",
    "ModelDisplay",
    "ModelGenerationFacts",
    "ModelNotAllowedError",
    "NullAgentEventSink",
    "NullSessionStore",
    "OrphanReapError",
    "Pending",
    "ProviderLogin",
    "ProviderNotReadyError",
    "ProviderReadiness",
    "ProviderRoutedExecutor",
    "ProviderSwitch",
    "ProviderSwitchReason",
    "QuotaCondition",
    "QuotaPlan",
    "QuotaResumeReason",
    "QuotaWaitCancelReason",
    "ReadinessProbe",
    "ReadinessProblem",
    "ReplyBreach",
    "ReplyGuard",
    "RoundProgress",
    "SchemaViolation",
    "ScriptedAgentClient",
    "ScriptedClientSetup",
    "ScriptedTurn",
    "SessionConfigurationError",
    "SessionPersistenceError",
    "SessionResumeError",
    "SessionScope",
    "SessionStore",
    "SkillSelection",
    "SteerOutcome",
    "SteerableAgentClient",
    "ThreadsClock",
    "TodoItemData",
    "ToolResultPayload",
    "ToolServerFailure",
    "Unknown",
    "UnvettedHarnessError",
    "UsageSpeed",
    "agent_supports_tool_servers",
    "auth_bind_mounts",
    "auth_copy_paths",
    "auth_env_passthrough",
    "auth_env_vars",
    "auth_paths",
    "build_agent_client",
    "check_allowed_model",
    "check_harness",
    "check_tool_servers",
    "cli_mcp_config_files",
    "cli_skill_dirs",
    "container_import_roots",
    "containerize_server",
    "declare_command_host_resources",
    "declare_provider_state_resources",
    "default_model",
    "describe_validation_error",
    "harnesses",
    "inspect_invocation_journal",
    "launch_shell_environment",
    "materialize_skills",
    "model_display",
    "native_schema",
    "native_schema_problems",
    "parse_typed_response",
    "provider_binary",
    "provider_display_name",
    "provider_login",
    "provider_login_command",
    "reap_orphaned_agents",
    "reap_recorded_agents",
    "reap_step_agents",
    "reply_breach",
    "session_env_allowlist",
    "session_environment",
    "task_agent_host_resources",
    "todos_from_tool_call",
    "validate_env_names",
    "vetted_models",
]


def __getattr__(name: str) -> object:
    """Expose the heavy composition lazily so imports cannot form a cycle."""
    if name == "AgentClient":
        from vs_agent.client import (  # noqa: PLC0415  # lint-waiver: LW-010114 [PLC0415]; Keep AgentClient lazy in __getattr__ so unused providers and import cycles stay unloaded.
            AgentClient,
        )

        return AgentClient
    if name in {"ScriptedAgentClient", "ScriptedClientSetup", "ScriptedTurn"}:
        # The scripted client builds on the fake client, which loads the skill and provider
        # helpers; only an agent-less run needs it.
        from vs_agent.scripted_client import (  # noqa: PLC0415  # lint-waiver: LW-482908 [PLC0415]; keep the fake client's imports unloaded for consumers of vs_agent.api that never run agent-less, like the other lazy names here.
            ScriptedAgentClient,
            ScriptedClientSetup,
            ScriptedTurn,
        )

        return {
            "ScriptedAgentClient": ScriptedAgentClient,
            "ScriptedClientSetup": ScriptedClientSetup,
            "ScriptedTurn": ScriptedTurn,
        }[name]
    if name == "materialize_skills":
        # Eager import runs provider discovery and loads unused agent backends.
        # Moving the whole helper module would split its existing shared CLI
        # surface; importlib would hide this dependency from static analysis.
        from vs_agent.cli_common import (  # noqa: PLC0415  # lint-waiver: LW-127905 [PLC0415]; preserve the declared CLI helper dependency lazily without eagerly loading provider backends or hiding it behind dynamic importlib lookup.
            materialize_skills,
        )

        return materialize_skills
    if name == "agent_supports_tool_servers":
        from vs_agent.factory import (  # noqa: PLC0415  # lint-waiver: LW-010115 [PLC0415]; Keep this dependency lazy in __getattr__ so unused providers and import cycles stay unloaded.
            agent_supports_tool_servers,
        )

        return agent_supports_tool_servers
    if name in {"native_schema", "native_schema_problems"}:
        # Reads agentshim's provider profiles; importing it at module import
        # would load agentshim into every consumer of this package.
        from vs_agent.shim_turns import (  # noqa: PLC0415  # lint-waiver: LW-010310 [PLC0415]; keep agentshim unloaded for consumers of vs_agent.api that never check a schema, like the other lazy names here.
            native_schema,
            native_schema_problems,
        )

        return {
            "native_schema": native_schema,
            "native_schema_problems": native_schema_problems,
        }[name]
    if name in {
        "OrphanReapError",
        "reap_orphaned_agents",
        "reap_recorded_agents",
        "reap_step_agents",
    }:
        # The reaper builds agentshim confinements, and importing agentshim at
        # module import would load it into every consumer of this package.
        from vs_agent.orphans import (  # noqa: PLC0415  # lint-waiver: LW-482907 [PLC0415]; keep agentshim unloaded for consumers of vs_agent.api that never reap, like the other lazy names here.
            OrphanReapError,
            reap_orphaned_agents,
            reap_recorded_agents,
            reap_step_agents,
        )

        return {
            "OrphanReapError": OrphanReapError,
            "reap_orphaned_agents": reap_orphaned_agents,
            "reap_recorded_agents": reap_recorded_agents,
            "reap_step_agents": reap_step_agents,
        }[name]
    message = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(message)


def build_agent_client(  # noqa: PLR0913  # lint-waiver: LW-011100 [PLR0913]; preserve the public keyword parameters shared with the factory so callers can configure each agent setting directly.
    *,
    spec: AgentSpec,
    backends: dict[str, Any] | None,
    workspace_sandboxes: Callable[[Path], DockerSandbox | None] | None = None,
    executor_factory: Callable[[], CommandExecutor] | None = None,
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
    launcher_env: Callable[[], Mapping[str, str]] | None = None,
    clock: Clock,
    threads: Threads,
) -> AgentClientProtocol:
    """Build an agent service through the application composition module."""
    from vs_agent.factory import (  # noqa: PLC0415  # lint-waiver: LW-010116 [PLC0415]; Keep build_agent_client as build lazy in build_agent_client so unused providers and import cycles stay unloaded.
        build_agent_client as build,
    )

    return build(
        spec=spec,
        backends=backends,
        workspace_sandboxes=workspace_sandboxes,
        executor_factory=executor_factory,
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
        launcher_env=launcher_env,
        clock=clock,
        threads=threads,
    )
