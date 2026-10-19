"""Pure session lease, invocation and all-role TURN accounting transitions.

Owns leases, invocations and run charges; inputs and interruptions are read-only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import _session_checkpoints, _turn_holds
from ._acquisition_stall import stalled_acquisition
from ._continuations import bounded_wait, wait_refused_at_turn_end
from ._proofs import (
    Proven,
    committed_stop,
    current_admission,
    current_closure,
    fresh_observation,
    observation_for,
    request_matches,
    resolved_observation,
)
from ._session_admission import (
    _admit_turn,
    _charged,
    _charges_authorized,
    _dispatch_reserved,
    _registered_request,
)
from ._session_checkpoints import turn_source_matches as _turn_proof
from ._session_rows import awaiting as _awaiting
from ._session_rows import awaiting_checkpoint as _awaiting_checkpoint
from ._session_rows import invocation_row as _invocation
from ._session_rows import replace_invocation as _replace_invocation
from ._session_rows import replace_session as _replace_session
from ._session_rows import session_row as _session
from ._session_scope import (
    attempt_for,
    inspect_turn,
    lost_turn_authority,
    retains_write_turn,
    scope_active,
    turn_request_id,
    write_turn_authority,
    yield_authority,
)
from ._session_state import (
    _cleanup,
    _close,
    _current_invocation,
    _ensure,
    _episode,
    _inspect_session,
    _reconciling,
    _record,
    _run_reusable,
    _session_id,
    _setup,
)
from ._turn_outcomes import (
    acceptance_signals,
    acceptance_unresolved,
    is_terminal,
    observed_phase,
    turn_result,
)
from .types.attempts import (
    AttemptPhase,
    AttemptSetupFailed,
    InitialSessionsFailed,
    InitialSessionsReady,
    InvocationCheckpointRequested,
    InvocationEnded,
    ReleaseDependencyObserved,
)
from .types.common import (
    AttemptId,
    AttemptRef,
    CompletionStatus,
    ContractValidationError,
    InvocationRef,
    ObservationStatus,
    ReleaseDependency,
    RequestId,
    Scope,
    SetupFailureKind,
)
from .types.kernel import AreaChange, DecisionCompleted
from .types.records import (
    ExecuteRegisteredOperation,
    Outbound,
    RequestPhase,
    within,
)
from .types.sessions import (
    CancelTurn,
    CloseSession,
    EnsureSession,
    InputAcceptanceObserved,
    InputReservationReleased,
    InputReservationRequested,
    InspectTurn,
    Invocation,
    InvocationCancellationRequested,
    InvocationChargesAuthorized,
    InvocationCheckpointAvailable,
    RegisteredTurnRequested,
    RunInvocationCheckpointObserved,
    RunInvocationCheckpointRequested,
    RunSessionsDrainRequested,
    SessionAcquisitionGroup,
    SessionDrainRequested,
    SessionObserved,
    SessionPhase,
    SessionsAcquireRequested,
    SessionsState,
    SessionView,
    TurnInputsReserved,
    TurnObserved,
    TurnRequested,
    session_may_wait,
)
from .types.strategy import Accepted, RequestTurn

if TYPE_CHECKING:
    from .types.common import Observation
    from .types.kernel import SessionsContext, Signal
    from .types.records import RequestRecord
    from .types.sessions import SessionsEvent, SessionSpec


def _acquisition_session(
    state: SessionsState,
    context: SessionsContext,
    event: SessionsAcquireRequested,
    spec: SessionSpec,
) -> SessionView:
    session = _session(state, spec.session_id)
    if session is None:
        return SessionView(
            spec=spec,
            scope=event.scope,
            generation=event.scope.generation,
            phase=SessionPhase.ACQUIRING,
        )
    if session.spec != spec or (
        session.scope != event.scope and not _run_reusable(session, context)
    ):
        raise ContractValidationError("specs", "session ownership conflict")
    if session.resource_id is None:
        raise ContractValidationError(
            "required_resource", "reattachment lacks durable correspondence"
        )
    if session.phase not in (
        SessionPhase.IDLE,
        SessionPhase.TERMINAL,
        SessionPhase.CHECKPOINTED,
        SessionPhase.SUSPENDED,
    ):
        raise ContractValidationError(
            "specs", "session is still owned by an active acquisition or invocation"
        )
    current = _current_invocation(state, session)
    if current is not None and current.phase in (
        SessionPhase.ACQUIRING,
        SessionPhase.EXECUTING,
        SessionPhase.UNKNOWN,
    ):
        raise ContractValidationError("specs", "session has an outstanding invocation")
    return session


def _acquire(
    state: SessionsState, context: SessionsContext, event: SessionsAcquireRequested
) -> AreaChange[SessionsState]:
    owner = attempt_for(context, event.scope)
    if (
        owner is None
        or event.attempt != AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation)
        or owner.admission_id != event.admission_id
        or owner.phase != AttemptPhase.ACQUIRING
        or isinstance(current_closure(owner, owner.closure), Proven)
    ):
        return AreaChange(state=state)
    ids = tuple(spec.session_id for spec in event.specs)
    if len(set(ids)) != len(ids):
        raise ContractValidationError("specs", "duplicate initial session identity")
    previous = next(
        (
            row
            for row in state.acquisition_groups
            if row.attempt == event.attempt and row.admission_id == event.admission_id
        ),
        None,
    )
    if previous is not None:
        if (
            previous.scope != event.scope
            or previous.session_ids != ids
            or tuple(
                member.spec if (member := _session(state, identity)) is not None else None
                for identity in ids
            )
            != event.specs
        ):
            raise ContractValidationError("specs", "acquisition episode payload conflict")
        return AreaChange(state=state)
    group = SessionAcquisitionGroup(
        attempt=event.attempt,
        admission_id=event.admission_id,
        scope=event.scope,
        session_ids=ids,
        phase="acquiring" if ids else "ready",
    )
    requests: list[Outbound] = []
    for spec in event.specs:
        session = _acquisition_session(state, context, event, spec)
        session = session.model_copy(update={"phase": SessionPhase.ACQUIRING})
        request = _ensure(session, context).model_copy(
            update={
                "request_id": _session_id(session, f"ensure:{event.admission_id.root}"),
                "admission_id": event.admission_id,
            }
        )
        session = _awaiting(
            session.model_copy(update={"pending_records": (request.request_id,)}), context, request
        )
        state = _replace_session(state, session)
        requests.append(_setup(context, request))
    state = state.model_copy(update={"acquisition_groups": (*state.acquisition_groups, group)})
    signals = (
        ()
        if ids
        else (
            InitialSessionsReady(
                attempt=event.attempt, admission_id=event.admission_id, session_ids=ids
            ),
        )
    )
    return AreaChange(state=state, requests=tuple(requests), signals=signals)


def _valid_observation(
    context: SessionsContext, session: SessionView, observation: Observation
) -> RequestRecord | None:
    record = _record(context, observation.request_id)
    if (
        record is None
        or record.request.scope != session.scope
        or not isinstance(request_matches(record, record.request), Proven)
        or not isinstance(observation_for(record, observation), Proven)
    ):
        return None
    if isinstance(session.scope.owner, AttemptId) and not isinstance(
        current_admission(
            attempt_for(context, session.scope), session.scope, observation.admission_id
        ),
        Proven,
    ):
        return None
    return record


def _group_admitted(context: SessionsContext, group: SessionAcquisitionGroup) -> bool:
    owner = attempt_for(context, group.scope)
    return (
        owner is not None
        and owner.phase == AttemptPhase.ACQUIRING
        and not isinstance(current_closure(owner, owner.closure), Proven)
    )


def _group_observed(
    state: SessionsState, context: SessionsContext, session: SessionView, event: SessionObserved
) -> AreaChange[SessionsState]:
    record = _record(context, event.observation.request_id)
    admission = record.request.admission_id if record is not None else None
    group = next(
        (
            row
            for row in state.acquisition_groups
            if row.admission_id == admission and session.spec.session_id in row.session_ids
        ),
        None,
    )
    if (
        group is None
        or group.phase == "ready"
        or (group.phase == "failed" and _run_reusable(session, context))
    ):
        return AreaChange(state=state)
    observation = event.observation
    if group.phase == "failed":
        if session.resource_id is None or session.phase in (
            SessionPhase.CLOSING,
            SessionPhase.TERMINAL,
        ):
            return AreaChange(state=state)
        if group.failure_request is None:
            raise ContractValidationError("failure_request", "failed group lacks cleanup authority")
        close = _close(session, context, group.failure_request)
        session = _awaiting(
            session.model_copy(
                update={
                    "phase": SessionPhase.CLOSING,
                    "pending_records": (*session.pending_records, close.request_id),
                }
            ),
            context,
            close,
        )
        return AreaChange(
            state=_replace_session(state, session), requests=(_cleanup(context, close),)
        )
    if (
        observation.terminal
        and not observation.accepted
        and observation.status
        in (ObservationStatus.FAILED, ObservationStatus.REJECTED, ObservationStatus.CANCELLED)
    ):
        group = group.model_copy(
            update={"phase": "failed", "failure_request": observation.request_id}
        )
        state = state.model_copy(
            update={
                "acquisition_groups": tuple(
                    group
                    if row.attempt == group.attempt and row.admission_id == group.admission_id
                    else row
                    for row in state.acquisition_groups
                )
            }
        )
        requests: list[Outbound] = []
        for identity in group.session_ids:
            member = _session(state, identity)
            if member is None or member.phase in (SessionPhase.CLOSING, SessionPhase.TERMINAL):
                continue
            if member.resource_id is not None and not _run_reusable(member, context):
                request = _close(member, context, observation.request_id)
                member = _awaiting(
                    member.model_copy(
                        update={
                            "phase": SessionPhase.CLOSING,
                            "pending_records": (*member.pending_records, request.request_id),
                        }
                    ),
                    context,
                    request,
                )
                state = _replace_session(state, member)
                requests.append(_cleanup(context, request))
        return AreaChange(
            state=state,
            requests=tuple(requests),
            signals=(
                InitialSessionsFailed(
                    attempt=group.attempt,
                    admission_id=group.admission_id,
                    session_id=session.spec.session_id,
                    observation=observation,
                    failure=SetupFailureKind.UNKNOWN,
                ),
            ),
        )
    if _group_admitted(context, group) and all(
        (member := _session(state, identity)) is not None
        and member.phase in (SessionPhase.IDLE, SessionPhase.SUSPENDED, SessionPhase.CHECKPOINTED)
        and member.resource_id is not None
        for identity in group.session_ids
    ):
        ready = group.model_copy(update={"phase": "ready"})
        state = state.model_copy(
            update={
                "acquisition_groups": tuple(
                    ready
                    if row.attempt == group.attempt and row.admission_id == group.admission_id
                    else row
                    for row in state.acquisition_groups
                )
            }
        )
        return AreaChange(
            state=state,
            signals=(
                InitialSessionsReady(
                    attempt=group.attempt,
                    admission_id=group.admission_id,
                    session_ids=group.session_ids,
                ),
            ),
        )
    return AreaChange(state=state)


def _never_acquired(context: SessionsContext, session: SessionView) -> bool:
    """Whether the session holds no conversation and none can still appear.

    Every ensure it asked for ended without one (core cancelled it unsent, or it was
    rejected), so closing it has nothing to release and the close is complete.
    """
    return not any(
        row.request_id in session.pending_records and isinstance(row.request, EnsureSession)
        for row in context.records.records
    )


def _close_observed(
    state: SessionsState, context: SessionsContext, session: SessionView, event: SessionObserved
) -> AreaChange[SessionsState]:
    observation = event.observation
    if not (
        isinstance(resolved_observation(observation), Proven)
        and (
            observation.resource_id == session.resource_id
            if session.resource_id is not None
            else _never_acquired(context, session)
        )
    ):
        inspection = _inspect_session(session, context, observation.request_id)
        return AreaChange(
            state=_replace_session(state, _awaiting(session, context, inspection)),
            requests=(_cleanup(context, inspection),),
        )
    session = session.model_copy(
        update={
            "phase": SessionPhase.TERMINAL,
            "pending_records": (),
            "acceptance_sequence": observation.sequence,
        }
    )
    state = _replace_session(state, session)
    owner = attempt_for(context, session.scope)
    signals = (
        ()
        if owner is None
        else (
            ReleaseDependencyObserved(
                attempt=AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
                dependency=ReleaseDependency(kind="session", identity=event.session_id),
                observation=observation,
            ),
        )
    )
    return AreaChange(state=state, signals=signals)


def _abandon_turn_acquisition(
    state: SessionsState, context: SessionsContext, session: SessionView, observation: Observation
) -> AreaChange[SessionsState]:
    if session.invocation is None:
        return AreaChange(state=state)
    ref = InvocationRef(
        session_id=session.spec.session_id,
        invocation_id=session.invocation,
        generation=session.generation,
    )
    invocation = _invocation(state, ref)
    if invocation is None or invocation.phase != SessionPhase.ACQUIRING:
        return AreaChange(state=state)
    owner = attempt_for(context, invocation.scope)
    invocation = invocation.model_copy(
        update={
            "phase": SessionPhase.TERMINAL,
            # A closing attempt counts a turn drained only on an observation of its own
            # dispatch; a cancelled acquisition never had one, so it ends as never started.
            "observation": stalled_acquisition(invocation, context.run.now_at)
            if observation.status == ObservationStatus.CANCELLED
            else observation,
        }
    )
    state = _replace_invocation(state, invocation)
    signals: list[Signal] = [InputReservationReleased(invocation=ref, observation=observation)]
    if owner is not None:
        signals.append(
            AttemptSetupFailed(
                attempt=AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
                observation=observation,
                failure=SetupFailureKind.UNKNOWN,
            )
        )
    record = _record(context, observation.request_id)
    if record is not None and record.request.decision_id is not None:
        status = (
            CompletionStatus.CANCELLED
            if observation.status == ObservationStatus.CANCELLED
            else CompletionStatus.FAILED
        )
        signals.append(DecisionCompleted(decision_id=record.request.decision_id, status=status))
    return AreaChange(
        state=state,
        signals=tuple(signals),
        events=(turn_result(ref, observation),),
    )


def _prepared_registered_completion(
    context: SessionsContext, invocation: Invocation
) -> tuple[Signal, ...]:
    record = next(
        (
            row
            for row in context.records.records
            if isinstance(row.request, ExecuteRegisteredOperation)
            and row.request.operation_id == invocation.registered_operation
            and row.request.scope == invocation.scope
        ),
        None,
    )
    if record is None or not isinstance(record.request, ExecuteRegisteredOperation):
        return ()
    request = _registered_request(context, record.request, invocation.turn)
    receipt = next(
        (row for row in context.run.receipts if row.decision_id == request.decision_id), None
    )
    return (
        ()
        if receipt is None or receipt.completion is not None
        else (
            DecisionCompleted(decision_id=receipt.decision_id, status=CompletionStatus.CANCELLED),
        )
    )


def _prepared_completion(context: SessionsContext, invocation: Invocation) -> tuple[Signal, ...]:
    if invocation.registered_operation is not None:
        return _prepared_registered_completion(context, invocation)
    decision = next(
        (
            receipt.decision
            for receipt in context.run.receipts
            if isinstance(receipt.feedback, Accepted)
            and isinstance(receipt.decision, RequestTurn)
            and receipt.decision.scope == invocation.scope
            and receipt.decision.turn == invocation.turn
            and receipt.completion is None
        ),
        None,
    )
    if decision is None:
        return ()
    return (DecisionCompleted(decision_id=decision.decision_id, status=CompletionStatus.CANCELLED),)


def _after_terminal_ensure(
    grouped: AreaChange[SessionsState],
    context: SessionsContext,
    session: SessionView,
    event: SessionObserved,
    closing_authority: RequestId | None,
) -> AreaChange[SessionsState]:
    """The session ended without a conversation: abandon its turn, and close it if its attempt is."""
    abandoned = _abandon_turn_acquisition(grouped.state, context, session, event.observation)
    if closing_authority is not None and session.resource_id is None:
        # A closing attempt waits on the session's release, which only a close can report.
        return _closing(abandoned, context, session, closing_authority)
    return abandoned


def _after_ensure(
    state: SessionsState, context: SessionsContext, session: SessionView, event: SessionObserved
) -> AreaChange[SessionsState]:
    grouped = _group_observed(state, context, session, event)
    if grouped.signals or grouped.requests:
        return grouped
    owner = attempt_for(context, session.scope)
    closure = current_closure(owner, owner.closure if owner is not None else None)
    if session.phase == SessionPhase.TERMINAL:
        authority = closure.value.authority if isinstance(closure, Proven) else None
        return _after_terminal_ensure(grouped, context, session, event, authority)
    if session.phase == SessionPhase.UNKNOWN:
        return grouped
    if isinstance(closure, Proven):
        invocation = _current_invocation(grouped.state, session)
        if invocation is not None and invocation.phase == SessionPhase.ACQUIRING:
            # Never reached an executor; closure counts a turn drained only on an observation.
            abandoned = invocation.model_copy(
                update={
                    "phase": SessionPhase.TERMINAL,
                    "observation": stalled_acquisition(invocation, context.run.now_at),
                }
            )
            grouped = grouped.model_copy(
                update={"state": _replace_invocation(grouped.state, abandoned)}
            )
        request = _close(session, context, closure.value.authority)
        closing = _awaiting(
            session.model_copy(
                update={
                    "phase": SessionPhase.CLOSING,
                    "pending_records": (*session.pending_records, request.request_id),
                }
            ),
            context,
            request,
        )
        signals = () if invocation is None else _prepared_completion(context, invocation)
        return AreaChange(
            state=_replace_session(grouped.state, closing),
            requests=(_cleanup(context, request),),
            signals=signals,
        )
    if session.invocation is not None:
        return _reservation_after_acquisition(grouped, context, session)
    return grouped


def _closing(
    change: AreaChange[SessionsState],
    context: SessionsContext,
    session: SessionView,
    authority: RequestId,
) -> AreaChange[SessionsState]:
    """*change* plus the close of *session*, which then waits for it."""
    request = _close(session, context, authority)
    closing = _awaiting(
        session.model_copy(
            update={
                "phase": SessionPhase.CLOSING,
                "pending_records": (*session.pending_records, request.request_id),
            }
        ),
        context,
        request,
    )
    return change.model_copy(
        update={
            "state": _replace_session(change.state, closing),
            "requests": (*change.requests, _cleanup(context, request)),
        }
    )


def _acquired_phase(state: SessionsState, session: SessionView) -> SessionPhase:
    current = _current_invocation(state, session)
    return (
        SessionPhase.SUSPENDED
        if current is not None and current.phase == SessionPhase.SUSPENDED
        else SessionPhase.IDLE
    )


def _ensure_observed(
    state: SessionsState,
    context: SessionsContext,
    session: SessionView,
    event: SessionObserved,
    request: EnsureSession,
) -> AreaChange[SessionsState]:
    observation = event.observation
    if session.phase == SessionPhase.TERMINAL:
        return AreaChange(state=state)
    ambiguous = (
        observation.status in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
        or (not observation.accepted and observation.status == ObservationStatus.SUCCEEDED)
        or (
            observation.accepted
            and (
                observation.resource_id is None
                or not observation.terminal
                or observation.status != ObservationStatus.SUCCEEDED
            )
        )
    )
    if ambiguous:
        session = session.model_copy(
            update={"phase": SessionPhase.UNKNOWN, "acceptance_sequence": observation.sequence}
        )
        inspection = _inspect_session(session, context, observation.request_id)
        return AreaChange(
            state=_replace_session(state, _awaiting(session, context, inspection)),
            requests=(_cleanup(context, inspection),),
        )
    if observation.accepted:
        if (
            request.required_resource is not None
            and observation.resource_id != request.required_resource
        ):
            raise ContractValidationError(
                "resource_id", "reattachment substituted a different conversation"
            )
        phase = _acquired_phase(state, session)
    elif observation.terminal and observation.status != ObservationStatus.PENDING:
        phase = (
            SessionPhase.UNKNOWN
            if _run_reusable(session, context) and request.required_resource is not None
            else SessionPhase.TERMINAL
        )
    else:
        return AreaChange(state=state)
    session = session.model_copy(
        update={
            "phase": phase,
            "resource_id": observation.resource_id or session.resource_id,
            "acceptance_sequence": observation.sequence,
            "pending_records": tuple(
                identity
                for identity in session.pending_records
                if phase == SessionPhase.UNKNOWN or identity != observation.request_id
            ),
        }
    )
    change = _after_ensure(_replace_session(state, session), context, session, event)
    if phase == SessionPhase.UNKNOWN:
        inspection = _inspect_session(session, context, observation.request_id)
        unknown = _session(change.state, session.spec.session_id)
        change = change.model_copy(
            update={
                "state": change.state
                if unknown is None or unknown.phase != SessionPhase.UNKNOWN
                else _replace_session(change.state, _awaiting(unknown, context, inspection)),
                "requests": (*change.requests, inspection),
            }
        )
    return change


def _reservation_after_acquisition(
    change: AreaChange[SessionsState], context: SessionsContext, session: SessionView
) -> AreaChange[SessionsState]:
    if session.invocation is None:
        return change
    ref = InvocationRef(
        session_id=session.spec.session_id,
        invocation_id=session.invocation,
        generation=session.generation,
    )
    invocation = _invocation(change.state, ref)
    if (
        invocation is not None
        and invocation.phase == SessionPhase.ACQUIRING
        and session.phase == SessionPhase.IDLE
        and scope_active(context, invocation.scope)
        and _charged(change.state, context, invocation)
    ):
        return change.model_copy(update={"signals": (InputReservationRequested(invocation=ref),)})
    return change


def _session_observed(
    state: SessionsState, context: SessionsContext, event: SessionObserved
) -> AreaChange[SessionsState]:
    session = _session(state, event.session_id)
    if session is None or event.observation.request_id not in session.pending_records:
        return AreaChange(state=state)
    record = _valid_observation(context, session, event.observation)
    if record is None:
        return AreaChange(state=state)
    observation = event.observation
    # Sequences belong to a request, not to the physical conversation globally.
    previous = record.observation
    if previous is not None and previous.sequence > observation.sequence:
        return AreaChange(state=state)
    request = record.request
    if isinstance(request, CloseSession) and request.session_id == event.session_id:
        return _close_observed(state, context, session, event)
    if isinstance(request, EnsureSession) and request.spec == session.spec:
        return _ensure_observed(state, context, session, event, request)
    return AreaChange(state=state)


def _terminal_signals(
    state: SessionsState, context: SessionsContext, invocation: Invocation, event: TurnObserved
) -> tuple[Signal, ...]:
    observation = event.observation
    signals: list[Signal] = []
    owner = attempt_for(context, invocation.scope)
    claim = next((row for row in state.interrupts if row.invocation == event.invocation), None)
    claimed = claim is not None and claim.phase in ("pending", "draining")
    # One producer for every reason a terminal turn leaves work to retain: an interruption
    # claim, a yield, or a conclusive write turn. The claim's authority wins when present.
    authority = (
        claim.authority
        if claim is not None
        else write_turn_authority(event.invocation)
        if retains_write_turn(invocation)
        else yield_authority(event.invocation)
    )
    if owner is not None:
        attempt = AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation)
        signals.append(
            InvocationEnded(attempt=attempt, invocation=event.invocation, observation=observation)
        )
        if event.suspension is not None or claimed or retains_write_turn(invocation):
            signals.append(
                InvocationCheckpointRequested(
                    attempt=attempt,
                    invocation=event.invocation,
                    retention="wip",
                    authority=authority,
                )
            )
    elif event.suspension is not None or claimed:
        signals.append(
            RunInvocationCheckpointRequested(
                invocation=event.invocation,
                scope=invocation.scope,
                retention="wip",
                authority=claim.authority if claim is not None else observation.request_id,
            )
        )
    status = {
        ObservationStatus.SUCCEEDED: CompletionStatus.SUCCEEDED,
        ObservationStatus.CANCELLED: CompletionStatus.CANCELLED,
    }.get(observation.status, CompletionStatus.FAILED)
    record = _record(context, observation.request_id)
    if (
        record is not None
        and record.request.decision_id is not None
        and not (event.suspension is not None and status == CompletionStatus.SUCCEEDED)
    ):
        # Checkpoint failure preserves the yielded decision dependency fence.
        signals.append(DecisionCompleted(decision_id=record.request.decision_id, status=status))
    return tuple(signals)


def _after_run_turn_cleanup(
    state: SessionsState, context: SessionsContext, session: SessionView
) -> AreaChange[SessionsState]:
    proof = committed_stop(context.run)
    if isinstance(proof, Proven) and session.scope == proof.value.scope:
        return _drain_lease(
            state, context, session, RequestId(root=f"stop:{proof.value.decision_id.root}")
        )
    return AreaChange(state=state)


def _after_turn_cleanup(
    state: SessionsState, context: SessionsContext, invocation: Invocation, session: SessionView
) -> AreaChange[SessionsState]:
    owner = attempt_for(context, invocation.scope)
    if owner is None or owner.closure is None:
        return (
            _after_run_turn_cleanup(state, context, session)
            if owner is None
            else AreaChange(state=state)
        )
    if session.phase == SessionPhase.TERMINAL and not _run_reusable(session, context):
        return AreaChange(state=state)
    if _run_reusable(session, context):
        observation = invocation.observation
        if observation is None or not (observation.released and observation.children_complete):
            return AreaChange(
                state=state, requests=(_reconciling(context, inspect_turn(context, invocation)),)
            )
        dependency = ReleaseDependency(kind="request", identity=observation.request_id)
        signals = (
            ()
            if dependency not in owner.release_dependencies
            else (
                ReleaseDependencyObserved(
                    attempt=AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
                    dependency=dependency,
                    observation=observation,
                ),
            )
        )
        return AreaChange(state=state, signals=signals)
    request = _close(session, context, owner.closure.authority)
    if request.request_id in session.pending_records:
        return AreaChange(state=state)
    closing = _awaiting(
        session.model_copy(
            update={
                "phase": SessionPhase.CLOSING,
                "pending_records": (*session.pending_records, request.request_id),
            }
        ),
        context,
        request,
    )
    return AreaChange(
        state=_replace_session(state, closing), requests=(_cleanup(context, request),)
    )


def _late_turn_observed(
    state: SessionsState,
    context: SessionsContext,
    invocation: Invocation,
    event: TurnObserved,
    *,
    inspected: bool = False,
) -> AreaChange[SessionsState]:
    previous = invocation.observation
    observation = event.observation
    if (
        previous is None
        or (not inspected and observation.sequence <= previous.sequence)
        or observation.status != previous.status
        or not observation.terminal
        or observation.request_id != previous.request_id
        or (previous.resource_id is not None and observation.resource_id != previous.resource_id)
    ):
        return AreaChange(state=state)
    if (
        observation.accepted
        and not previous.accepted
        and previous.status
        in (ObservationStatus.REJECTED, ObservationStatus.FAILED, ObservationStatus.CANCELLED)
    ):
        raise ContractValidationError(
            "observation.accepted", "acceptance contradicts positive nonacceptance"
        )
    accepted = previous.accepted or observation.accepted
    # The immutable terminal output stays final. Correlated later facts strengthen
    # lease/delivery proof; absent flags cannot revoke earlier positive evidence.
    known_children = set(previous.children)
    incoming_children = set(observation.children)
    complete = (observation.children_complete and known_children <= incoming_children) or (
        previous.children_complete and incoming_children <= known_children
    )
    proof = observation.model_copy(
        update={
            "accepted": accepted,
            "released": previous.released or observation.released,
            "children_complete": complete,
            "resource_id": observation.resource_id or previous.resource_id,
            "children": tuple(dict.fromkeys((*previous.children, *observation.children))),
        }
    )
    invocation = invocation.model_copy(update={"observation": proof})
    state = _replace_invocation(state, invocation)
    signals: tuple[Signal, ...] = ()
    if observation.accepted and not previous.accepted:
        signals = (InputAcceptanceObserved(invocation=event.invocation, observation=observation),)
    session = _session(state, invocation.invocation.session_id)
    if session is None or session.invocation != invocation.invocation.invocation_id:
        return AreaChange(state=state, signals=signals)
    session = session.model_copy(
        update={
            "accepted": session.accepted or accepted,
            "phase": _physical_turn_phase(
                session,
                context,
                SessionPhase.UNKNOWN if acceptance_unresolved(invocation) else invocation.phase,
            ),
        }
    )
    inspection = inspect_turn(context, invocation) if acceptance_unresolved(invocation) else None
    if inspection is not None and session.phase == SessionPhase.UNKNOWN:
        session = _awaiting(session, context, inspection)
    state = _replace_session(state, session)
    cleanup = _after_turn_cleanup(state, context, invocation, session)
    requests = () if inspection is None else (_reconciling(context, inspection),)
    return cleanup.model_copy(
        update={
            "signals": (*signals, *cleanup.signals),
            "requests": tuple(dict.fromkeys((*requests, *cleanup.requests))),
        }
    )


def _physical_turn_phase(
    session: SessionView, context: SessionsContext, phase: SessionPhase
) -> SessionPhase:
    physical_phase = phase if phase != SessionPhase.TERMINAL else SessionPhase.IDLE
    if session.phase == SessionPhase.TERMINAL or (
        session.phase == SessionPhase.CLOSING
        and any(
            row.request_id in session.pending_records and isinstance(row.request, CloseSession)
            for row in context.records.records
        )
    ):
        physical_phase = session.phase
    return physical_phase


def _correlated_turn_observation(
    context: SessionsContext, invocation: Invocation, event: TurnObserved
) -> tuple[TurnObserved, bool]:
    previous = invocation.observation
    inspection = _record(context, event.observation.request_id)
    inspected = inspection is not None and isinstance(inspection.request, InspectTurn)
    if inspected and previous is not None:
        # Inspection strengthens the original dispatch delivery facts.
        event = event.model_copy(
            update={
                "observation": event.observation.model_copy(
                    update={
                        "request_id": previous.request_id,
                        "sequence": max(previous.sequence, event.observation.sequence),
                    }
                )
            }
        )
    return event, inspected


def _turn_observed(
    state: SessionsState, context: SessionsContext, event: TurnObserved
) -> AreaChange[SessionsState]:
    invocation = _invocation(state, event.invocation)
    if (
        invocation is None
        or not _turn_proof(context, invocation, event.observation)
        or not isinstance(
            fresh_observation(
                () if invocation.observation is None else (invocation.observation,),
                event.observation,
                complete=True,
            ),
            Proven,
        )
    ):
        return AreaChange(state=state)
    previous = invocation.observation
    if _unresolved_after_inspection(context, invocation, event):
        return _release_lost_turn(state, context, event.invocation)
    event, inspected = _correlated_turn_observation(context, invocation, event)
    if is_terminal(invocation):
        return _late_turn_observed(state, context, invocation, event, inspected=inspected)
    if (
        not inspected and previous is not None and event.observation.sequence <= previous.sequence
    ) or invocation.phase == SessionPhase.ACQUIRING:
        return AreaChange(state=state)
    return _apply_turn_observation(state, context, event, invocation)


def _unresolved_after_inspection(
    context: SessionsContext, invocation: Invocation, event: TurnObserved
) -> bool:
    """Whether the turn's inspection completed and acceptance is still unknown."""
    inspection = _record(context, inspect_turn(context, invocation).request_id)
    return (
        invocation.phase == SessionPhase.UNKNOWN
        and event.observation.status == ObservationStatus.UNKNOWN
        and inspection is not None
        and inspection.phase == RequestPhase.COMPLETED
    )


