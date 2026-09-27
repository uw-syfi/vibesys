"""Product-owned wiring between VibeSys configuration and runtime adapters."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from vibesys.constants import DomainName
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vs_agent.api import (
    DEFAULT_CLI_PROVIDER,
    AgentBackend,
    AgentSpec,
    Driver,
    StdioServerDescriptor,
    ToolServerDescriptor,
    expose_as_tools,
)
from vs_evaluation.api import (
    EvaluationAgentRole,
    EvaluationAgentService,
)
from vs_evaluation.api.tools import evaluation_mcp_descriptor
from vs_runtime.api.infrastructure import (
    ModelArtifactRequest,
    PreparedModelArtifacts,
    prepare_model_artifacts,
)
from vs_sandbox.api import HostResource, HostResourceAccess

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys.config import Config
    from vibesys.run.evaluation_backend import SemanticEvaluationBackend
    from vs_runtime.api import AgentRole, AgentToolBindingContext


@dataclass(slots=True)
class AgentToolContext:
    """Product facts available while binding one declared agent tool."""

    profiler_id: str
    profiler_env: tuple[tuple[str, str], ...] = ()
    evaluation_service: EvaluationAgentService | None = None
    evaluation_backend: SemanticEvaluationBackend | None = None

    def install_evaluation(
        self,
        service: EvaluationAgentService,
        backend: SemanticEvaluationBackend,
    ) -> None:
        """Install the run-owned service before orchestration creates sessions."""
        if self.evaluation_service is not None or self.evaluation_backend is not None:
            message = "evaluation agent service is already installed"
            raise RuntimeError(message)
        self.evaluation_service = service
        self.evaluation_backend = backend


def prepare_domain_model_artifacts(
    domain: DomainName,
    request: ModelArtifactRequest,
    *,
    isolated: bool,
    materialize_local_weights: bool,
) -> PreparedModelArtifacts:
    """Bind the closed LLM-serving domain to runtime model preparation."""
    if domain is not DomainName.LLM_SERVING:
        return PreparedModelArtifacts()
    return prepare_model_artifacts(
        request,
        isolated=isolated,
        materialize_local_weights=materialize_local_weights,
    )


def _vibesys_runtime_host_resource() -> HostResource:
    """Declare the installed product package needed by host-confined agents."""
    return HostResource(
        Path(__file__).resolve().parents[1],
        HostResourceAccess.READ_ONLY,
        "VibeSys runtime",
    )


def resolve_agent_driver(config: Config) -> Driver:
    """Resolve the configured agent driver, defaulting to agentshim."""
    return Driver(config.agent.driver) if config.agent.driver is not None else Driver.AGENTSHIM


def agent_spec_from_config(
    config: Config,
    *,
    backend: AgentBackend | str | None = None,
    driver: Driver | str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> AgentSpec:
    """Bind application config and explicit overrides into one agent spec."""
    agent_cfg = config.agent
    resolved_backend = AgentBackend(backend or agent_cfg.backend or AgentBackend.CLI)
    resolved_driver = Driver(driver) if driver is not None else resolve_agent_driver(config)

    if resolved_backend != AgentBackend.CLI and agent_cfg.driver is not None:
        message = f"agent driver {agent_cfg.driver!r} is valid only with backend='cli', not {resolved_backend.value!r}"
        raise SystemExit(message)

    resolved_provider = provider or agent_cfg.cli_provider or DEFAULT_CLI_PROVIDER
    return AgentSpec(
        backend=resolved_backend,
        driver=resolved_driver,
        provider=resolved_provider,
        model=model if model is not None else config.model.name,
        reasoning_effort=config.thinking.level,
        cli_timeout=agent_cfg.cli_timeout,
    )


def resolve_agent_specs(
    config: Config,
    roles: tuple[AgentRole, ...],
    *,
    backend: AgentBackend | str | None = None,
    provider: str | None = None,
) -> Mapping[str, AgentSpec]:
    """Resolve one immutable execution spec per plugin-declared role.

    Authored role entries are sparse overrides. Every authored key must name a
    role in the selected plugin; every declared role is returned, inheriting
    the global model and reasoning defaults when no override is present.
    """
    declared = {role.id for role in roles}
    unknown = sorted(config.agent.roles.keys() - declared)
    if unknown:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="agent_role_configuration_invalid",
                stage="agent_configuration_validation",
                message=(
                    "agent configuration names roles not declared by the selected "
                    f"orchestration: {', '.join(unknown)}"
                ),
            )
        )
    resolved: dict[str, AgentSpec] = {}
    default_spec = agent_spec_from_config(
        config,
        backend=backend,
        provider=provider,
    )
    for role in roles:
        override = config.agent.roles.get(role.id)
        resolved[role.id] = replace(
            default_spec,
            model=(
                override.model
                if override is not None and override.model is not None
                else default_spec.model
            ),
            reasoning_effort=(
                override.reasoning_effort
                if override is not None and override.reasoning_effort is not None
                else default_spec.reasoning_effort
            ),
        )
    return MappingProxyType(resolved)


def _profiler_tool(
    context: object, _binding: AgentToolBindingContext
) -> tuple[ToolServerDescriptor, ...]:
    """Bind the selected profiler's analysis server to one agent session."""
    resolved = cast("AgentToolContext", context)
    if resolved.profiler_id == "none":
        return ()
    support_name = f"{resolved.profiler_id}_profiler"
    return (
        StdioServerDescriptor(
            name=f"vibesys-{resolved.profiler_id.replace('_', '-')}-profiler",
            command="python",
            args=(f"{support_name}/server.py",),
            env=resolved.profiler_env,
        ),
    )


