"""VibeSys built-in catalog and application wiring.

Only entrypoints, tests and scripts select these defaults. Core and frontends
receive the resulting contracts and never import this package.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, cast

from launch.agents import BuiltInSessionAgents
from launch.catalog import built_in_orchestrations
from launch.composition import STATELESS_TOOL_SERVERS
from vibesys.api.request import validate_descriptor as _validate_descriptor
from vibesys.api.request import validate_run_request as _validate_run_request
from vibesys.api.store import open_run_store as _open_run_store
from vibesys.api.wiring import NativeProfilerPreflight, SessionImplementations
from vibesys.api.wiring import create_session as _create_session
from vs_agent.api import SHIPPED_PROVIDERS, AgentInvocationState, build_agent_client
from vs_github.api import GitHubCLI
from vs_project.api import Git, generate_run_id
from vs_runtime.api.infrastructure import EnvironmentSystem
from vs_runtime.api.wiring import InProcessRuns
from vs_sandbox.api import SubprocessDockerCli, create_compute_backend
from vs_sim.api import ProcessesProbe, utc_datetime

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.api import (
        CoreEvent,
        OrchestrationDescriptor,
        OrchestrationRegistry,
        RunRequest,
        RunResult,
        Runs,
        RunSession,
        RunStore,
    )
    from vibesys.api.contracts import EventSink
    from vibesys.api.wiring import ProfilerPreflight, SessionAgents
    from vs_agent.api import AgentClientProtocol, AgentInvocationStore, AgentSessionKey
    from vs_project.api import GitRepositoryFactory, Project, StateStoreFactory
    from vs_runtime.api.core import RunTiming
    from vs_runtime.api.infrastructure import RunState, StopTimer
    from vs_sandbox.api import ComputeBackendImpl
    from vs_sim.api import BlockingRunner, Network, PipedProcessLauncher, Processes, Threads


@dataclass(frozen=True, slots=True)
class LaunchSettings:
    """Implementations selected once at the application wiring boundary."""

    timing: RunTiming
    """The run's time sources and lease; the entrypoint builds them from real or simulated time."""
    processes: Processes
    """Starts the run's Git and GitHub CLI commands; the entrypoint builds real or fake ones."""
    piped_processes: PipedProcessLauncher
    """Starts the run's streamed commands, such as a container command's ``docker exec``."""
    blocking: BlockingRunner
    """Runs the run's blocking calls off the event loop."""
    network: Network
    """Opens the run's sockets."""
    registry: OrchestrationRegistry | None = None
    agent_client_factory: Callable[..., AgentClientProtocol] | None = None
    backend_factory: Callable[..., ComputeBackendImpl] | None = None
    stop_timer: StopTimer | None = None
    """Times a stop's grace period; ``None`` sleeps on the run's clock."""
    agents: SessionAgents | None = None
    invocation_store_factory: Callable[[RunState, AgentSessionKey], AgentInvocationStore] | None = (
        None
    )
    git_repository: GitRepositoryFactory | None = None
    """Builds the run's ``GitRepository`` implementations; ``None`` opens the configured one."""
    slurm_processes: Processes | None = None
    """Starts the Slurm transport's programs; ``None`` starts them with ``processes``."""
    state_stores: StateStoreFactory | None = None
    """Opens each run's ``StateStore``; ``None`` is the local crash-atomic one."""
    profiler_preflight: ProfilerPreflight | None = None
    """Checks native profiler availability; ``None`` probes the host through ``processes``."""