def _release_lost_turn(
    state: SessionsState, context: SessionsContext, ref: InvocationRef
) -> AreaChange[SessionsState]:
    """Cancel a turn whose acceptance the executor's own inspection could not settle.

    The inspection either answered unknown or ended without an answer. The cancellation
    releases the executor's record of the unfinished dispatch, so a successor turn can
    continue the conversation; its result ends this turn as lost.
    """
    return _cancel(
        state,
        context,
        InvocationCancellationRequested(invocation=ref, authority=lost_turn_authority(ref)),
    )


def _apply_turn_observation(
    state: SessionsState,
    context: SessionsContext,
    event: TurnObserved,
    invocation: Invocation,
) -> AreaChange[SessionsState]:
    released_lost = (
        invocation.phase == SessionPhase.UNKNOWN
        and event.observation.status == ObservationStatus.CANCELLED
    )

    observation = event.observation
    if event.suspension is not None:
        event = event.model_copy(update={"suspension": bounded_wait(context.run, event.suspension)})
    if event.suspension is not None and (
        observation.status != ObservationStatus.SUCCEEDED
        or wait_refused_at_turn_end(state, context, invocation, event.suspension)
    ):
        # A wait core cannot honor ends the turn plainly, before anything claims it.
        event = event.model_copy(update={"suspension": None})
    phase = observed_phase(invocation, event)
    invocation = invocation.model_copy(
        update={
            "observation": observation,
            "phase": phase,
            "output_schema": event.output_schema,
            "output_json": event.output_json,
            "pending_suspension": event.suspension,
        }
    )
    state = _replace_invocation(state, invocation)
    session = _session(state, event.invocation.session_id)
    if session is None or session.invocation != event.invocation.invocation_id:
        return AreaChange(state=state)
    session = session.model_copy(
        update={
            "accepted": session.accepted
            or (observation.accepted and observation.status != ObservationStatus.UNKNOWN),
            "acceptance_sequence": observation.sequence,
            "phase": _physical_turn_phase(
                session,
                context,
                SessionPhase.UNKNOWN if acceptance_unresolved(invocation) else phase,
            ),
        }
    )
    signals = acceptance_signals(invocation, event, observation)
    requests: tuple[Outbound, ...] = ()
    inspection: InspectTurn | None = None
    if phase == SessionPhase.UNKNOWN or acceptance_unresolved(invocation):
        inspection = inspect_turn(context, invocation)
        requests = (_reconciling(context, inspection),)
    if session_may_wait(session.phase):
        # A turn still running moves the session only by its own next answer, or by the
        # inspection sent here.
        named: tuple[InspectTurn | RequestId, ...] = (observation.request_id,)
        if inspection is not None:
            named = (*named, inspection)
        session = _awaiting(session, context, *named)
    state = _replace_session(state, session)
    if inspection is not None:
        # The inspection settles the acceptance a yield's checkpoint, or an interruption's,
        # needs before core can request it.
        state = _awaiting_checkpoint(state, context, event.invocation, inspection)
    if not is_terminal(invocation):
        return AreaChange(state=state, signals=signals, requests=requests)
    signals = (*signals, *_terminal_signals(state, context, invocation, event))
    events = (
        turn_result(
            event.invocation,
            observation,
            reply=event,
            lost=released_lost,
            held=released_lost and _turn_holds.held_when_lost(context.records, invocation),
        ),
    )
    cleanup = _after_turn_cleanup(state, context, invocation, session)
    return AreaChange(
        state=cleanup.state,
        signals=(*signals, *cleanup.signals),
        requests=tuple(dict.fromkeys((*requests, *cleanup.requests))),
        events=events,
    )


