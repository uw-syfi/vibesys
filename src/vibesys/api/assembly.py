"""Caller-selected implementations for transitional run assembly.

These contracts are for application wiring. Frontends receive RunHandle and
Runs instead of resource bundles or implementation constructors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys.api.auxiliary import AuxiliaryAgentLaunch, ManagedAgent
    from vibesys.run.integration import RunResources
    from vs_agent.api import (
        AgentClientProtocol,
        AgentEventSink,
        AgentInvocationStore,
        AgentSessionKey,
    )
    from vs_mcp.api import ToolServerDescriptor
    from vs_project.api import GitRepositoryFactory, StateStoreFactory
    from vs_runtime.api import AgentToolBindingContext
    from vs_runtime.api.core import RunTiming
    from vs_runtime.api.infrastructure import RunState, ScopedAgentEnvironment, StopTimer
    from vs_sandbox.api import ComputeBackendImpl, HostResource
    from vs_sim.api import Threads
    from vs_slurm.api import SlurmProcess


class SessionAgents(Protocol):
    """Construct scoped execution environments and auxiliary conversations.

    The caller owns each returned resource and its close operation. Failed
    construction closes any environment already acquired.
    """

    def open_environment(
        self,
        resources: RunResources,
        *,
        mounts: tuple[HostResource, ...] = (),
        agent_backend: str | None = None,
        cli_provider: str | None = None,
    ) -> ScopedAgentEnvironment:
        """Open a scoped environment over a run's acquired resources."""
        ...

    def create_agent(
        self,
        launch: AuxiliaryAgentLaunch,
        resources: RunResources,
        agent_events: AgentEventSink,
    ) -> ManagedAgent:
        """Open one conversation and transfer its resource ownership."""
        ...


@dataclass(frozen=True, slots=True)
class SessionImplementations:
    """Execution implementations supplied explicitly by application wiring."""

    agent_client_factory: Callable[..., AgentClientProtocol]
    backend_factory: Callable[..., ComputeBackendImpl]
    agents: SessionAgents
    agent_providers: tuple[str, ...]
    stop_timer: StopTimer
    timing: RunTiming
    invocation_store_factory: Callable[[RunState, AgentSessionKey], AgentInvocationStore]
    agent_tool_bindings: (
        Mapping[str, Callable[[object, AgentToolBindingContext], tuple[ToolServerDescriptor, ...]]]
        | None
    ) = None
    git_repository: GitRepositoryFactory | None = None
    """Builds the run's ``GitRepository`` implementations; ``None`` runs the Git CLI."""
    threads: Threads | None = None
    """Locks for the session's shared state; ``None`` uses the operating system's."""
    slurm_process: SlurmProcess | None = None
    """Replaces the Slurm transport's process boundary; ``None`` runs the configured programs."""
    state_stores: StateStoreFactory | None = None
    """Opens each run's ``StateStore``; ``None`` is the local crash-atomic one."""


__all__ = ["SessionAgents", "SessionImplementations"]
