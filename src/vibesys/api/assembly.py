"""Caller-selected implementations for transitional run assembly.

These contracts are for application wiring. Frontends receive RunHandle and
Runs instead of resource bundles or implementation constructors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vibesys.run.profilers import NativeProfilerPreflight, ProfilerPreflight

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
    from vs_github.api import GitHubCLI
    from vs_mcp.api import ToolServerDescriptor
    from vs_project.api import Git, GitRepositoryFactory, StateStoreFactory
    from vs_runtime.api.core import RunTiming
    from vs_runtime.api.infrastructure import (
        EnvironmentSystem,
        RunState,
        ScopedAgentEnvironment,
        StopTimer,
    )
    from vs_sandbox.api import ComputeBackendImpl, HostResource
    from vs_sim.api import BlockingRunner, Network


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
    blocking: BlockingRunner
    """Runs the run's synchronous effects off the event loop."""
    network: Network
    """Serves the run's agent tool sockets."""
    invocation_store_factory: Callable[[RunState, AgentSessionKey], AgentInvocationStore]
    git: Git
    """Starts the run's Git commands."""
    github: GitHubCLI
    """Runs the run's GitHub CLI commands."""
    system: EnvironmentSystem
    """The host's threads, sockets and processes the run's environments and evaluations run on."""
    profiler_preflight: ProfilerPreflight
    """Checks native profiler availability on the host."""
    stateless_tool_servers: Mapping[str, Callable[[object], ToolServerDescriptor | None]] | None = (
        None
    )
    """The server of each tool that holds no run state, which a core run may offer a role."""
    git_repository: GitRepositoryFactory | None = None
    """Builds the run's ``GitRepository`` implementations; ``None`` opens the configured one."""
    state_stores: StateStoreFactory | None = None
    """Opens each run's ``StateStore``; ``None`` is the local crash-atomic one."""


__all__ = [
    "NativeProfilerPreflight",
    "ProfilerPreflight",
    "SessionAgents",
    "SessionImplementations",
]
