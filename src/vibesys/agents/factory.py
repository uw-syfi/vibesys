"""Application composition for agent clients and execution drivers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vibesys.agents.client import AgentClient, AgentDiagnosticLog
from vibesys.agents.provider_policy import DEFAULT_CLI_PROVIDER
from vibesys.constants import DEFAULT_AGENT_BACKEND

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path
    from typing import TextIO

    from vibesys.agents.contracts import AgentClientProtocol
    from vibesys.agents.session_store import SessionStore
    from vibesys.config import Config
    from vibesys.constants import ComputeBackend
    from vs_sandbox import HostResource, ProjectPathPolicy

DEFAULT_AGENT_DRIVER = "agentshim"

AGENT_DRIVERS: tuple[str, ...] = ("agentshim", "omnigent", "mock")
"""Every driver this application configuration can select.

``mock`` is test infrastructure: it satisfies the same driver contract while
streaming a deterministic playbook, so integration tests exercise the real
client, sink, and application integration path without an agent CLI.
"""


def resolve_agent_driver(config: Config) -> str:
    """Resolve the configured agent driver, defaulting to agentshim."""
    return config.agent.driver or DEFAULT_AGENT_DRIVER


def agent_driver_supports_mcp_servers(
    config: Config,
    *,
    agent_backend: str | None,
) -> bool | None:
    """Return whether the configured external driver supports session MCP.

    This query has no runtime side effects, so wiring code can reject an
    incompatible feature before creating a project or driver resources.
    Non-CLI backends do not use the external-driver contract.
    """
    backend = agent_backend or config.agent.backend or DEFAULT_AGENT_BACKEND
    if backend != "cli":
        return None

    driver_name = resolve_agent_driver(config)
    if driver_name == "omnigent":
        from vibesys.agents.drivers.omnigent import OMNIGENT_CAPABILITIES  # noqa: PLC0415

        return OMNIGENT_CAPABILITIES.mcp_servers
    if driver_name == "mock":
        from vibesys.agents.drivers.mock import MOCK_CAPABILITIES  # noqa: PLC0415

        return MOCK_CAPABILITIES.mcp_servers

    from vibesys.agents.drivers.agentshim import AGENTSHIM_CAPABILITIES  # noqa: PLC0415

    return AGENTSHIM_CAPABILITIES.mcp_servers


def supported_cli_providers(driver_name: str) -> tuple[str, ...]:
    """Return the provider names one external driver supports.

    Raises ``ValueError`` for an unknown driver so callers validating a
    requested driver/provider pair reject both halves before building
    anything.
    """
    if driver_name == "omnigent":
        from vibesys.agents.omnigent.providers import supported_providers  # noqa: PLC0415

        return tuple(supported_providers())
    if driver_name == "agentshim":
        from vibesys.agents.drivers.agentshim import supported_providers  # noqa: PLC0415

        return tuple(supported_providers())
    if driver_name == "mock":
        from vibesys.agents.drivers.mock import supported_providers  # noqa: PLC0415

        return tuple(supported_providers())
    raise ValueError(  # noqa: TRY003  # tracked: #288
        f"unknown agent driver {driver_name!r}; expected one of: {', '.join(AGENT_DRIVERS)}"
    )


def build_agent_client(  # noqa: C901, PLR0912, PLR0913
    config: Config,
    *,
    agent_backend: str | None,
    cli_provider: str | None,
    backends: dict[str, Any] | None,
    skills: list[str],
    skill_source_dirs: list[Path],
    compute_backend: ComputeBackend | None = None,
    model: Any,  # noqa: ANN401
    model_name: str,
    run_log_file: TextIO | None,
    use_docker: bool,
    log_dir: Path | None = None,
    host_resources: Iterable[HostResource] = (),
    project_path_policy: ProjectPathPolicy | None = None,
    require_host_sandbox: bool = False,
    session_store: SessionStore | None = None,
) -> AgentClientProtocol:
    """Build the configured application-level agent service."""
    host_resources = tuple(host_resources)
    agent_cfg = config.agent
    backend = agent_backend or agent_cfg.backend or DEFAULT_AGENT_BACKEND

    if backend != "cli" and agent_cfg.driver is not None:
        raise SystemExit(  # noqa: TRY003  # tracked: #288
            f"agent driver {agent_cfg.driver!r} is valid only with backend='cli', not {backend!r}"
        )

    if require_host_sandbox and backend not in {"cli", "stub"}:
        raise SystemExit(  # noqa: TRY003  # tracked: #288
            "local project execution requires the CLI agent backend so VibeSys can "
            "enforce nested read-only and hidden paths"
        )

    if backend == "deepagents":
        if backends is None:
            raise SystemExit(  # noqa: TRY003  # tracked: #288
                "internal error: build_agent_client called with backend='deepagents' "
                "but no backends dict was provided"
            )
        from vibesys.agents.deepagents_runner import DeepAgentsClient  # noqa: PLC0415

        return DeepAgentsClient(
            model=model,
            backends=backends,
            skills=skills,
            model_name=model_name,
            run_log_file=run_log_file,
        )

    if backend == "stub":
        from vibesys.agents.stub_runner import StubAgentClient  # noqa: PLC0415

        return StubAgentClient()

    if backend != "cli":
        raise SystemExit(f"unknown agent backend: {backend!r}")  # noqa: TRY003  # tracked: #288

    driver_name = resolve_agent_driver(config)
    provider = cli_provider or agent_cfg.cli_provider or DEFAULT_CLI_PROVIDER
    timeout = agent_cfg.cli_timeout
    driver_log = AgentDiagnosticLog(run_log_file)

    if driver_name == "mock":
        from vibesys.agents.drivers.mock import MockDriver  # noqa: PLC0415

        # The mock runs no agent, so the provider name only labels the run.
        provider = "mock"
        driver = MockDriver()
    elif driver_name == "omnigent":
        if use_docker:
            raise SystemExit(  # noqa: TRY003  # tracked: #288
                "agent.driver='omnigent' is not supported with --docker"
            )
        from vibesys.agents.drivers.omnigent import (  # noqa: PLC0415
            OmnigentDriver,
            OmnigentDriverError,
        )
        from vibesys.agents.omnigent.providers import (  # noqa: PLC0415
            OMNIGENT_PROVIDER_EXECUTORS,
            supported_providers,
        )

        if provider not in OMNIGENT_PROVIDER_EXECUTORS:
            raise OmnigentDriverError(  # noqa: TRY003
                f"Omnigent does not support agent provider {provider!r}; "
                f"supported providers: {supported_providers()}. Select "
                "agent.driver='agentshim' for other providers."
            )
        if host_resources:
            raise OmnigentDriverError(  # noqa: TRY003
                "Omnigent cannot enforce the requested VibeSys host-resource "
                f"grants ({[str(resource.path) for resource in host_resources]}). Select "
                "agent.driver='agentshim' for this policy."
            )

        driver = OmnigentDriver()
    else:
        docker_sandboxes = None
        if use_docker:
            from vibesys.agents.cli_docker import DOCKER_PROVIDER_ENV  # noqa: PLC0415

            if provider not in DOCKER_PROVIDER_ENV:
                raise SystemExit(  # noqa: TRY003  # tracked: #288
                    f"--cli-provider {provider!r} is not yet supported with --docker; "
                    f"supported: {sorted(DOCKER_PROVIDER_ENV)}"
                )
            docker_sandboxes = backends
        from vibesys.agents.drivers.agentshim import AgentShimDriver  # noqa: PLC0415

        driver = AgentShimDriver(
            provider=provider,
            timeout=timeout,
            docker_sandboxes=docker_sandboxes,
            log=driver_log,
        )

    return AgentClient(
        driver,
        driver_name=driver_name,
        provider=provider,
        skills=skill_source_dirs,
        compute_backend=compute_backend,
        model_name=model_name or provider,
        timeout=timeout,
        run_log_file=run_log_file,
        log_dir=log_dir,
        default_reasoning_effort=config.thinking.level,
        role_models={
            role: configured
            for role, configured in {
                "orchestrator": agent_cfg.outer.model,
                "implementer": agent_cfg.inner.model,
            }.items()
            if configured is not None
        },
        role_reasoning_efforts={
            role: configured
            for role, configured in {
                "orchestrator": agent_cfg.outer.reasoning_effort,
                "implementer": agent_cfg.inner.reasoning_effort,
            }.items()
            if configured is not None
        },
        project_path_policy=project_path_policy,
        host_resources=host_resources,
        require_host_sandbox=require_host_sandbox,
        containerized=use_docker,
        driver_log=driver_log,
        session_store=session_store,
    )
