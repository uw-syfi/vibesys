"""Application composition for agent clients and execution drivers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vibesys.agents.catalog import agent_catalog
from vibesys.agents.client import AgentClient, AgentDiagnosticLog
from vibesys.agents.sink import NULL_AGENT_EVENT_SINK
from vibesys.agents.spec import AgentBackend, Driver
from vs_agent.skills import NULL_SKILL_SELECTION

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path
    from typing import TextIO

    from vibesys.agents.contracts import AgentClientProtocol
    from vibesys.agents.session_store import SessionStore
    from vibesys.agents.sink import AgentEventSink
    from vibesys.agents.spec import AgentSpec
    from vs_agent.skills import SkillSelection
    from vs_sandbox import HostResource, ProjectPathPolicy


def agent_driver_supports_mcp_servers(spec: AgentSpec) -> bool | None:
    """Return whether the configured external driver supports session MCP.

    This query has no runtime side effects, so wiring code can reject an
    incompatible feature before creating a project or driver resources.
    Non-CLI backends do not use the external-driver contract.
    """
    if spec.backend != AgentBackend.CLI:
        return None

    driver_name = spec.driver
    if driver_name is Driver.OMNIGENT:
        from vibesys.agents.drivers.omnigent import OMNIGENT_CAPABILITIES  # noqa: PLC0415

        return OMNIGENT_CAPABILITIES.mcp_servers
    if driver_name is Driver.MOCK:
        from vibesys.agents.drivers.mock import MOCK_CAPABILITIES  # noqa: PLC0415

        return MOCK_CAPABILITIES.mcp_servers

    from vibesys.agents.drivers.agentshim import AGENTSHIM_CAPABILITIES  # noqa: PLC0415

    return AGENTSHIM_CAPABILITIES.mcp_servers


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
    """Build the configured application-level agent service from ``spec``."""
    host_resources = tuple(host_resources)
    backend = spec.backend

    if require_host_sandbox and backend not in {AgentBackend.CLI, AgentBackend.STUB}:
        raise SystemExit(  # noqa: TRY003  # tracked: #288
            "local project execution requires the CLI agent backend so VibeSys can "
            "enforce nested read-only and hidden paths"
        )

    if backend is AgentBackend.STUB:
        from vibesys.agents.stub_runner import StubAgentClient  # noqa: PLC0415

        return StubAgentClient(event_sink=events)

    if backend != AgentBackend.CLI:
        raise SystemExit(f"unknown agent backend: {backend.value!r}")  # noqa: TRY003  # tracked: #288

    driver_name = spec.driver
    provider = spec.provider
    timeout = spec.cli_timeout
    driver_log = AgentDiagnosticLog(run_log_file)

    if use_docker and not agent_catalog()[driver_name].supports_docker:
        raise SystemExit(  # noqa: TRY003  # tracked: #288
            f"agent.driver={driver_name.value!r} is not supported with --docker"
        )

    if driver_name == Driver.MOCK:
        from vibesys.agents.drivers.mock import MockDriver  # noqa: PLC0415

        driver = MockDriver()
    elif driver_name == Driver.OMNIGENT:
        from vibesys.agents.drivers.omnigent import (  # noqa: PLC0415
            OmnigentDriver,
            OmnigentDriverError,
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
        skill_selection=skill_selection,
        model_name=spec.model or provider,
        timeout=timeout,
        run_log_file=run_log_file,
        log_dir=log_dir,
        default_reasoning_effort=spec.reasoning_effort,
        role_models=spec.role_models,
        role_reasoning_efforts=spec.role_reasoning_efforts,
        project_path_policy=project_path_policy,
        host_resources=host_resources,
        require_host_sandbox=require_host_sandbox,
        containerized=use_docker,
        driver_log=driver_log,
        session_store=session_store,
        event_sink=events,
    )
