"""Thread-affine ownership of one explicit agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol, TypeVar, cast

from pydantic import BaseModel

from vs_runtime._agent_lifecycle import (
    AgentExecutionFinished,
    AgentExecutionLifecycleEvent,
    AgentExecutionLifecycleSink,
    AgentExecutionStarted,
    AgentExecutionStatus,
)
from vs_sandbox.api import EnvironmentBindMount, HostResourceAccess
from vs_sim.api import CloseOnce, ReleaseStack

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from typing import TextIO

    from vs_agent.api import (
        AgentInvocationStore,
        AgentSessionKey,
        AgentSpec,
        SkillSelection,
    )
    from vs_runtime._run_environment import RunEnvironmentRequest, RunEnvironmentSession
    from vs_sandbox.api import CommandRunner, HostResource, ProjectPathPolicy
    from vs_sim.api import Released

ResponseT = TypeVar("ResponseT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class AgentExecutionConfiguration:
    """Composition-resolved, session-fixed inputs for one agent execution."""

    agent_id: str
    spec: AgentSpec
    resources: tuple[HostResource, ...] = ()
    reasoning_effort: str | None = None


class AgentExecutionEnvironment(Protocol):
    """One scoped environment opened and closed on the agent worker thread."""

    @property
    def skill_source_dirs(self) -> tuple[Path, ...]: ...

    @property
    def skill_selection(self) -> SkillSelection: ...

    @property
    def project_path_policy(self) -> ProjectPathPolicy: ...

    @property
    def host_resources(self) -> tuple[HostResource, ...]: ...

    @property
    def backends(self) -> dict[str, CommandRunner] | None: ...

    @property
    def use_docker(self) -> bool: ...

    def close(self) -> None:
        """Release the scoped environment idempotently."""
        ...


class _AgentPathEnvironment(Protocol):
    """Narrow capability used only when exporting an agent-visible path."""

    def agent_path(self, host_path: Path | str) -> str: ...


class SharedAgentEnvironmentConflictError(ValueError):
    """A shared run session cannot satisfy agent-specific environment inputs."""

    def __init__(self) -> None:
        super().__init__("a shared agent environment cannot override backend, provider, or mounts")


@dataclass(slots=True)
class ScopedAgentEnvironment:
    """Runtime-owned agent view over a borrowed or independently opened session."""

    session: RunEnvironmentSession
    skill_source_dirs: tuple[Path, ...]
    skill_selection: SkillSelection
    project_path_policy: ProjectPathPolicy
    host_resources: tuple[HostResource, ...]
    backends: dict[str, CommandRunner] | None
    use_docker: bool
    owns_session: bool
    _close: CloseOnce = field(default_factory=CloseOnce)

    def close(self) -> None:
        """Release an owned session; borrowed sessions remain open. A failed close runs again."""
        if self.owns_session:
            self._close.run(self._release)

    def _release(self) -> Released:
        steps = ReleaseStack()
        steps.callback(self.session.close)
        return steps.close()

    def agent_path(self, host_path: Path | str) -> str:
        """Translate a host path through this environment's active sandbox."""
        return cast("_AgentPathSandbox", self.session.sandbox).agent_path(host_path)


class _AgentPathSandbox(Protocol):
    """Path translation implemented by every supported agent sandbox."""

    def agent_path(self, host_path: Path | str) -> str: ...


def open_agent_execution_environment(  # noqa: PLR0913  # lint-waiver: LW-954433 [PLR0913]; these are independent resolved lower-layer resources; bundling them would create the broad environment DTO this composition seam replaces.
    base_request: RunEnvironmentRequest,
    shared_session: RunEnvironmentSession | None,
    *,
    share_session: bool,
    skill_source_dirs: tuple[Path, ...],
    skill_selection: SkillSelection,
    host_resources: tuple[HostResource, ...],
    mounts: tuple[HostResource, ...] = (),
    agent_backend: str | None = None,
    cli_provider: str | None = None,
    open_session: Callable[[RunEnvironmentRequest], RunEnvironmentSession],
) -> ScopedAgentEnvironment:
    """Open one agent environment, borrowing the run session when required.

    A shared session fixes backend, provider, and mounts at workspace-open time.
    Independently opened sessions inherit that request and add only the explicit
    per-agent overrides and resources supplied here.
    """
    if share_session:
        if (
            mounts
            or (agent_backend is not None and agent_backend != base_request.agent_backend)
            or (cli_provider is not None and cli_provider != base_request.cli_provider)
        ):
            raise SharedAgentEnvironmentConflictError
        if shared_session is None:
            raise SharedAgentEnvironmentConflictError
        session = shared_session
        request = base_request
        owns_session = False
    else:
        request = replace(
            base_request,
            session_role=type(base_request.session_role).CHILD,
            agent_backend=(
                agent_backend if agent_backend is not None else base_request.agent_backend
            ),
            cli_provider=(cli_provider if cli_provider is not None else base_request.cli_provider),
            environment_bind_mounts=(
                *base_request.environment_bind_mounts,
                *(
                    EnvironmentBindMount(
                        mount.path,
                        mount.agent_path if mount.agent_path is not None else str(mount.path),
                        read_only=mount.access is HostResourceAccess.READ_ONLY,
                    )
                    for mount in mounts
                ),
            ),
        )
        session = open_session(request)
        owns_session = True

    sandboxed = session.view.cli_sandboxed
    return ScopedAgentEnvironment(
        session=session,
        skill_source_dirs=skill_source_dirs,
        skill_selection=skill_selection,
        project_path_policy=request.project_path_policy,
        host_resources=host_resources,
        backends={"chat": session.sandbox} if sandboxed else None,
        use_docker=sandboxed,
        owns_session=owns_session,
    )


type ScopedAgentEnvironmentOpener = Callable[
    [AgentExecutionConfiguration], AgentExecutionEnvironment
]


@dataclass(frozen=True, slots=True)
class AgentExecutionScope:
    """Run-owned effects and paths fixed for one agent execution."""

    workspace_path: Path
    log_directory: Path
    open_environment: ScopedAgentEnvironmentOpener
    current_log_file: Callable[[], TextIO]
    environment_variables: Callable[[], Mapping[str, str]]
    invocation_store: Callable[[AgentSessionKey], AgentInvocationStore] | None = None
    #: Root of the run's dedicated agent CLI homes (see ``build_agent_client``).
    agent_homes_directory: Path | None = None


__all__ = [
    "AgentExecutionConfiguration",
    "AgentExecutionEnvironment",
    "AgentExecutionFinished",
    "AgentExecutionLifecycleEvent",
    "AgentExecutionLifecycleSink",
    "AgentExecutionScope",
    "AgentExecutionStarted",
    "AgentExecutionStatus",
    "ScopedAgentEnvironment",
    "SharedAgentEnvironmentConflictError",
    "open_agent_execution_environment",
]
