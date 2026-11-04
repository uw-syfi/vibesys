"""Lookups and replacements over the sessions state that every session transition shares."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._deadlines import cleanup, limit_to
from ._session_scope import (
    attempt_for,
    encode_identity,
)
from .types.common import (
    DecisionId,
    RequestId,
    Scope,
)
from .types.records import (
    InspectRequest,
    Outbound,
    within,
)
from .types.sessions import (
    CloseSession,
    EnsureSession,
    Invocation,
    SessionsState,
    SessionView,
)

if TYPE_CHECKING:
    from .types.kernel import SessionsContext
    from .types.records import Request, RequestRecord


def _session_id(session: SessionView, action: str) -> RequestId:
    parts = (
        session.scope.owner.kind,
        session.scope.owner.root,
        str(session.scope.generation),
        str(session.generation),
        session.spec.session_id.root,
        action,
    )
    return RequestId(root=encode_identity("session", *parts))


def _setup(context: SessionsContext, request: Request) -> Outbound:
    """Session setup may take the time the run has left."""
    return within(request, limit_to(context.run, context.run.deadline_at))


def _cleanup(context: SessionsContext, request: Request) -> Outbound:
    """Session close and inspection get core's bounded cleanup time, even after the run ends."""
    return cleanup(context.run, request)


def _reconciling(context: SessionsContext, request: Request) -> Outbound:
    """An inspection gets the reconciliation bound."""
    return within(request, context.run.limits.reconciliation_bound)


def _record(context: SessionsContext, identity: RequestId | None) -> RequestRecord | None:
    return context.records.record(identity)


def _episode(context: SessionsContext, scope: Scope) -> DecisionId | None:
    owner = attempt_for(context, scope)
    return owner.admission_id if owner is not None else None


def _ensure(session: SessionView, context: SessionsContext) -> EnsureSession:
    episode = _episode(context, session.scope)
    action = "ensure" if episode is None else f"ensure:{episode.root}"
    return EnsureSession(
        request_id=_session_id(session, action),
        scope=session.scope,
        admission_id=_episode(context, session.scope),
        spec=session.spec,
        required_resource=session.resource_id,
    )


def _run_reusable(session: SessionView, context: SessionsContext) -> bool:
    return (
        session.scope.owner == context.run.run_id
        and session.scope.generation == context.run.generation
        and session.spec.policy == "reuse"
        and session.spec.lifetime == "owner"
    )


def _current_invocation(state: SessionsState, session: SessionView) -> Invocation | None:
    return next(
        (
            row
            for row in state.invocations
            if row.invocation.session_id == session.spec.session_id
            and row.invocation.invocation_id == session.invocation
            and row.invocation.generation == session.generation
        ),
        None,
    )


def _close(session: SessionView, context: SessionsContext, authority: RequestId) -> CloseSession:
    return CloseSession(
        request_id=_session_id(session, f"close:{authority.root}"),
        scope=session.scope,
        admission_id=_episode(context, session.scope),
        session_id=session.spec.session_id,
        resource_id=session.resource_id,
        episode=_episode(context, session.scope),
    )


def _inspect_session(
    session: SessionView, context: SessionsContext, target: RequestId
) -> InspectRequest:
    # One inspection per host epoch: a later epoch asks afresh. An answer a dead host's
    # inspection gave (or never gave) says nothing new to the restarted host, and the same
    # identity would only hand back that ended request.
    epoch = str(context.records.epoch)
    identity = RequestId(root=encode_identity("inspect-root", epoch, target.root))
    previous = _record(context, identity)
    if previous is not None and isinstance(previous.request, InspectRequest):
        return previous.request
    original = _record(context, target)
    return InspectRequest(
        request_id=identity,
        scope=session.scope,
        admission_id=original.request.admission_id
        if original is not None
        else _episode(context, session.scope),
        target=target,
        resource_id=None,
    )
