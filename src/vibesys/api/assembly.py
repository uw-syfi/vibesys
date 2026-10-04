"""Caller-selected implementations for transitional run assembly.

These contracts are for application wiring. Frontends receive RunHandle and
Runs instead of resource bundles or implementation constructors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vibesys.api.auxiliary import AuxiliaryAgentDriver, AuxiliaryAgentLaunch, ManagedAgent
    from vibesys.run.integration import RunResources
    from vs_agent.api import (
        AgentClientProtocol,
        AgentEventSink,
        AgentInvocationStore,
        AgentSessionKey,
        ToolServerDescriptor,
    )
    from vs_runtime.api import AgentToolBindingContext
    from vs_runtime.api.infrastructure import RunState, ScopedAgentEnvironment, StopTimer
    from vs_sandbox.api import ComputeBackendImpl, HostResource


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
    agent_drivers: tuple[AuxiliaryAgentDriver, ...]
    stop_timer: StopTimer
    invocation_store_factory: Callable[[RunState, AgentSessionKey], AgentInvocationStore]
    agent_tool_bindings: (
        Mapping[str, Callable[[object, AgentToolBindingContext], tuple[ToolServerDescriptor, ...]]]
        | None
    ) = None


__all__ = ["SessionAgents", "SessionImplementations"]
