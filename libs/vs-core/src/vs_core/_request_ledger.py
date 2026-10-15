"""RequestRecord ledger: the core's durable record of every request it has authorized.

A request is issued (registered PREPARED by the kernel), dispatched (committed
before any I/O), observed, and finally terminal. The ledger decides which
observations are fresh, forwards each fresh one to its owning area, and
completes decisions it owns. Observations are never accepted with a
non-increasing sequence: duplicates and older sequences are inert, a different
fact at an accepted sequence is a typed rejection.

Unknown acceptance and retryable failures keep the record open for a later,
higher-sequence observation; only a conclusive terminal observation closes it,
exactly once. Recovery (``_request_recovery``) reads this ledger and never writes
its request fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._deadlines import from_dispatch, started_until
from ._dispatch_episode import episode_ended
from ._outcomes import prove_outcome
from ._proofs import (
    Mismatch,
    Missing,
    ProofField,
    Proven,
    accepted_receipt_for,
    dependency_failed,
    fresh_observation,
    observation_for,
    released_owner,
)
from ._registry import ClosedRecordContradictionError, ContractError
from ._request_forward import declaration, owner_signals, records_own
from ._request_recovery import command_open
from ._session_scope import lost_turn_cancellation
from ._values import digest
from .types.attempts import RevisionOperationRequested
from .types.common import (
    CompletionStatus,
    ContractValidationError,
    EventId,
    ExecuteRegisteredOperation,
    LifecycleClass,
    Observation,
    ObservationStatus,
    OperationNormalizationKind,
    RequestId,
    RevisionAuthority,
    RunStatus,
    SetupFailureKind,
)
from .types.evaluation import RegisteredJobRequested
from .types.kernel import AreaChange, DecisionCompleted
from .types.records import (
    BlockedRecordStalled,
    BlockRequest,
    CancelOwnedResource,
    DispatchAuthorized,
    InspectRequest,
    OperationResult,
    OperationRetireRequested,
    Outbound,
    RecordsState,
    RecoveryPhase,
    ReissueProof,
    RequestObserved,
    RequestPhase,
    RequestPrepared,
    RequestRecord,
    RequestUndispatchable,
    RetryDue,
    TargetObservation,
    request_lifecycle,
    success_without_reply,
    within,
)
from .types.sessions import (
    CancelTurn,
    DispatchTurn,
    InspectTurn,
    RegisteredTurnRequested,
    ResumeSessionTurn,
    TurnObserved,
)
from .types.strategy import Cancel, Withdraw

if TYPE_CHECKING:
    from .types.attempts import AttemptsState
    from .types.common import DecisionId
    from .types.kernel import RecordsContext, RunState, Signal, StrategyEvent
    from .types.records import RecordsEvent, RecordsState, Request
    from .types.sessions import SessionsState

    type Facts = RequestObserved | TargetObservation

_CONCLUSIVE = (
    ObservationStatus.SUCCEEDED,
    ObservationStatus.FAILED,
    ObservationStatus.REJECTED,
    ObservationStatus.CANCELLED,
)
_REPLAYABLE = (LifecycleClass.QUERY, LifecycleClass.IDEMPOTENT_WRITE)
_COMPLETION = {
    ObservationStatus.SUCCEEDED: CompletionStatus.SUCCEEDED,
    ObservationStatus.CANCELLED: CompletionStatus.CANCELLED,
    ObservationStatus.FAILED: CompletionStatus.FAILED,
    ObservationStatus.REJECTED: CompletionStatus.FAILED,
}


def advance(
    state: RecordsState, context: RecordsContext, event: RecordsEvent
) -> AreaChange[RecordsState]:
    """Consume the five ledger events; recovery events belong to Records B."""
    match event:
        case RequestPrepared():
            change = _prepared(state, context, event)
        case DispatchAuthorized():
            change = _dispatched(state, context, event)
        case RequestObserved():
            change = _observed(state, context, event)
        case OperationRetireRequested():
            change = _retire(state, context, event)
        case RetryDue():
            change = _retry(state, context, event)
        case RequestUndispatchable():
            change = _undispatchable(state, context, event)
        case BlockedRecordStalled():
            change = _end_stalled_block(state, context, event)
        case _:
            raise ContractValidationError("event.kind", "event is owned by record recovery")
    return change


def _unique(state: RecordsState, identity: RequestId) -> RequestRecord | None:
    rows = state.find(identity)
    if len(rows) > 1:
        raise ContractError(("request_id", identity.root), "ambiguous canonical request identity")
    return rows[0] if rows else None


def _replace(state: RecordsState, record: RequestRecord) -> RecordsState:
    records = list(state.records)
    for position in state.positions(record.request_id):
        records[position] = record
    return state.model_copy(update={"records": tuple(records)})


def _conclusive(observation: Observation | None) -> bool:
    return observation is not None and observation.terminal and observation.status in _CONCLUSIVE


def _finish(change: AreaChange[RecordsState], *more: AreaChange[RecordsState]) -> AreaChange:
    """Merge sequential changes; signals and requests keep order without duplicates."""
    merged = change
    for item in more:
        merged = AreaChange(
            state=item.state,
            signals=tuple(dict.fromkeys((*merged.signals, *item.signals))),
            requests=tuple(dict.fromkeys((*merged.requests, *item.requests))),
            events=(*merged.events, *item.events),
        )
    return merged


# --- Registration -----------------------------------------------------------------


def _prepared(
    state: RecordsState, context: RecordsContext, event: RequestPrepared
) -> AreaChange[RecordsState]:
    """Route an accepted proposal to the one area that owns its lifecycle."""
    request = event.request
    if request_lifecycle(request) != event.lifecycle:
        raise ContractError(("lifecycle",), "differs from the canonical request class")
    if request.request_id is not None:
        previous = _unique(state, request.request_id)
        if previous is not None:
            if previous.payload_digest != digest(request):
                raise ContractError(("request_id",), "request identity conflict")
            return AreaChange(state=state)
    if not isinstance(request, ExecuteRegisteredOperation):
        return AreaChange(
            state=state,
            requests=(Outbound(request=request, until=from_dispatch(context.run, event.until)),),
        )
    return _route_operation(state, context, event, request)


def _route_operation(
    state: RecordsState,
    context: RecordsContext,
    event: RequestPrepared,
    request: ExecuteRegisteredOperation,
) -> AreaChange[RecordsState]:
    descriptor = declaration(context, request)
    if descriptor.normalization == OperationNormalizationKind.SCOPE_REOPEN:
        raise ContractError(("operation",), "scope reopen enters through its own guarded event")
    if descriptor.revision_authority != RevisionAuthority.NONE:
        owner: Signal = RevisionOperationRequested(
            request=request, authority=descriptor.revision_authority, until=event.until
        )
        return AreaChange(state=state, signals=(owner,))
    if descriptor.lifecycle == LifecycleClass.SESSION_TURN:
        if event.normalized_turn is None:
            raise ContractError(("normalized_turn",), "registered turn requires its normalization")
        turn: Signal = RegisteredTurnRequested(
            request=request, turn=event.normalized_turn, until=event.until
        )
        return AreaChange(state=state, signals=(turn,))
    if descriptor.resource_pool is not None:
        job: Signal = RegisteredJobRequested(
            request=request,
            resource_pool=descriptor.resource_pool,
            expected_measurement=event.normalized_measurement,
        )
        return AreaChange(state=state, signals=(job,))
    return AreaChange(
        state=state,
        requests=(Outbound(request=request, until=from_dispatch(context.run, event.until)),),
    )


def _dispatched(
    state: RecordsState, context: RecordsContext, event: DispatchAuthorized
) -> AreaChange[RecordsState]:
    """Record that I/O may start; the kernel already proved dependencies and recovery.

    A request whose limit counts from its dispatch gets its deadline now, from the run
    clock the event just advanced to the shell's time.
    """
    record = _unique(state, event.request_id)
    if record is None:
        raise ContractError(("request_id",), "dispatch names no canonical request")
    match record.phase:
        case RequestPhase.PREPARED:
            pass
        case RequestPhase.DISPATCHED:
            return AreaChange(state=state)
        case RequestPhase.RECONCILING if record.lifecycle in _REPLAYABLE:
            pass
        case _:
            raise ContractError(("phase",), f"{record.phase.value} request cannot be dispatched")
    updated = record.model_copy(
        update={"phase": RequestPhase.DISPATCHED, "dispatched_in": state.recovery.epoch}
    )
    if record.limit_from_dispatch is not None and record.dispatched_at is None:
        updated = updated.model_copy(
            update={
                "until": started_until(
                    context.run, record.limit_from_dispatch, record.until.watches
                ),
                "dispatched_at": context.run.now_at,
            }
        )
    return AreaChange(state=_replace(state, updated))


# --- Observation ------------------------------------------------------------------


def _reject_proof(proof: Mismatch | Missing, path: str) -> ContractError:
    detail = proof.field.value if isinstance(proof, Mismatch) else proof.reason.value
    return ContractError(("observation", path), f"differs from canonical request: {detail}")


def _observed(
    state: RecordsState, context: RecordsContext, event: RequestObserved
) -> AreaChange[RecordsState]:
    """Commit the root fact, then independent root facts about an inspected target."""
    root = _unique(state, event.observation.request_id)
    if root is None:
        raise ContractError(("observation", "request_id"), "observation names no canonical request")
    change = _apply(state, context, root, event)
    if isinstance(root.request, CancelTurn | DispatchTurn):
        return _finish(change, _end_released_turn_pair(change.state, context, root, event))
    target = event.target
    if target is None or target.target_resource is not None:
        return change
    original = _unique(change.state, target.observation.request_id)
    if original is None:
        raise ContractError(("target", "request_id"), "target names no canonical request")
    if target.reissue is not None and original.phase == RequestPhase.PREPARED:
        # Another inspection already returned it to PREPARED (two epochs inspected it).
        return change
    if target.reissue is not None and _reissuable(original, target.reissue):
        return _finish(change, _reissue(change.state, original, context))
    change = _finish(change, _apply(change.state, context, original, target))
    if isinstance(root.request, InspectTurn) and not success_without_reply(target.observation):
        # An inspection that learned nothing new still ends: Sessions must hear that the
        # inspection of this turn completed, or an unresolved turn would wait for no one.
        # A finished turn is not restated: the reply is one fact that only the inspection's
        # owner event carries, and ending the turn here first would drop it.
        restated = TurnObserved(invocation=root.request.invocation, observation=target.observation)
        return _finish(change, AreaChange(state=change.state, signals=(restated,)))
    return change


def _end_released_turn_pair(
    state: RecordsState, context: RecordsContext, root: RequestRecord, event: RequestObserved
) -> AreaChange[RecordsState]:
    """A release of a turn closes the other half of its dispatch and cancellation pair."""
    if isinstance(root.request, CancelTurn):
        return _end_released_dispatch(state, context, root, event)
    return _end_cancels_of_released_turn(state, context, root, event)


def _end_released_dispatch(
    state: RecordsState, context: RecordsContext, root: RequestRecord, event: RequestObserved
) -> AreaChange[RecordsState]:
    """A cancellation that released a lost turn ends that turn's open dispatch.

    The dispatch never learned its own outcome (the transport died after sending, or its
    limit passed), so the release is the only fact that can close it. The cancellation of a
    lost turn (``lost_turn_authority``) that the executor conclusively refuses (no such turn) closes it
    too: there is no turn left to stop, so the turn is lost as well, as if it was cancelled.
    A cancellation that failed proves nothing about the writer, so it releases nothing: the
    session keeps waiting and the run halts loudly (``WaitKind.SESSION``) rather than free a
    workspace a live writer may still write to. (The workspace fence refuses snapshots of an
    unjudged workspace; it does not stop a running writer.)
    """
    request = root.request
    observation = event.observation
    lost = isinstance(request, CancelTurn) and request.request_id == lost_turn_cancellation(
        request.invocation
    )
    refused = lost and observation.status == ObservationStatus.REJECTED
    if (
        not isinstance(request, CancelTurn)
        or not observation.terminal
        or not (
            observation.status == ObservationStatus.CANCELLED
            or (lost and observation.status == ObservationStatus.SUCCEEDED)
            or refused
        )
    ):
        return AreaChange(state=state)
    rows = tuple(
        row for row in context.sessions.invocations if row.invocation == request.invocation
    )
    dispatch = next(
        (
            record
            for record in state.records
            if isinstance(record.request, DispatchTurn | ResumeSessionTurn)
            and record.phase in (RequestPhase.RECONCILING, RequestPhase.BLOCKED)
            and any(
                record.request.turn == row.turn and record.request.scope == row.scope
                for row in rows
            )
        ),
        None,
    )
    if dispatch is None:
        return AreaChange(state=state)
    answered = dispatch.observation
    ended = observation.model_copy(
        update={
            "request_id": dispatch.request_id,
            "sequence": 0 if answered is None else answered.sequence + 1,
            "status": ObservationStatus.CANCELLED,
            "accepted": False,
            # A refused cancellation says the executor holds no such turn: nothing runs.
            "released": observation.released or refused,
            "children_complete": observation.children_complete or refused,
        }
    )
    return _apply(state, context, dispatch, TargetObservation(observation=ended))


def _end_cancels_of_released_turn(
    state: RecordsState, context: RecordsContext, root: RequestRecord, event: RequestObserved
) -> AreaChange[RecordsState]:
    """A turn that ended released ends the cancellations still waiting on its dispatch.

    A cancellation sent while the turn's dispatch was in flight could not prove the turn
    stopped, so it waits. The dispatch's own release is that proof: left open, the cancellation
    reaches its reconciliation deadline and blocks, and a run that was stopped cannot close.
    """
    observation = event.observation
    request = root.request
    if not isinstance(request, DispatchTurn) or not (
        observation.terminal and observation.released and observation.children_complete
    ):
        return AreaChange(state=state)
    invocations = {
        row.invocation
        for row in context.sessions.invocations
        if row.turn == request.turn and row.scope == request.scope
    }
    waiting = [
        (record, record.observation)
        for record in state.records
        if isinstance(record.request, CancelTurn)
        and record.request.invocation in invocations
        and record.phase
        in (RequestPhase.DISPATCHED, RequestPhase.RECONCILING, RequestPhase.BLOCKED)
        and record.observation is not None
        and not record.observation.terminal
    ]
    change = AreaChange(state=state)
    for cancel, waiting_on in waiting:
        ended = observation.model_copy(
            update={
                "request_id": cancel.request_id,
                "event_id": EventId(root=f"{cancel.request_id.root}:released-with-dispatch"),
                "sequence": waiting_on.sequence + 1,
                "status": ObservationStatus.CANCELLED,
                "accepted": False,
                "resource_id": waiting_on.resource_id,
            }
        )
        change = _finish(
            change, _apply(change.state, context, cancel, TargetObservation(observation=ended))
        )
    return change


def _reissuable(record: RequestRecord, proof: ReissueProof) -> bool:
    """Whether an inspection's proof lets core send this open record out again.

    A session turn is not reissued for a begun record: what its owner does with an
    interrupted turn is turn policy, not recovery, so its inspection is recorded as usual.
    """
    return record.phase in (RequestPhase.DISPATCHED, RequestPhase.RECONCILING) and not (
        proof == ReissueProof.BEGUN_UNSEALED and record.lifecycle == LifecycleClass.SESSION_TURN
    )


def _reissue(
    state: RecordsState, record: RequestRecord, context: RecordsContext
) -> AreaChange[RecordsState]:
    """An open request the executor can account for goes out again, unchanged, from PREPARED.

    Never begun: no effect started, so the request is simply sent again. Begun without a
    result: the effect may have run, so the request is sent again to resume, and its
    executor inspects the external effect before repeating it. Either way the request is
    neither observed nor reported to its owner: the owner never learns it was interrupted,
    and the replay equals the straight run.

    A command (an inspection, cancel or block) is core's own and has no owner's limit: sent
    again, it gets a fresh reconciliation bound from now, or a re-send that waited out its
    first bound would lapse before it is answered.
    """
    until = record.until
    if command_open(record):
        fresh = context.run.now_at + context.run.limits.reconciliation_bound
        until = until.model_copy(update={"at": max(until.at, fresh)})
    reissued = record.model_copy(
        update={
            "phase": RequestPhase.PREPARED,
            "observation": None,
            "sequence": None,
            "retry_at": None,
            "until": until,
        }
    )
    return AreaChange(state=_replace(state, reissued))


def _apply(
    state: RecordsState,
    context: RecordsContext,
    record: RequestRecord,
    facts: Facts,
    *,
    unsent: bool = False,
) -> AreaChange[RecordsState]:
    observation = facts.observation
    proof = observation_for(record, observation)
    if not isinstance(proof, Proven):
        raise _reject_proof(proof, "scope")
    if record.phase == RequestPhase.PREPARED and not unsent:
        raise ContractError(
            ("observation", "request_id"), "observation requires committed dispatch authorization"
        )
    previous = record.observation
    fresh = fresh_observation(() if previous is None else (previous,), observation, complete=True)
    if isinstance(fresh, Mismatch) and fresh.field == ProofField.SEQUENCE:
        if previous is not None and observation.sequence < previous.sequence:
            return AreaChange(state=state)
        raise ContractError(("observation", "sequence"), "conflicts with the accepted observation")
    if not isinstance(fresh, Proven):
        raise _reject_proof(fresh, "sequence")
    if observation == previous:
        return AreaChange(state=state)
    if record.phase == RequestPhase.COMPLETED:
        return _refine(state, record, observation)
    return _advance_phase(state, context, record, facts)


def _refine(
    state: RecordsState, record: RequestRecord, observation: Observation
) -> AreaChange[RecordsState]:
    """A closed record only gains evidence: release facts and children. Its disposition stands.

    A later answer that agrees with the disposition (see `_agrees`) adds what it proves: a
    request core failed when the run closed around it may later be inspected, and the executor's
    "never started" proves it released. A record's release facts never go back. An answer that
    contradicts the disposition is a typed `ClosedRecordContradictionError`.
    """
    previous = record.observation
    if (
        previous is not None
        and observation.status == ObservationStatus.UNKNOWN
        and observation.accepted != previous.accepted
    ):
        # An executor that cannot say reports itself unaccepted, even of a job it once took
        # (it lost its record of a submission that never reached the cluster). Such an answer
        # claims nothing, so it neither contradicts the disposition nor proves a release.
        return AreaChange(state=state)
    if previous is None or not _agrees(previous, observation):
        raise ClosedRecordContradictionError(record.request_id, previous, observation)
    added = tuple(child for child in observation.children if child not in previous.children)
    released = observation.released and not previous.released
    complete = observation.children_complete and not previous.children_complete
    if not (added or released or complete):
        # Owners hold the record's observation as their proof; an answer that proves
        # nothing new leaves it, and them, as they are.
        return AreaChange(state=state)
    proven = previous.model_copy(
        update={
            "event_id": observation.event_id,
            "sequence": observation.sequence,
            "observed_at": observation.observed_at,
            "released": previous.released or released,
            "children": (*previous.children, *added),
            "children_complete": previous.children_complete or complete,
        }
    )
    updated = record.model_copy(update={"observation": proven, "sequence": proven.sequence})
    return AreaChange(state=_replace(state, updated))


def canonical(state: RecordsState, event: RequestObserved) -> RequestObserved:
    """The answer as the ledger recorded it, for the owners that read it after the ledger.

    A closed record keeps its disposition and records only what an answer proves
    (`_refine`), so its recorded observation, not the executor's raw status, is the
    canonical fact at that sequence.
    """
    target = event.target
    if target is None:
        recorded = _recorded(state, event.observation)
        return event if recorded is None else event.model_copy(update={"observation": recorded})
    recorded = None if target.target_resource is not None else _recorded(state, target.observation)
    if recorded is None:
        return event
    return event.model_copy(update={"target": target.model_copy(update={"observation": recorded})})


def _recorded(state: RecordsState, observation: Observation) -> Observation | None:
    """The closed record's observation, when it differs from *observation*.

    The ledger has already taken *observation* in (`_refine`): it either refined the stored
    observation with what *observation* proves, or left it as it was because *observation*
    proved nothing new. Either way the stored observation is the canonical fact, never the
    executor's raw status.
    """
    record = _unique(state, observation.request_id)
    stored = None if record is None else record.observation
    if (
        record is None
        or stored is None
        or record.phase != RequestPhase.COMPLETED
        or stored == observation
    ):
        return None
    return stored


_DID_NOT_SUCCEED = frozenset(
    {ObservationStatus.FAILED, ObservationStatus.REJECTED, ObservationStatus.CANCELLED}
)


def _agrees(closed: Observation, later: Observation) -> bool:
    """Whether a later answer is consistent with the one that closed a record.

    It must claim the same acceptance and resource. An unknown status claims no outcome, so
    it agrees with any: a later host may know less than the one that closed the record. A
    success agrees only with a success; an answer that says the request did not succeed
    (failed, rejected, cancelled) agrees with any other such answer, since each only says
    how it did not succeed. A pending answer says the request is still running, which no
    closed record allows.
    """
    return (
        later.accepted == closed.accepted
        and later.resource_id == closed.resource_id
        and (
            later.status in {ObservationStatus.UNKNOWN, closed.status}
            or {later.status, closed.status} <= _DID_NOT_SUCCEED
        )
    )


def retry_delay(context: RecordsContext, retries: int) -> float:
    """Core seconds to wait before attempt number ``retries + 1``, as the backend declared."""
    return context.run.limits.request_retries.delay(retries)


def _retry_limit(context: RecordsContext, record: RequestRecord) -> int:
    request = record.request
    if isinstance(request, ExecuteRegisteredOperation):
        return request.retry_limit
    return context.run.limits.request_retries.limit


def _next_phase(record: RequestRecord, observation: Observation) -> tuple[RequestPhase, int]:
    """Only a conclusive observation closes; everything else keeps the record open."""
    if _conclusive(observation) and not (
        observation.status == ObservationStatus.SUCCEEDED and not observation.accepted
    ):
        return RequestPhase.COMPLETED, record.retry_count
    if observation.status == ObservationStatus.UNKNOWN or observation.terminal:
        # Unknown, or success whose acceptance is unproven: inspect before any retry.
        return RequestPhase.RECONCILING, record.retry_count
    if observation.retryable:
        return RequestPhase.DISPATCHED, record.retry_count + 1
    return RequestPhase.DISPATCHED, record.retry_count


_EXHAUSTED = "retries-exhausted:"


def is_late(state: RecordsState, event: RequestObserved) -> bool:
    """Whether *event* answers a request core already ended because its retries ran out.

    Core wrote that failure itself and told the owner. Whatever the executor says next is too
    late to change it, and its sequence may equal the one core invented, so it is ignored.
    """
    root = _unique(state, event.observation.request_id)
    return (
        root is not None
        and root.observation is not None
        and root.observation.event_id.root.startswith(_EXHAUSTED)
    )


def _exhausted(context: RecordsContext, record: RequestRecord, facts: Facts) -> Facts:
    """The conclusive failure a request ends with once its retries are used up.

    The executor said, ``retry_limit + 1`` times, that it did not run the request. That is a
    conclusive failure, so the owner of the request hears it as one (for a checkpoint, as a
    declined checkpoint) instead of waiting on a retry that will not come. The sequence moves
    up by one so a redelivery of the retryable answer stays inert.
    """
    observation = facts.observation
    if not observation.retryable or record.retry_count + 1 <= _retry_limit(context, record):
        return facts
    return TargetObservation(
        observation=observation.model_copy(
            update={
                "event_id": EventId(root=f"{_EXHAUSTED}{record.request_id.root}"),
                "sequence": observation.sequence + 1,
                "accepted": False,
                "terminal": True,
                "released": True,
                "children_complete": True,
            }
        ),
        setup_failure=SetupFailureKind.UNKNOWN,
    )


def due_retries(state: RecordsState, now_at: float) -> tuple[RetryDue, ...]:
    """One retry per request whose backoff has passed at core time ``now_at``."""
    return tuple(
        RetryDue(request_id=record.request_id, now_at=now_at)
        for record in state.records
        if (due := record.retry_wait_until) is not None and due <= now_at
    )


def next_retry_at(state: RecordsState) -> float | None:
    """The earliest core time at which a ``ClockAdvanced`` sends a request out again."""
    return min(
        (due for row in state.records if (due := row.retry_wait_until) is not None), default=None
    )


def _retry(
    state: RecordsState, context: RecordsContext, event: RetryDue
) -> AreaChange[RecordsState]:
    """Send a retryably failed request out again, unchanged, once its backoff has passed.

    Recovery owns every open request while it gates the run, so a retry waits for it.
    """
    record = _unique(state, event.request_id)
    due = None if record is None else record.retry_wait_until
    if (
        record is None
        or due is None
        or max(context.run.now_at, event.now_at) < due
        or state.recovery.phase != RecoveryPhase.READY
    ):
        return AreaChange(state=state)
    return _reissue(state, record, context)


@dataclass(frozen=True)
class _EpisodeFacts:
    """The state slices an episode check reads, joined from the context and the records."""

    run: RunState
    attempts: AttemptsState
    sessions: SessionsState
    records: RecordsState


def _undispatchable(
    state: RecordsState, context: RecordsContext, event: RequestUndispatchable
) -> AreaChange[RecordsState]:
    """Close a prepared request that can never be dispatched, as an unsent cancellation.

    Its dependencies failed for good, or the episode that prepared it ended.
    """
    record = _unique(state, event.request_id)
    if record is None or record.phase != RequestPhase.PREPARED:
        return AreaChange(state=state)
    facts = _EpisodeFacts(
        run=context.run, attempts=context.attempts, sessions=context.sessions, records=state
    )
    if not (dependency_failed(record.request, state) or episode_ended(facts, record.request)):
        return AreaChange(state=state)
    return _cancel_prepared(state, context, (record,))


def _end_stalled_block(
    state: RecordsState, context: RecordsContext, event: BlockedRecordStalled
) -> AreaChange[RecordsState]:
    """End the blocked record a closing run waits on, as a conclusive failure.

    The record's bound passed and nothing addresses it, so no executor answer will come. The
    failure keeps what the executor reported (acceptance, resource, children, release), so
    the end never claims a release nobody proved: an owner whose release is unproven keeps
    holding closure until its own cancellation proves it.
    """
    record = _unique(state, event.request_id)
    if (
        record is None
        or record.phase != RequestPhase.BLOCKED
        or context.run.status != RunStatus.CLOSING
    ):
        return AreaChange(state=state)
    previous = record.observation
    request = record.request
    ended = (
        Observation(
            event_id=EventId(root=f"blocked-at-close:{record.request_id.root}"),
            request_id=record.request_id,
            scope=request.scope,
            sequence=0,
            observed_at=context.run.now_at,
            status=ObservationStatus.FAILED,
            terminal=True,
            admission_id=request.admission_id,
        )
        if previous is None
        else previous.model_copy(
            update={
                "event_id": EventId(root=f"blocked-at-close:{record.request_id.root}"),
                "sequence": previous.sequence + 1,
                "observed_at": context.run.now_at,
                "status": ObservationStatus.FAILED,
                "terminal": True,
            }
        )
    ).model_copy(update={"diagnostic": _BLOCKED_AT_CLOSE})
    return _apply(state, context, record, TargetObservation(observation=ended))


_BLOCKED_AT_CLOSE = "the run closed while this request was blocked at its reconciliation bound"


def _record(
    record: RequestRecord, facts: Facts, phase: RequestPhase, retries: int, retry_at: float | None
) -> RequestRecord:
    observation = facts.observation
    update: dict[str, object] = {
        "phase": phase,
        "retry_count": retries,
        "retry_at": retry_at,
        "sequence": observation.sequence,
        "observation": observation,
        "setup_failure": facts.setup_failure,
        "evaluation_result": facts.evaluation_result,
        "suspension": facts.suspension,
    }
    if facts.outcome is not None and facts.operation_schema is not None:
        update.update(
            outcome_schema=facts.operation_schema.outcome_schema,
            outcome_json=facts.outcome_json,
            outcome=facts.outcome,
        )
        if facts.outcome_is_registered:
            # The record carries the proof its decoded copy gets from the registry, so a
            # host that keeps running decides as one that reloaded the record does.
            return prove_outcome(
                record.model_copy(update=update), facts.outcome, facts.operation_schema
            )
    return record.model_copy(update=update)


def _advance_phase(
    state: RecordsState, context: RecordsContext, record: RequestRecord, facts: Facts
) -> AreaChange[RecordsState]:
    facts = _exhausted(context, record, facts)
    phase, retries = _next_phase(record, facts.observation)
    retry_at = (
        context.run.now_at + retry_delay(context, retries)
        if phase == RequestPhase.DISPATCHED and facts.observation.retryable
        else None
    )
    updated = _record(record, facts, phase, retries, retry_at)
    state = _replace(state, updated)
    signals = owner_signals(context, updated, facts)
    events: tuple[StrategyEvent, ...] = ()
    if phase == RequestPhase.COMPLETED:
        state = _block_target(state, updated)
        completion, events = _complete(context, updated, facts)
        change = AreaChange(
            state=state, signals=tuple(dict.fromkeys((*signals, *completion))), events=events
        )
        status = facts.observation.status
        if status != ObservationStatus.SUCCEEDED:
            return _finish(change, _cancel_dependents(change.state, context, updated))
        return change
    return AreaChange(state=state, signals=signals)


def _block_target(state: RecordsState, record: RequestRecord) -> RecordsState:
    """A recorded block command leaves its target fenced awaiting reconciliation."""
    request = record.request
    observation = record.observation
    if (
        not isinstance(request, BlockRequest)
        or observation is None
        or observation.status != ObservationStatus.SUCCEEDED
    ):
        return state
    target = _unique(state, request.target)
    if target is None or target.phase == RequestPhase.COMPLETED:
        return state
    return _replace(state, target.model_copy(update={"phase": RequestPhase.BLOCKED}))


# --- Command completion -----------------------------------------------------------


def _completable(context: RecordsContext, record: RequestRecord) -> bool:
    request = record.request
    if isinstance(request, CancelOwnedResource):
        return True
    return isinstance(request, ExecuteRegisteredOperation) and records_own(
        declaration(context, request)
    )


def _completion_signal(
    context: RecordsContext, decision_id: DecisionId, status: CompletionStatus
) -> tuple[Signal, ...]:
    """One completion per accepted, unfinished decision."""
    proof = accepted_receipt_for(context.run.receipts, decision_id, None)
    if not isinstance(proof, Proven) or proof.value.completion is not None:
        return ()
    return (DecisionCompleted(decision_id=proof.value.decision_id, status=status),)


def _operation_result(
    record: RequestRecord, facts: Facts, context: RecordsContext
) -> tuple[StrategyEvent, ...]:
    request = record.request
    if (
        not isinstance(request, ExecuteRegisteredOperation)
        or not records_own(declaration(context, request))
        or facts.operation_schema is None
        or facts.outcome is None
        or not facts.outcome_is_registered
    ):
        return ()
    result = OperationResult(
        operation_id=request.operation_id,
        observation=facts.observation,
        outcome_schema=facts.operation_schema.outcome_schema,
        operation_schema=facts.operation_schema,
        outcome_json=facts.outcome_json,
        outcome=facts.outcome,
    )
    return (prove_outcome(result, facts.outcome, facts.operation_schema),)


def _complete(
    context: RecordsContext, record: RequestRecord, facts: Facts
) -> tuple[tuple[Signal, ...], tuple[StrategyEvent, ...]]:
    if not _completable(context, record) or record.request.decision_id is None:
        return (), ()
    status = _COMPLETION[facts.observation.status]
    return (
        _completion_signal(context, record.request.decision_id, status),
        _operation_result(record, facts, context),
    )


# --- Dependencies and retirement ----------------------------------------------------


def _unsent_cancellation(context: RecordsContext, record: RequestRecord) -> TargetObservation:
    """Never-dispatched work is provably unaccepted, so core can close it itself."""
    request = record.request
    return TargetObservation(
        observation=Observation(
            event_id=EventId(root=f"cancelled-unsent:{record.request_id.root}"),
            request_id=record.request_id,
            scope=request.scope,
            sequence=0 if record.observation is None else record.observation.sequence + 1,
            observed_at=context.run.now_at,
            status=ObservationStatus.CANCELLED,
            accepted=False,
            terminal=True,
            released=True,
            children_complete=True,
            admission_id=request.admission_id,
        ),
        setup_failure=SetupFailureKind.UNKNOWN,
    )


def _cancel_prepared(
    state: RecordsState, context: RecordsContext, doomed: tuple[RequestRecord, ...]
) -> AreaChange[RecordsState]:
    change = AreaChange(state=state)
    for record in doomed:
        current = _unique(change.state, record.request_id)
        if current is None or current.phase != RequestPhase.PREPARED:
            continue
        facts = _unsent_cancellation(context, current)
        change = _finish(change, _apply(change.state, context, current, facts, unsent=True))
    return change


def _cancel_dependents(
    state: RecordsState, context: RecordsContext, failed: RequestRecord
) -> AreaChange[RecordsState]:
    """Prepared requests that required this request can never be dispatched."""
    doomed = tuple(
        row
        for row in state.records
        if row.phase == RequestPhase.PREPARED and failed.request_id in row.request.depends_on
    )
    return _cancel_prepared(state, context, doomed)


def _retirement_decision(
    context: RecordsContext, event: OperationRetireRequested
) -> DecisionId | None:
    rows = tuple(
        row.decision_id
        for row in context.run.receipts
        if isinstance(row.decision, Withdraw)
        and isinstance(row.decision.disposition, Cancel)
        and row.decision.target == event.operation
        and row.decision.scope == event.scope
        and isinstance(accepted_receipt_for(context.run.receipts, row.decision_id, None), Proven)
    )
    return rows[0] if len(rows) == 1 else None


def _retire(
    state: RecordsState, context: RecordsContext, event: OperationRetireRequested
) -> AreaChange[RecordsState]:
    """Cancel an owned operation: unsent work is closed, live work gets a bounded command."""
    matches = tuple(
        row
        for row in state.records
        if isinstance(row.request, ExecuteRegisteredOperation)
        and row.request.operation_id == event.operation.operation_id
        and row.request.scope == event.scope
    )
    if len(matches) != 1:
        raise ContractError(("operation",), "retirement needs one canonical operation")
    record = matches[0]
    withdraw = _retirement_decision(context, event)
    done = (
        ()
        if withdraw is None
        else _completion_signal(context, withdraw, CompletionStatus.SUCCEEDED)
    )
    if record.phase == RequestPhase.PREPARED:
        change = _cancel_prepared(state, context, (record,))
        return _finish(change, AreaChange(state=change.state, signals=done))
    observation = record.observation
    if record.phase == RequestPhase.COMPLETED and isinstance(
        released_owner(record, (record,)), Proven
    ):
        return AreaChange(state=state, signals=done)
    resource = None if observation is None else observation.resource_id
    request: Request
    if resource is not None:
        request = CancelOwnedResource(
            request_id=RequestId(root=f"retire:{record.request_id.root}"),
            scope=record.request.scope,
            admission_id=record.request.admission_id,
            resource_id=resource,
            target=record.request_id,
        )
        limit = context.run.limits.cancellation_bound
    else:
        request = InspectRequest(
            request_id=RequestId(root=f"retire-inspect:{record.request_id.root}"),
            scope=record.request.scope,
            admission_id=record.request.admission_id,
            target=record.request_id,
        )
        limit = context.run.limits.reconciliation_bound
    return AreaChange(state=state, requests=(within(request, limit),))