def _cancel(
    state: SessionsState, context: SessionsContext, event: InvocationCancellationRequested
) -> AreaChange[SessionsState]:
    invocation = _invocation(state, event.invocation)
    if invocation is None or is_terminal(invocation):
        return AreaChange(state=state)
    session = _session(state, event.invocation.session_id)
    if session is None or session.invocation != event.invocation.invocation_id:
        return AreaChange(state=state)
    claim = next(
        (
            row
            for row in state.interrupts
            if row.invocation == event.invocation and row.authority == event.authority
        ),
        None,
    )
    owner = attempt_for(context, invocation.scope)
    closure = current_closure(owner, owner.closure if owner is not None else None)
    cleanup = isinstance(closure, Proven) and closure.value.authority == event.authority
    stop = committed_stop(context.run)
    run_cleanup = (
        isinstance(stop, Proven)
        and stop.value.scope == invocation.scope
        and event.authority == RequestId(root=f"stop:{stop.value.decision_id.root}")
    )
    lost = invocation.phase in (
        SessionPhase.UNKNOWN,
        SessionPhase.EXECUTING,
    ) and event.authority == lost_turn_authority(event.invocation)
    if claim is None and not cleanup and not run_cleanup and not lost:
        raise ContractValidationError(
            "authority", "cancellation requires recorded interruption or cleanup record"
        )
    if lost and invocation.phase == SessionPhase.EXECUTING:
        # The turn's dispatch ended without an answer (its limit passed): it is as lost as
        # one whose acceptance stayed unknown, and its release is told apart by this phase.
        state = _replace_invocation(
            state, invocation.model_copy(update={"phase": SessionPhase.UNKNOWN})
        )
    request = CancelTurn(
        request_id=turn_request_id(event.invocation, f"cancel:{event.authority.root}"),
        scope=invocation.scope,
        admission_id=_episode(context, invocation.scope),
        invocation=event.invocation,
    )
    if request.request_id in session.pending_records:
        return AreaChange(state=state)
    session = _awaiting(
        session.model_copy(
            update={
                "phase": SessionPhase.CLOSING,
                "pending_records": (*session.pending_records, request.request_id),
            }
        ),
        context,
        request,
    )
    return AreaChange(
        state=_replace_session(state, session),
        requests=(within(request, context.run.limits.cancellation_bound),),
    )


