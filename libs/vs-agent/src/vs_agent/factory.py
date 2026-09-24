"""Application composition for agent clients and execution drivers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vs_agent.catalog import agent_catalog
from vs_agent.client import AgentClient, AgentDiagnosticLog
from vs_agent.sink import NULL_AGENT_EVENT_SINK
from vs_agent.skills import NULL_SKILL_SELECTION
from vs_agent.spec import AgentBackend, Driver

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path
    from typing import TextIO

    from vs_agent.contracts import AgentClientProtocol
    from vs_agent.session_store import SessionStore
    from vs_agent.sink import AgentEventSink
    from vs_agent.skills import SkillSelection
    from vs_agent.spec import AgentSpec
    from vs_sandbox.api import HostResource, ProjectPathPolicy


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
        from vs_agent.drivers.omnigent import OMNIGENT_CAPABILITIES

        return OMNIGENT_CAPABILITIES.mcp_servers

    from vs_agent.drivers.agentshim import AGENTSHIM_CAPABILITIES

    return AGENTSHIM_CAPABILITIES.mcp_servers


def build_agent_client(
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
        raise SystemExit(  # noqa: TRY003  # lint-waiver: LW-008075 [TRY003]; this CLI policy failure must exit with SystemExit for the launcher to report configuration errors.
            "local project execution requires the CLI agent backend so VibeSys can "
            "enforce nested read-only and hidden paths"
        )

    if backend is AgentBackend.STUB:
        from vs_agent.stub_runner import StubAgentClient

        return StubAgentClient(event_sink=events)

    if backend != AgentBackend.CLI:
        raise SystemExit(f"unknown agent backend: {backend.value!r}")  # noqa: TRY003  # lint-waiver: LW-008076 [TRY003]; preserve the CLI SystemExit contract for an invalid backend.

    driver_name = spec.driver
    provider = spec.provider
    timeout = spec.cli_timeout
    driver_log = AgentDiagnosticLog(run_log_file)

    if use_docker and not agent_catalog()[driver_name].supports_docker:
        raise SystemExit(f"agent.driver={driver_name.value!r} is not supported with --docker")  # noqa: TRY003  # lint-waiver: LW-008077 [TRY003]; preserve the CLI SystemExit contract for an unsupported docker policy.

    if driver_name == Driver.OMNIGENT:
        from vs_agent.drivers.omnigent import (
            OmnigentDriver,
            OmnigentDriverError,
        )

        if host_resources:
            raise OmnigentDriverError.host_resource_grants_require_agentshim(
                [str(resource.path) for resource in host_resources]
            )

        driver = OmnigentDriver()
    else:
        docker_sandboxes = None
        if use_docker:
            from vs_agent.cli_docker import DOCKER_PROVIDER_ENV

            if provider not in DOCKER_PROVIDER_ENV:
                raise SystemExit(  # noqa: TRY003  # lint-waiver: LW-008078 [TRY003]; preserve the CLI SystemExit contract and list providers not yet supported by docker.
                    f"--cli-provider {provider!r} is not yet supported with --docker; "
                    f"supported: {sorted(DOCKER_PROVIDER_ENV)}"
                )
            docker_sandboxes = backends
        from vs_agent.drivers.agentshim import AgentShimDriver

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
