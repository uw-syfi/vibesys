"""A session world that also runs CancelTurn, CloseSession and ResumeSessionTurn.

It reuses ``session_world`` (real disk, the Fake driver, one durable journal) and
only adds a client that records which keys were cancelled and released, so a test
can count the physical effect of a lifecycle request.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tests.support.session_world import (
    SCOPE,
    SESSION,
    FakeSessionResolver,
    ProviderFaults,
    SessionHost,
    turn_spec,
)

from vs_agent.api import AgentClient, AgentSessionKey, AgentTurnRequest
from vs_agent.api.testing import FakeAgentInvocationStore, FakeAgentSessions, FakeDriver
from vs_core.api import (
    CancelTurn,
    CloseSession,
    ContinuationId,
    InvocationId,
    InvocationRef,
    RequestId,
    ResumeSessionTurn,
)
from vs_prompts.api import TemplateRenderer
from vs_runtime.api.core import (
    ReceiptStore,
    RuntimeSessionRequests,
    SessionLifecycleRequests,
    SessionRequestRouter,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_agent.api import SessionStore
    from vs_runtime.api.core import SessionResolver

CONTINUATION = ContinuationId(root="continuation-1")


class RecordingClient(AgentClient):
    """The real client, plus the keys it was asked to cancel and release."""

    def __init__(self, driver: FakeDriver, store: SessionStore) -> None:
        """Wrap the Fake driver; *store* keeps the checkpoint across a release."""
        super().__init__(driver, session_store=store)
        self.cancelled: list[AgentSessionKey] = []
        self.released: list[AgentSessionKey] = []

    def cancel_session(self, key: AgentSessionKey) -> None:
        """Record, then cancel."""
        self.cancelled.append(key)
        super().cancel_session(key)

    def release_session(self, key: AgentSessionKey) -> None:
        """Record, then release."""
        self.released.append(key)
        super().release_session(key)


@dataclass
class TurnGate:
    """Holds a provider turn in flight: it signals ``started``, then waits for ``proceed``."""

    started: threading.Event = field(default_factory=threading.Event)
    proceed: threading.Event = field(default_factory=threading.Event)

    def hold(self) -> None:
        """Called on the provider thread when a turn arrives."""
        self.started.set()
        self.proceed.wait()


def open_lifecycle_host(
    workspace: Path,
    store: SessionStore,
    *,
    answer: dict[str, object] | None = None,
    gate: TurnGate | None = None,
) -> SessionHost:
    """Like ``open_host`` but over a ``RecordingClient`` with a durable checkpoint store."""
    turns: list[AgentTurnRequest] = []
    faults = ProviderFaults()

    def on_turn(request: AgentTurnRequest) -> None:
        turns.append(request)
        if gate is not None:
            gate.hold()
        if faults.down:
            message = "provider died after accepting the turn"
            raise ConnectionError(message)

    client = RecordingClient(FakeDriver(answer=answer or {"value": 7}, on_turn=on_turn), store)
    return SessionHost(
        FakeSessionResolver(workspace, TemplateRenderer(workspace)),
        client,
        FakeAgentInvocationStore(),
        turns,
        faults,
    )


def lifecycle_executor(
    host: SessionHost, store: ReceiptStore, sessions: FakeAgentSessions | None = None
) -> SessionRequestRouter:
    """A SESSIONS executor over the host's durable pieces.

    By default a freshly started host, which forgets which turns it was running;
    pass *sessions* to keep one process alive across calls.
    """
    resolver: SessionResolver = host.resolver
    sessions = sessions or FakeAgentSessions(host.client, host.journal)
    turns = RuntimeSessionRequests(sessions, resolver, store)
    return SessionRequestRouter(turns, SessionLifecycleRequests(sessions, turns, store))


def released_keys(host: SessionHost) -> int:
    """Distinct conversation keys released (releasing twice is one physical state)."""
    assert isinstance(host.client, RecordingClient)
    return len(set(host.client.released))


def cancelled_keys(host: SessionHost) -> int:
    """Distinct conversation keys a cancellation reached."""
    assert isinstance(host.client, RecordingClient)
    return len(set(host.client.cancelled))


def cancel_request(request_id: str = "req-cancel", invocation: str = "inv-1") -> CancelTurn:
    """A CancelTurn of one invocation of the default session."""
    return CancelTurn(
        request_id=RequestId(root=request_id),
        scope=SCOPE,
        deadline_at=100.0,
        invocation=InvocationRef(
            session_id=SESSION, invocation_id=InvocationId(root=invocation), generation=0
        ),
    )


def close_request(request_id: str = "req-close") -> CloseSession:
    """A CloseSession of the default session."""
    return CloseSession(
        request_id=RequestId(root=request_id),
        scope=SCOPE,
        deadline_at=100.0,
        session_id=SESSION,
    )


def resume_request(
    request_id: str = "req-resume",
    invocation: str = "inv-2",
    continuation: ContinuationId = CONTINUATION,
) -> ResumeSessionTurn:
    """A ResumeSessionTurn that continues the default session with *invocation*."""
    turn = turn_spec(invocation).model_copy(
        update={"charge_class": "resume", "continuation_id": continuation}
    )
    return ResumeSessionTurn(
        request_id=RequestId(root=request_id),
        scope=SCOPE,
        deadline_at=100.0,
        turn=turn,
        continuation_id=continuation,
    )