def _issue_board_tool(
    _host: object, _binding: AgentToolBindingContext
) -> tuple[ToolServerDescriptor, ...]:
    """Bind the fixed issue-board server to workspace-relative policy artifacts."""
    return (
        expose_as_tools(
            name="vibesys-issue-board",
            entrypoint_module="vibesys.orchestration.issue_queue.tool_server",
            entrypoint_args=(
                "issues.json",
                ".vibesys/issue-tool-policy.json",
                ".vibesys/issue-tracker.json",
            ),
        ),
    )


def _evaluation_tool(
    context: object, binding: AgentToolBindingContext
) -> tuple[ToolServerDescriptor, ...]:
    """Issue one role and logical-member scoped evaluation capability."""
    resolved = cast("AgentToolContext", context)
    service = resolved.evaluation_service
    backend = resolved.evaluation_backend
    if service is None or backend is None:
        message = "evaluation agent service is not installed"
        raise RuntimeError(message)
    role = _EVALUATION_ROLES.get(binding.role.id)
    if role is None:
        message = f"agent role {binding.role.id!r} has no evaluation capability profile"
        raise RuntimeError(message)
    backend.bind(binding)
    scope_id = binding.workspace.id
    principal_member = binding.member_id or scope_id or "root"
    grant = service.grant(
        principal_id=f"{role.value}:{principal_member}",
        role=role,
        scope_id=scope_id,
        run_observer=role is EvaluationAgentRole.RUN_OBSERVER,
    )
    return (evaluation_mcp_descriptor(grant, binding.agent_path(service.socket_path)),)


AGENT_TOOL_BINDINGS: Mapping[
    str, Callable[[object, AgentToolBindingContext], tuple[ToolServerDescriptor, ...]]
] = {
    "evaluation": _evaluation_tool,
    "issue-board": _issue_board_tool,
    "profiler": _profiler_tool,
}
"""Built-in agent tools bound by product composition, not orchestration policy."""


_EVALUATION_ROLES: Mapping[str, EvaluationAgentRole] = {
    "dynamic-implementer": EvaluationAgentRole.IMPLEMENTER,
    "dynamic-judge": EvaluationAgentRole.JUDGE,
    "dynamic-orchestrator": EvaluationAgentRole.RUN_OBSERVER,
    "dynamic-profiler": EvaluationAgentRole.PROFILER,
    "implementer": EvaluationAgentRole.IMPLEMENTER,
    "judge": EvaluationAgentRole.JUDGE,
    "orchestrator": EvaluationAgentRole.ORCHESTRATOR,
    "portfolio_dispatch": EvaluationAgentRole.PORTFOLIO_DISPATCH,
    "profiler": EvaluationAgentRole.PROFILER,
}