def _drain_acquiring(
    state: SessionsState,
    context: SessionsContext,
    session: SessionView,
    invocation: Invocation | None,
) -> AreaChange[SessionsState]:
    ensures = tuple(
        row
        for row in context.records.records
        if row.request_id in session.pending_records and isinstance(row.request, EnsureSession)
    )
    if ensures:
        inspections = tuple(_inspect_session(session, context, row.request_id) for row in ensures)
        return AreaChange(
            state=_replace_session(state, _awaiting(session, context, *inspections)),
            requests=tuple(_cleanup(context, inspection) for inspection in inspections),
        )
    if invocation is None:
        return AreaChange(state=state)
    abandoned = invocation.model_copy(update={"phase": SessionPhase.TERMINAL})
    state = _replace_invocation(state, abandoned)
    if _run_reusable(session, context):
        detached = session.model_copy(
            update={
                "phase": SessionPhase.IDLE,
                "invocation": None,
                "accepted": False,
                "continuation_id": None,
            }
        )
        cleanup = AreaChange(state=_replace_session(state, detached))
    else:
        cleanup = _after_turn_cleanup(state, context, abandoned, session)
    return cleanup.model_copy(
        update={"signals": (*cleanup.signals, *_prepared_completion(context, invocation))}
    )


def _drain_lease(
    state: SessionsState,
    context: SessionsContext,
    session: SessionView,
    authority: RequestId,
) -> AreaChange[SessionsState]:
    if session.resource_id is None:
        ensures = tuple(
            row
            for row in context.records.records
            if row.request_id in session.pending_records and isinstance(row.request, EnsureSession)
        )
        inspections = tuple(_inspect_session(session, context, row.request_id) for row in ensures)
        return AreaChange(
            state=_replace_session(state, _awaiting(session, context, *inspections)),
            requests=tuple(_cleanup(context, inspection) for inspection in inspections),
        )
    request = _close(session, context, authority)
    if request.request_id in session.pending_records:
        return AreaChange(state=state)
    closing = _awaiting(
        session.model_copy(
            update={
                "phase": SessionPhase.CLOSING,
                "pending_records": (*session.pending_records, request.request_id),
            }
        ),
        context,
        request,
    )
    return AreaChange(
        state=_replace_session(state, closing), requests=(_cleanup(context, request),)
    )


