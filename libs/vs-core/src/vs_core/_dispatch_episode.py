"""Whether a prepared request still belongs to the episode that may dispatch it.

A request carries the admission of the attempt scope that prepared it. Dispatch authority
is checked against that scope when the shell asks to start the request, and again by the
wait check, so a request whose episode ended (the attempt closed, a stop retired its scope)
is recognised as one that can never be dispatched. Both read the one predicate here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ._proofs import Proven, accepted_receipt_for, committed_stop, current_admission
from .types.attempts import (
    AttemptPhase,
    CloseAttemptScope,
    DiscardWorkspace,
    RetainRevision,
    SnapshotAndRetain,
)
from .types.common import (
    AttemptId,
    AttemptRef,
    InvocationRef,
    LifecycleClass,
    ObservationStatus,
    OperationId,
    RequestId,
    Scope,
    SessionId,
)
from .types.evaluation import CancelOwnedJob, InspectOwnedJob, ObserveOwnedJob
from .types.records import (
    BlockRequest,
    CancelOwnedResource,
    ExecuteRegisteredOperation,
    InspectRequest,
    RequestPhase,
    request_lifecycle,
)
from .types.sessions import CancelTurn, CloseSession, InspectTurn
from .types.strategy import Finish, StartAttempt, Withdraw

if TYPE_CHECKING:
    from .types.attempts import AttemptsState
    from .types.common import DecisionId
    from .types.kernel import RunState
    from .types.records import RecordsState, Request
    from .types.sessions import SessionsState


class EpisodeFacts(Protocol):
    """The slices of core state that decide an episode.

    A ``CoreState`` is one, and so is an
    area reducer's context joined with the records it is changing.
    """

    @property
    def run(self) -> RunState:
        """The run's receipts and committed stop."""
        ...

    @property
    def attempts(self) -> AttemptsState:
        """The attempts, whose phase and admission bound an episode."""
        ...

    @property
    def sessions(self) -> SessionsState:
        """The sessions and invocations a retirement may target."""
        ...

    @property
    def records(self) -> RecordsState:
        """The recorded requests a retirement may target."""
        ...


def scope_admission(state: EpisodeFacts, scope: Scope | None) -> DecisionId | None:
    """Separately proposed work derives authority from its exact current owner."""
    if scope is None or not isinstance(scope.owner, AttemptId):
        return None
    owner = next(
        (
            attempt
            for attempt in state.attempts.attempts
            if attempt.attempt_id == scope.owner and attempt.generation == scope.generation
        ),
        None,
    )
    return owner.admission_id if owner is not None else None


def retirement_dispatch(request: Request) -> bool:
    """Recorded inspection and release requests retain their original lease authority."""
    return isinstance(
        request,
        InspectRequest
        | InspectTurn
        | InspectOwnedJob
        | ObserveOwnedJob
        | CancelOwnedResource
        | CancelTurn
        | CancelOwnedJob
        | BlockRequest,
    )


def _episode_recorded(state: EpisodeFacts, request: Request) -> bool:
    """Admission history binds cleanup authority to its exact attempt generation."""
    if not isinstance(request.scope.owner, AttemptId):
        return True
    target = AttemptRef(attempt_id=request.scope.owner, generation=request.scope.generation)
    proof = accepted_receipt_for(state.run.receipts, request.admission_id, None)
    if not isinstance(proof, Proven):
        return False
    decision = proof.value.decision
    return (
        isinstance(decision, StartAttempt)
        and decision.scope.owner == state.run.run_id
        and decision.attempt_id == target.attempt_id
        and decision.scope.generation == target.generation
    )


def _retirement_target_matches(
    state: EpisodeFacts, request: Request, decision: Withdraw | Finish
) -> bool:
    if not _episode_recorded(state, request):
        return False
    if isinstance(decision, Finish):
        stop = committed_stop(state.run)
        return isinstance(stop, Proven) and stop.value == decision
    target = decision.target
    if isinstance(target, AttemptRef):
        return request.scope == Scope(owner=target.attempt_id, generation=target.generation) and (
            not isinstance(
                request, CloseAttemptScope | DiscardWorkspace | RetainRevision | SnapshotAndRetain
            )
            or request.attempt == target
        )
    if isinstance(target, InvocationRef):
        return any(
            invocation.invocation == target and invocation.scope == request.scope
            for invocation in state.sessions.invocations
        )
    return any(
        isinstance(record.request, ExecuteRegisteredOperation)
        and record.request.operation_id == target.operation_id
        and record.request.scope == request.scope
        for record in state.records.records
    )


def _session_retirement_matches(state: EpisodeFacts, request: CloseSession) -> bool:
    """A reusable conversation cannot be closed by a previous admission's request."""
    if (
        isinstance(request.scope.owner, AttemptId)
        and scope_admission(state, request.scope) != request.admission_id
    ):
        return False
    return any(
        session.spec.session_id == request.session_id and session.scope == request.scope
        for session in state.sessions.sessions
    )


