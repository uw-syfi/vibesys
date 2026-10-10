"""Application composition for agent clients and their session launchers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vs_agent.client import AgentClient, AgentDiagnosticLog
from vs_agent.sink import NULL_AGENT_EVENT_SINK
from vs_agent.skills import NULL_SKILL_SELECTION
from vs_agent.spec import AgentBackend

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path
    from typing import TextIO

    from agentshim import CommandExecutor

    from vs_agent.contracts import AgentClientProtocol
    from vs_agent.session_store import SessionStore
    from vs_agent.sink import AgentEventSink
    from vs_agent.skills import SkillSelection
    from vs_agent.spec import AgentSpec
    from vs_sandbox.api import DockerSandbox, HostResource, ProjectPathPolicy


def agent_supports_tool_servers(spec: AgentSpec) -> bool | None:
    """Return whether the configured agent backend supports agent tool servers.

    This query has no runtime side effects, so wiring code can reject an
    incompatible feature before creating a project or launcher resources.
    Non-CLI backends have no external provider session, so they answer ``None``.
    """
    if spec.backend != AgentBackend.CLI:
        return None

    from vs_agent.session_launch import (  # noqa: PLC0415  # lint-waiver: LW-010171 [PLC0415]; Keep AGENTSHIM_CAPABILITIES lazy so unused providers and import cycles stay unloaded.
        AGENTSHIM_CAPABILITIES,
    )

    return AGENTSHIM_CAPABILITIES.tool_servers


def build_agent_client(  # noqa: PLR0913  # lint-waiver: LW-010172 [PLR0913]; Preserve build_agent_client's named-argument contract because callers pass these independent settings directly.
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
) -> AgentClientProtocol:
    """Build the configured application-level agent service from ``spec``.

    ``agent_homes_dir`` is the run's root for dedicated provider CLI homes
    (``Project.agent_homes_directory_for``); without it a provider that can
    only isolate through such a home runs with the operator's configuration.
    """
    host_resources = tuple(host_resources)
    backend = spec.backend

    if require_host_sandbox and backend not in {AgentBackend.CLI, AgentBackend.STUB}:
        message = (
            "local project execution requires the CLI agent backend so VibeSys can "
            "enforce nested read-only and hidden paths"
        )
        raise SystemExit(message)

    if backend is AgentBackend.STUB:
        from vs_agent.stub_runner import (  # noqa: PLC0415  # lint-waiver: LW-010173 [PLC0415]; Keep StubAgentClient lazy in build_agent_client so unused providers and import cycles stay unloaded.
            StubAgentClient,
        )

        return StubAgentClient(event_sink=events)

    if backend != AgentBackend.CLI:
        message = f"unknown agent backend: {backend.value!r}"
        raise SystemExit(message)

    provider = spec.provider
    timeout = spec.cli_timeout
    diagnostic_log = AgentDiagnosticLog(run_log_file)

    if workspace_sandboxes is not None and not use_docker:
        message = "workspace_sandboxes routes container turns, but this client does not use Docker"
        raise ValueError(message)
    docker_sandboxes = None
    if use_docker:
        if backends is None:
            message = "a Docker agent client needs the run's sandboxes (backends), got none"
            raise ValueError(message)
        from vs_agent.cli_docker import (  # noqa: PLC0415  # lint-waiver: LW-010175 [PLC0415]; Keep DOCKER_PROVIDER_ENV lazy in build_agent_client so unused providers and import cycles stay unloaded.
            DOCKER_PROVIDER_ENV,
        )

        if provider not in DOCKER_PROVIDER_ENV:
            message = (
                f"--cli-provider {provider!r} is not yet supported in Docker; "
                f"supported: {sorted(DOCKER_PROVIDER_ENV)}"
            )
            raise SystemExit(message)
        docker_sandboxes = backends
    from vs_agent.session_launch import (  # noqa: PLC0415  # lint-waiver: LW-010176 [PLC0415]; Keep ConfinedSessionLauncher lazy in build_agent_client so unused providers and import cycles stay unloaded.
        ConfinedSessionLauncher,
    )

    launcher = ConfinedSessionLauncher(
        provider=provider,
        timeout=timeout,
        docker_sandboxes=docker_sandboxes,
        workspace_sandboxes=workspace_sandboxes,
        executor_factory=executor_factory,
        log=diagnostic_log,
        agent_homes=agent_homes_dir,
        env_passthrough=spec.env_passthrough,
    )

    return AgentClient(
        launcher,
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
        diagnostic_log=diagnostic_log,
        session_store=session_store,
        event_sink=events,
        check_readiness=True,
    )