def _drain_session(
    state: SessionsState,
    context: SessionsContext,
    session: SessionView,
    event: SessionDrainRequested,
) -> AreaChange[SessionsState]:
    invocation = _current_invocation(state, session)
    scope = Scope(owner=event.attempt.attempt_id, generation=event.attempt.generation)
    if (
        session.scope != scope and (invocation is None or invocation.scope != scope)
    ) or session.phase == SessionPhase.TERMINAL:
        return AreaChange(state=state)
    if invocation is not None and invocation.phase == SessionPhase.ACQUIRING:
        return _drain_acquiring(state, context, session, invocation)
    if (
        invocation is not None
        and invocation.phase == SessionPhase.TERMINAL
        and invocation.observation is None
    ):
        return _drain_lease(state, context, session, event.authority)
    if invocation is not None and not is_terminal(invocation):
        return _cancel(
            state,
            context,
            InvocationCancellationRequested(
                invocation=invocation.invocation, authority=event.authority
            ),
        )
    if _run_reusable(session, context) and invocation is not None:
        return _after_turn_cleanup(state, context, invocation, session)
    return _drain_lease(state, context, session, event.authority)


def _drain(
    state: SessionsState, context: SessionsContext, event: SessionDrainRequested
) -> AreaChange[SessionsState]:
    scope = Scope(owner=event.attempt.attempt_id, generation=event.attempt.generation)
    owner = attempt_for(context, scope)
    closure = current_closure(owner, owner.closure if owner is not None else None)
    if (
        not isinstance(closure, Proven)
        or closure.value.authority != event.authority
        or closure.value.disposition != event.disposition
    ):
        return AreaChange(state=state)
    requests: list[Outbound] = []
    signals: list[Signal] = []
    groups = tuple(
        group.model_copy(update={"phase": "failed", "failure_request": event.authority})
        if group.attempt == event.attempt
        and group.admission_id == closure.value.admission_id
        and group.phase == "acquiring"
        else group
        for group in state.acquisition_groups
    )
    state = state.model_copy(update={"acquisition_groups": groups})
    for session in state.sessions:
        change = _drain_session(state, context, session, event)
        state = change.state
        requests.extend(change.requests)
        signals.extend(change.signals)
    return AreaChange(state=state, requests=tuple(requests), signals=tuple(signals))


