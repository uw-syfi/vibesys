"""Product-owned wiring between VibeSys configuration and runtime adapters."""

from __future__ import annotations

import importlib.metadata
import importlib.util
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from vibesys.constants import DomainName
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vs_agent.api import (
    DEFAULT_CLI_PROVIDER,
    AgentBackend,
    AgentSpec,
    Driver,
)
from vs_runtime.api.infrastructure import (
    ModelArtifactRequest,
    PreparedModelArtifacts,
    prepare_model_artifacts,
)
from vs_sandbox.api import HostResource, HostResourceAccess

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vibesys.config import Config
    from vibesys.run.evaluation_backend import SemanticEvaluationBackend
    from vs_evaluation.api import EvaluationAgentService
    from vs_runtime.api import AgentRole

_DISTRIBUTION = "vibesys"
"""The distribution whose top-level packages confined tool servers import."""


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


def _vibesys_runtime_host_resources() -> tuple[HostResource, ...]:
    """Declare the installed product packages needed by host-confined agents.

    Stdio tool servers run ``python -m <first-party module>`` inside the
    sandbox. A wheel installs every first-party package under one
    site-packages directory, but an editable checkout keeps each library under
    its own ``libs/<name>/src`` root, so every distinct root must be readable.
    """
    roots = {Path(__file__).resolve().parents[1]}
    for name, distributions in importlib.metadata.packages_distributions().items():
        if _DISTRIBUTION not in distributions:
            continue
        spec = importlib.util.find_spec(name)
        if spec is not None and spec.origin is not None:
            roots.add(Path(spec.origin).resolve().parents[1])
    return tuple(
        HostResource(root, HostResourceAccess.READ_ONLY, "VibeSys runtime")
        for root in sorted(roots)
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
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="agent_driver_configuration_invalid",
                stage="agent_configuration_validation",
                message=message,
            )
        )

    resolved_provider = provider or agent_cfg.cli_provider or DEFAULT_CLI_PROVIDER
    return AgentSpec(
        backend=resolved_backend,
        driver=resolved_driver,
        provider=resolved_provider,
        model=model if model is not None else config.model.name,
        reasoning_effort=config.thinking.level,
        cli_timeout=agent_cfg.cli_timeout,
        env_passthrough=agent_cfg.env_passthrough,
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
                    f"orchestration: {', '.join(f'agent.roles.{role_id}' for role_id in unknown)}"
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