def create_session(
    request: RunRequest,
    *,
    sink: EventSink,
    settings: LaunchSettings,
    registry: OrchestrationRegistry | None = None,
) -> RunSession:
    """Compose a session with built-ins or an explicitly supplied catalog."""
    selected_registry = _catalog(registry if registry is not None else settings.registry)
    timing = settings.timing
    client_factory = (
        partial(build_agent_client, threads=timing.threads)
        if settings.agent_client_factory is None
        else settings.agent_client_factory
    )
    backend_factory = (
        partial(
            create_compute_backend,
            docker=SubprocessDockerCli(
                processes=settings.processes, launcher=settings.piped_processes
            ),
            threads=timing.threads,
            launcher=settings.piped_processes,
            probe=ProcessesProbe(settings.processes),
        )
        if settings.backend_factory is None
        else settings.backend_factory
    )
    agents = BuiltInSessionAgents(client_factory) if settings.agents is None else settings.agents

    def publish(event: CoreEvent) -> None:
        # Pre-attachment observations have no durable journal identity yet.
        if not event.run_id and (request.run_id is not None or request.resume is not None):
            event = event.model_copy(update={"run_id": request.resolved_run_id})
        sink(event)

    return _create_session(
        request,
        sink=publish,
        registry=selected_registry,
        implementations=SessionImplementations(
            agent_client_factory=client_factory,
            backend_factory=backend_factory,
            profiler_preflight=(
                NativeProfilerPreflight(settings.processes)
                if settings.profiler_preflight is None
                else settings.profiler_preflight
            ),
            agents=agents,
            stop_timer=timing.clock.sleep if settings.stop_timer is None else settings.stop_timer,
            timing=timing,
            blocking=settings.blocking,
            network=settings.network,
            invocation_store_factory=(
                _durable_invocation_store
                if settings.invocation_store_factory is None
                else settings.invocation_store_factory
            ),
            stateless_tool_servers=STATELESS_TOOL_SERVERS,
            git=Git(settings.processes),
            github=GitHubCLI(settings.processes),
            git_repository=settings.git_repository,
            system=EnvironmentSystem(
                threads=timing.threads,
                network=settings.network,
                processes=settings.processes,
                launcher=settings.piped_processes,
                slurm_processes=(
                    settings.processes
                    if settings.slurm_processes is None
                    else settings.slurm_processes
                ),
            ),
            state_stores=settings.state_stores,
            agent_providers=tuple(sorted(SHIPPED_PROVIDERS)),
        ),
    )


def default_runs(settings: LaunchSettings) -> Runs:
    """Assemble the in-process launcher using the VibeSys built-in catalog.

    Config remains part of each RunRequest. LaunchSettings selects alternate
    implementations without changing the launch or frontend contracts.
    """
    registry = _catalog(settings.registry)

    def session_factory(request: RunRequest, sink: Callable[[CoreEvent], None]) -> RunSession:
        return create_session(
            request, sink=cast("EventSink", sink), registry=registry, settings=settings
        )

    def prepare(request: RunRequest) -> RunRequest:
        if request.resume is not None or request.run_id is not None:
            return request
        run_id = generate_run_id(request.resolved_run_id, now=utc_datetime(settings.timing.clock))
        return request.model_copy(update={"run_id": run_id})

    runs: InProcessRuns[RunRequest, CoreEvent, RunResult, RunSession] = InProcessRuns(
        session_factory,
        identity=lambda request: request.resolved_run_id,
        is_resume=lambda request: request.resume is not None,
        prepare=prepare,
    )
    return runs


def _durable_invocation_store(state: RunState, key: AgentSessionKey) -> AgentInvocationStore:
    """Open the run-owned invocation journal selected by the built-in catalog."""
    return state.local("agent").slot(
        f"invocations/{hashlib.sha256(str(key).encode()).hexdigest()}.json",
        AgentInvocationState,
    )


def _catalog(registry: OrchestrationRegistry | None) -> OrchestrationRegistry:
    return built_in_orchestrations() if registry is None else registry


def validate_descriptor(
    descriptor: OrchestrationDescriptor, *, registry: OrchestrationRegistry | None = None
) -> None:
    """Validate a built-in descriptor before provisioning any resources."""
    _validate_descriptor(descriptor, registry=_catalog(registry))


def validate_run_request(
    request: RunRequest, *, git: Git, registry: OrchestrationRegistry | None = None
) -> None:
    """Validate execution settings and the selected built-in policy; ``git`` starts Git."""
    _validate_run_request(request, registry=_catalog(registry), git=git)


def open_run_store(
    project: Project, *, git: Git, threads: Threads, registry: OrchestrationRegistry | None = None
) -> RunStore:
    """Open recorded runs with the built-in policy projections.

    ``git`` reads their history and ``threads`` makes the store's locks.
    """
    return _open_run_store(project, registry=_catalog(registry), git=git, threads=threads)


__all__ = [
    "LaunchSettings",
    "built_in_orchestrations",
    "create_session",
    "default_runs",
    "open_run_store",
    "validate_descriptor",
    "validate_run_request",
]