def advance(
    state: SessionsState, context: SessionsContext, event: SessionsEvent
) -> AreaChange[SessionsState]:
    """Consume wrapper-routed events without changing input or interruption authority."""
    match event:
        case TurnRequested() | RegisteredTurnRequested():
            change = _admit_turn(state, context, event)
        case SessionsAcquireRequested():
            change = _acquire(state, context, event)
        case InvocationChargesAuthorized():
            change = _charges_authorized(state, context, event)
        case TurnInputsReserved():
            change = _dispatch_reserved(state, context, event)
        case SessionObserved():
            change = _session_observed(state, context, event)
        case TurnObserved():
            change = _turn_observed(state, context, event)
        case InvocationCancellationRequested():
            change = _cancel(state, context, event)
        case InvocationCheckpointAvailable():
            change = _session_checkpoints.advance(state, context, event)
        case SessionDrainRequested():
            change = _drain(state, context, event)
        case _:
            raise ContractValidationError("event.kind", "event is owned by session inputs")
    return change


def _run_drain(
    state: SessionsState, context: SessionsContext, event: RunSessionsDrainRequested
) -> AreaChange[SessionsState]:
    stop = committed_stop(context.run)
    if (
        not isinstance(stop, Proven)
        or stop.value.decision_id != event.authority
        or stop.value.scope != event.scope
    ):
        return AreaChange(state=state)
    requests: list[Outbound] = []
    signals: list[Signal] = []
    for session in state.sessions:
        if session.scope != event.scope or session.phase == SessionPhase.TERMINAL:
            continue
        invocation = _current_invocation(state, session)
        if invocation is not None and invocation.phase == SessionPhase.ACQUIRING:
            change = _drain_acquiring(state, context, session, invocation)
        elif invocation is not None and not is_terminal(invocation):
            change = _cancel(
                state,
                context,
                InvocationCancellationRequested(
                    invocation=invocation.invocation,
                    authority=RequestId(root=f"stop:{event.authority.root}"),
                ),
            )
        else:
            change = _drain_lease(
                state, context, session, RequestId(root=f"stop:{event.authority.root}")
            )
        state = change.state
        requests.extend(change.requests)
        signals.extend(change.signals)
    return AreaChange(state=state, requests=tuple(requests), signals=tuple(signals))


def advance_run_authority(
    state: SessionsState, context: SessionsContext, event: SessionsEvent
) -> AreaChange[SessionsState]:
    """Own run checkpoint and first committed Stop authority without attempt currency."""
    match event:
        case RunInvocationCheckpointRequested() | RunInvocationCheckpointObserved():
            return _session_checkpoints.advance(state, context, event)
        case RunSessionsDrainRequested():
            return _run_drain(state, context, event)
        case _:
            raise ContractValidationError("event.kind", "event is not run authority")