def _closure_retirement(state: EpisodeFacts, request: Request) -> bool:
    """Settlement and setup cleanup use recorded closure and exact release edges."""
    if not isinstance(request.scope.owner, AttemptId):
        return False
    target = AttemptRef(attempt_id=request.scope.owner, generation=request.scope.generation)
    owner = next(
        (
            item
            for item in state.attempts.attempts
            if item.attempt_id == target.attempt_id and item.generation == target.generation
        ),
        None,
    )
    if owner is None or owner.closure is None or owner.closure.admission_id != request.admission_id:
        return False
    if (
        isinstance(
            request, CloseAttemptScope | DiscardWorkspace | RetainRevision | SnapshotAndRetain
        )
        and request.attempt != target
    ):
        return False
    identities: set[RequestId | SessionId | OperationId | None] = {request.request_id}
    if isinstance(request, CloseSession):
        identities.add(request.session_id)
    if isinstance(request, ExecuteRegisteredOperation):
        identities.add(request.operation_id)
    return request.request_id == owner.closure.authority or any(
        edge.identity in identities for edge in owner.release_dependencies
    )


def registered_retirement(state: EpisodeFacts, request: Request) -> bool:
    """Reusable scope mutation needs canonical retirement and its recorded episode."""
    if not isinstance(
        request,
        ExecuteRegisteredOperation
        | CloseAttemptScope
        | CloseSession
        | DiscardWorkspace
        | RetainRevision
        | SnapshotAndRetain,
    ):
        return False
    if (
        isinstance(request, ExecuteRegisteredOperation)
        and request.operation.schema_ref.lifecycle != LifecycleClass.IDEMPOTENT_WRITE
    ):
        return False
    authority = accepted_receipt_for(state.run.receipts, request.decision_id, None)
    decision_authority = (
        isinstance(authority, Proven)
        and isinstance(authority.value.decision, Withdraw | Finish)
        and _retirement_target_matches(state, request, authority.value.decision)
    )
    return (
        (decision_authority or _closure_retirement(state, request))
        and (
            not isinstance(
                request,
                ExecuteRegisteredOperation | SnapshotAndRetain | DiscardWorkspace | CloseSession,
            )
            or not isinstance(request.scope.owner, AttemptId)
            or scope_admission(state, request.scope) == request.admission_id
        )
        and (not isinstance(request, CloseSession) or _session_retirement_matches(state, request))
    )


def dispatch_episode_current(state: EpisodeFacts, request: Request) -> bool:
    """Whether dispatching this prepared request would still act in its own episode.

    A prepared ordinary mutation never gains authority over a later episode: its attempt
    must still be acquiring or active under the admission that prepared it. Queries and
    retirement (cleanup) requests keep the authority they were prepared with. Once this is
    False it stays False, so the request can never be dispatched.
    """
    if not isinstance(request.scope.owner, AttemptId):
        return True
    if (
        request_lifecycle(request) == LifecycleClass.QUERY
        or retirement_dispatch(request)
        or registered_retirement(state, request)
    ):
        return True
    owner = next(
        (
            attempt
            for attempt in state.attempts.attempts
            if attempt.attempt_id == request.scope.owner
            and attempt.generation == request.scope.generation
        ),
        None,
    )
    return (
        owner is not None
        and isinstance(current_admission(owner, request.scope, request.admission_id), Proven)
        and owner.phase in (AttemptPhase.ACQUIRING, AttemptPhase.ACTIVE)
    )


_CLEANUP_KINDS = (
    CloseAttemptScope,
    CloseSession,
    DiscardWorkspace,
    RetainRevision,
    SnapshotAndRetain,
)


def episode_ended(state: EpisodeFacts, request: Request) -> bool:
    """Whether the episode that prepared this request is over for good.

    Only an ordinary mutation can end this way: its attempt left acquiring and active, or
    a later admission replaced its own. Cleanup kinds are left out because their authority
    is recorded after they are prepared (a release edge, a committed stop), so a refusal to
    dispatch one now says nothing about later. Such a request is never reported as ended.
    """
    if isinstance(request, InspectRequest):
        return _inspection_moot(state, request)
    return not isinstance(request, _CLEANUP_KINDS) and not dispatch_episode_current(state, request)


def _inspection_moot(state: EpisodeFacts, request: InspectRequest) -> bool:
    """Whether the request inspected was closed unsent, so there is nothing to find out.

    Core ends a prepared request of an ended episode itself (provably unaccepted). An
    inspection prepared meanwhile would report a second, conflicting first observation.
    """
    target = state.records.record(request.target)
    observation = None if target is None else target.observation
    return (
        target is not None
        and target.phase == RequestPhase.COMPLETED
        and observation is not None
        and observation.status == ObservationStatus.CANCELLED
        and not observation.accepted
        and observation.terminal
        and observation.released
    )
