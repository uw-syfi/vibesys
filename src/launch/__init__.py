"""VibeSys built-in catalog and application wiring.

Only entrypoints, tests and scripts select these defaults. Core and frontends
receive the resulting contracts and never import this package.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from launch.agents import BuiltInSessionAgents
from launch.catalog import built_in_orchestrations
from launch.composition import AGENT_TOOL_BINDINGS
from vibesys.api import AuxiliaryAgentDriver
from vibesys.api.request import validate_descriptor as _validate_descriptor
from vibesys.api.request import validate_run_request as _validate_run_request
from vibesys.api.store import open_run_store as _open_run_store
from vibesys.api.wiring import SessionImplementations
from vibesys.api.wiring import create_session as _create_session
from vs_agent.api import AgentInvocationState, agent_catalog, build_agent_client
from vs_project.api import generate_run_id
from vs_runtime.api.wiring import InProcessRuns
from vs_sandbox.api import create_compute_backend

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
    from vibesys.api.wiring import SessionAgents
    from vs_agent.api import AgentClientProtocol, AgentInvocationStore, AgentSessionKey
    from vs_project.api import Project
    from vs_runtime.api.infrastructure import RunState, StopTimer
    from vs_sandbox.api import ComputeBackendImpl


@dataclass(frozen=True, slots=True)
class LaunchSettings:
    """Implementations selected once at the application wiring boundary."""

    registry: OrchestrationRegistry | None = None
    agent_client_factory: Callable[..., AgentClientProtocol] | None = None
    backend_factory: Callable[..., ComputeBackendImpl] | None = None
    stop_timer: StopTimer = asyncio.sleep
    agents: SessionAgents | None = None
    invocation_store_factory: Callable[[RunState, AgentSessionKey], AgentInvocationStore] | None = (
        None
    )


def create_session(
    request: RunRequest,
    *,
    sink: EventSink,
    registry: OrchestrationRegistry | None = None,
    settings: LaunchSettings | None = None,
) -> RunSession:
    """Compose a session with built-ins or an explicitly supplied catalog."""
    selected = LaunchSettings() if settings is None else settings
    selected_registry = _catalog(registry if registry is not None else selected.registry)
    client_factory = (
        build_agent_client
        if selected.agent_client_factory is None
        else selected.agent_client_factory
    )
    backend_factory = (
        create_compute_backend if selected.backend_factory is None else selected.backend_factory
    )
    agents = BuiltInSessionAgents(client_factory) if selected.agents is None else selected.agents

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
            agents=agents,
            stop_timer=selected.stop_timer,
            invocation_store_factory=(
                _durable_invocation_store
                if selected.invocation_store_factory is None
                else selected.invocation_store_factory
            ),
            agent_tool_bindings=AGENT_TOOL_BINDINGS,
            agent_drivers=tuple(
                AuxiliaryAgentDriver(driver=info.driver.value, providers=info.providers)
                for info in agent_catalog().values()
            ),
        ),
    )


def default_runs(settings: LaunchSettings | None = None) -> Runs:
    """Assemble the in-process launcher using the VibeSys built-in catalog.

    Config remains part of each RunRequest. LaunchSettings selects alternate
    implementations without changing the launch or frontend contracts.
    """
    selected = LaunchSettings() if settings is None else settings
    registry = _catalog(selected.registry)

    def session_factory(request: RunRequest, sink: Callable[[CoreEvent], None]) -> RunSession:
        return create_session(
            request, sink=cast("EventSink", sink), registry=registry, settings=selected
        )

    def prepare(request: RunRequest) -> RunRequest:
        if request.resume is not None or request.run_id is not None:
            return request
        return request.model_copy(update={"run_id": generate_run_id(request.resolved_run_id)})

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
    request: RunRequest, *, registry: OrchestrationRegistry | None = None
) -> None:
    """Validate execution settings and the selected built-in policy."""
    _validate_run_request(request, registry=_catalog(registry))


def open_run_store(project: Project, *, registry: OrchestrationRegistry | None = None) -> RunStore:
    """Open recorded runs with the built-in policy projections."""
    return _open_run_store(project, registry=_catalog(registry))


__all__ = [
    "LaunchSettings",
    "built_in_orchestrations",
    "create_session",
    "default_runs",
    "open_run_store",
    "validate_descriptor",
    "validate_run_request",
]
