"""Pure proofs about an attempt's invocations: which one is terminal, which sessions it retains.

Split from ``_attempt_acquisition`` to keep that module under the file-length ceiling; the
functions read the sessions and records states and decide nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._proofs import (
    Mismatch,
    Missing,
    ProofField,
    ProofReason,
    Proven,
    Verdict,
    invocation_for,
    observation_for,
)
from .types.attempts import EnsureWorkspace
from .types.common import (
    AttemptRef,
    DecisionId,
    ExecuteRegisteredOperation,
    Observation,
    ObservationStatus,
    RequestBase,
    RunId,
    Scope,
)
from .types.records import (
    RecordsState,
    RequestPhase,
)
from .types.sessions import (
    DispatchTurn,
    EnsureSession,
    InspectTurn,
    ResumeSessionTurn,
    SessionsState,
)

if TYPE_CHECKING:
    from .types.attempts import AttemptView
    from .types.evaluation import Continuation
    from .types.records import RequestRecord
    from .types.sessions import Invocation, SessionView


def vacant_workspace_request(record: RequestRecord | None) -> bool:
    """A workspace request that was answered terminal without taking anything.

    A request the executor rejected, failed, or cancelled before accepting it created no
    workspace resource, so there is nothing for a closing attempt to wait to see released.
    The executor marks such an answer neither ``released`` nor ``children_complete`` (there is
    nothing to release and no manifest), so ``_released`` can never hold for it.
    """
    if record is None or not isinstance(record.request, EnsureWorkspace):
        return False
    observation = record.observation
    return bool(
        observation is not None
        and observation.terminal
        and not observation.accepted
        and observation.resource_id is None
        and not observation.children
        and observation.status
        in (ObservationStatus.REJECTED, ObservationStatus.FAILED, ObservationStatus.CANCELLED)
    )


def identity_mismatch(checks: tuple[tuple[ProofField, object, object], ...]) -> Mismatch | None:
    ordered = sorted(checks, key=lambda check: tuple(ProofField).index(check[0]))
    return next(
        (Mismatch(field) for field, actual, expected in ordered if actual != expected), None
    )


def _terminal_lease(invocation: Invocation, session: SessionView) -> Mismatch | None:
    ref = invocation.invocation
    return identity_mismatch(
        (
            (ProofField.INVOCATION_ID, invocation.turn.invocation_id, ref.invocation_id),
            (ProofField.SESSION_ID, session.spec.session_id, ref.session_id),
            (ProofField.PAYLOAD, session.spec, invocation.turn.session),
            (
                ProofField.RESOURCE_ID,
                invocation.observation.resource_id
                if invocation.observation is not None
                and invocation.observation.resource_id is not None
                else session.resource_id,
                session.resource_id,
            ),
            (ProofField.GENERATION, session.generation, ref.generation),
        )
    )


def request_turn_matches(invocation: Invocation, request: RequestBase) -> bool:
    if isinstance(request, DispatchTurn | ResumeSessionTurn):
        return request.turn == invocation.turn
    if isinstance(request, InspectTurn):
        return request.invocation == invocation.invocation
    return (
        isinstance(request, ExecuteRegisteredOperation)
        and invocation.registered_operation is not None
        and request.operation_id == invocation.registered_operation
    )


def _terminal_request(
    invocation: Invocation, records: RecordsState, observation: Observation, episode: DecisionId
) -> Verdict[Observation]:
    rows = records.find(observation.request_id)
    if not rows:
        return Missing(ProofReason.ABSENT_REQUEST)
    if len(rows) != 1:
        return Mismatch(ProofField.REQUEST_ID)
    record = rows[0]
    if record.observation is not None and record.observation != observation:
        return Mismatch(ProofField.PAYLOAD)
    proof = observation_for(record, observation)
    if not isinstance(proof, Proven):
        return proof
    matches = request_turn_matches(invocation, record.request)
    mismatch = identity_mismatch(
        (
            (ProofField.SCOPE, observation.scope, invocation.scope),
            (ProofField.ADMISSION_ID, observation.admission_id, episode),
            (ProofField.PAYLOAD, matches, True),
        )
    )
    return mismatch if mismatch is not None else Proven(observation)


def exact_invocation_terminal(
    invocation: Invocation | None,
    session: SessionView | None,
    records: RecordsState,
    episode: DecisionId | None,
) -> Verdict[Observation]:
    if invocation is None or session is None:
        return Missing(
            ProofReason.ABSENT_INVOCATION if invocation is None else ProofReason.ABSENT_SESSION
        )
    if episode is None:
        return Missing(ProofReason.ABSENT_EPISODE)
    observation = invocation.observation
    if observation is None:
        return Missing(ProofReason.ABSENT_OBSERVATION)
    if observation.accepted and session.resource_id is None:
        return Missing(ProofReason.ABSENT_RESOURCE)
    mismatch = _terminal_lease(invocation, session)
    if mismatch is not None:
        return mismatch
    proof = _terminal_request(invocation, records, observation, episode)
    return _resolved_terminal(proof)


def _resolved_terminal(proof: Verdict[Observation]) -> Verdict[Observation]:
    if not isinstance(proof, Proven):
        return proof
    observation = proof.value
    if (
        not observation.terminal
        or observation.status == ObservationStatus.PENDING
        or unresolved_acceptance(observation)
    ):
        return Missing(ProofReason.UNRESOLVED)
    return proof


def _retained_session(
    attempt: AttemptView, continuation: Continuation, rows: tuple[SessionView, ...]
) -> Verdict[SessionView]:
    if not rows:
        return Missing(ProofReason.ABSENT_SESSION)
    if len(rows) != 1:
        return Mismatch(ProofField.SESSION_ID)
    row = rows[0]
    if row.resource_id is None:
        return Missing(ProofReason.ABSENT_RESOURCE)
    if (
        row.spec.session_id == continuation.invocation.session_id
        and row.generation != continuation.invocation.generation
    ):
        return Mismatch(ProofField.GENERATION)
    if row.scope != attempt_scope(attempt) and (
        not isinstance(row.scope.owner, RunId)
        or row.spec.policy != "reuse"
        or row.spec.lifetime != "owner"
    ):
        return Mismatch(ProofField.SCOPE)
    mismatch = identity_mismatch(
        (
            (
                ProofField.SESSION_ID,
                continuation.invocation.session_id,
                continuation.next_invocation.session_id,
            ),
            (
                ProofField.GENERATION,
                continuation.invocation.generation,
                continuation.next_invocation.generation,
            ),
        )
    )
    return mismatch if mismatch is not None else Proven(row)


def retained_acquisition(
    attempt: AttemptView, session: SessionView, sessions: SessionsState, records: RecordsState
) -> bool:
    if not any(
        group.attempt == attempt_ref(attempt)
        and group.admission_id == attempt.admission_id
        and group.phase == "ready"
        and session.spec.session_id in group.session_ids
        for group in sessions.acquisition_groups
    ):
        return False
    return any(
        isinstance(row.request, EnsureSession)
        and row.request.spec == session.spec
        and row.request.required_resource == session.resource_id
        and row.request.scope == session.scope
        and row.request.admission_id == attempt.admission_id
        and row.phase == RequestPhase.COMPLETED
        and isinstance(observation_for(row, row.observation), Proven)
        and row.observation is not None
        and row.observation.resource_id == session.resource_id
        and successful(row.observation)
        for row in records.records
    )


def retained_sessions_for(
    attempt: AttemptView | None,
    continuation: Continuation | None,
    sessions: SessionsState,
    records: RecordsState,
) -> Verdict[tuple[SessionView, ...]]:
    if attempt is None or continuation is None:
        return Missing(ProofReason.ABSENT_INVOCATION)
    source = invocation_for(sessions.invocations, continuation.invocation, attempt_scope(attempt))
    if not isinstance(source, Proven):
        return source
    required = tuple(dict.fromkeys((*attempt.sessions, continuation.invocation.session_id)))
    matched: list[SessionView] = []
    for identity in required:
        rows = tuple(row for row in sessions.sessions if row.spec.session_id == identity)
        proof = _retained_session(attempt, continuation, rows)
        if not isinstance(proof, Proven):
            return proof
        row = proof.value
        if identity == continuation.invocation.session_id and (
            row.spec != source.value.turn.session
            or (
                row.invocation != continuation.invocation.invocation_id
                and (
                    row.invocation is not None
                    or not retained_acquisition(attempt, row, sessions, records)
                )
            )
        ):
            return Mismatch(ProofField.INVOCATION_ID)
        matched.append(row)
    return Proven(tuple(matched))


def attempt_ref(attempt: AttemptView) -> AttemptRef:
    return AttemptRef(attempt_id=attempt.attempt_id, generation=attempt.generation)


def attempt_scope(attempt: AttemptView) -> Scope:
    return Scope(owner=attempt.attempt_id, generation=attempt.generation)


def unresolved_acceptance(observation: Observation) -> bool:
    return observation.status == ObservationStatus.UNKNOWN or (
        observation.status == ObservationStatus.SUCCEEDED and not observation.accepted
    )


def successful(observation: Observation) -> bool:
    return (
        observation.status == ObservationStatus.SUCCEEDED
        and observation.accepted
        and observation.terminal
    )
