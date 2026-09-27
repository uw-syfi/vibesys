"""Explicit effect injection for tests exercising the product run session.

This module keeps fake composition out of the application-facing
``vibesys.api.create_session`` contract. Tests still exercise the same session,
persistence, event, and cleanup lifecycle as product callers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.api.session import RunSession, _create_session

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.api.contracts import EventSink
    from vibesys.orchestration.contracts import OrchestrationRegistry
    from vibesys.orchestration.request import RunRequest
    from vs_agent.api import AgentClientProtocol
    from vs_sandbox.api import ComputeBackendImpl


def create_session(
    request: RunRequest,
    *,
    sink: EventSink,
    registry: OrchestrationRegistry,
    agent_client_factory: Callable[..., AgentClientProtocol],
    backend_factory: Callable[..., ComputeBackendImpl],
) -> RunSession:
    """Build the product session with caller-owned fake effect factories."""
    return _create_session(
        request,
        sink=sink,
        registry=registry,
        agent_client_factory=agent_client_factory,
        backend_factory=backend_factory,
    )


__all__ = ["create_session"]
