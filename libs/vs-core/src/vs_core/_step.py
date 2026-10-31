"""Single revision authority, deterministic propagation and durable registration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache, partial
from typing import TYPE_CHECKING, Literal, assert_never

from pydantic import TypeAdapter

from . import (
    _run_park,
    _turn_permit,
    attempts,
    evaluation,
    records,
    scheduling,
    sessions,
    settlement,
)
from ._adoption import reopen as reopen_adoption
from ._continuations import (
    SuspensionRefusal,
    exhausted,
    paid_prefix_refusal,
    published_history,
    resume_refusal,
    wait_refusal_in,
)
from ._deadlines import queued_until, resolve_until
from ._dispatch_episode import (
    dispatch_episode_current,
    registered_retirement,
    retirement_dispatch,
    scope_admission,
)
from ._host_epochs import downtime
from ._inspection import validate_inspection_target, validate_registered_owner
from ._ledger_compaction import compact_ledger
from ._operator_actions import cancellations, notice, reopen_retirements
from ._ownership import cleanup_pending
from ._parked_admission import parked_epoch_drops
from ._proofs import (
    Proven,
    accepted_receipt_for,
    committed_stop,
    descriptor_matches,
    observation_for,
    occupied_episode,
    operation_for,
    request_dependency_status,
    requested_stop_mode,
    stop_mode,
)
from ._quota_pause_clock import credit_quota_pause
from ._registry import ContractError
from ._routing import SIGNAL_ORDER, event_area
from ._run_pause import EpochFacts, control_refusal, reopened, stop_standing
from ._session_inputs import check_declared_target, may_have_been_read
from ._session_inputs import mid_turn_offers as _mid_turn_offers
from ._validation import validate_decision
from ._values import digest, share_unchanged
from ._waits import (
    lapsed_check_waits,
    lapsed_job_waits,
    lapsed_session_waits,
    stalled_acquisitions,
    stalled_blocked_records,
    stalled_checkpoint_waits,
    undispatchable_requests,
    unmet_waits,
)
from ._watches import watch_deadlines
from .types.attempts import (
    AttemptAdmitted,
    AttemptRegistered,
    AttemptsEvent,
    RetireRequested,
    ScopeReopenAdmitted,
)
from .types.common import (
    OPERATOR_ACTIONS,
    Area,
    AttemptId,
    AttemptRef,
    ControlId,
    ControlInput,
    DecisionId,
    DependencyStatus,
    InvocationRef,
    KernelNotImplementedError,
    LifecycleClass,
    OperationId,
    OperationNormalizationKind,
    OperationRef,
    PauseOwner,
    RejectionCode,
    RequestId,
    RevisionAuthority,
    RunPark,
    RunStatus,
    Scope,
    SettlementId,
    SignalCycleError,
    StopMode,
    escalate_stop,
)
from .types.evaluation import (
    ContinuationPhase,
    ContinuationReopenRequested,
    EvaluationEvent,
    MeasurementRequested,
)
from .types.kernel import (
    AreaChange,
    AreaContext,
    AttemptsContext,
    ControlChanged,
    CoreEvent,
    CoreState,
    DecisionCompleted,
    DecisionReceipt,
    DecisionSubmitted,
    EpochResumed,
    EvaluationContext,
    ProposalSubmitted,
    RecordsContext,
    RunControlEvent,
    RunEnded,
    SchedulingContext,
    SessionsContext,
    SettlementContext,
    Signal,
    StopLiftRecorded,
    StrategyEvent,
    Transition,
)
from .types.records import (
    Attribution,
    BlockedRecordStalled,
    BlockRequest,
    CancelOwnedResource,
    DispatchAuthorized,
    ExecuteRegisteredOperation,
    InspectRequest,
    OperationResult,
    OperationRetireRequested,
    Outbound,
    RecordsEvent,
    RecoveryPhase,
    RecoveryReady,
    RecoveryStarted,
    Request,
    RequestObserved,
    RequestPhase,
    RequestPrepared,
    RequestRecord,
    RequestUndispatchable,
    Scheduled,
    request_lifecycle,
)
from .types.scheduling import (
    AdmissionControl,
    AdmitAttempt,
    AttemptReopenRequest,
    AttemptRequest,
    AttemptRequested,
    ClockAdvanced,
    CloseAdmission,
    ParallelLimitChanged,
    RegisterAttempt,
    RunDrained,
    SchedulingEvent,
)
from .types.session_inputs import InputDropped, InputDropReason
from .types.sessions import (
    Access,
    AcquisitionStalled,
    CheckpointStalled,
    DispatchTurn,
    InputWithdrawRequested,
    InterruptRequested,
    Invocation,
    ResumeSessionTurn,
    SessionsEvent,
    SessionsState,
    SteerReceived,
    TurnRequested,
    TurnSpec,
)
from .types.settlement import (
    AssessmentSubmitted,
    Settlement,
    SettlementEvent,
    WinnerProposed,
)
from .types.strategy import (
    Accepted,
    Cancel,
    Command,
    Finish,
    Interrupt,
    Measure,
    Operation,
    ProposeWinner,
    Rejected,
    RequestTurn,
    Settle,
    StartAttempt,
    Submission,
    Wait,
    Withdraw,
)
from .types.until import FromDispatch, Inherited, Until, Within

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .attempts import Reducer as AttemptsReducer
    from .evaluation import Reducer as EvaluationReducer
    from .types.evaluation import Continuation
    from .types.evaluation_history import EvaluationHistoryCursor
    from .types.kernel import RunState
    from .types.session_inputs import MidTurnOffer

MAX_SIGNALS = 1024

type Dispatch = Callable[[CoreState, Signal], AreaChange]
type _Pending = tuple[Signal, DecisionId | None, DecisionId | None]


@dataclass(frozen=True)
class CoreReducers:
    """Explicit pure implementations of sibling lifecycle transition interfaces.

    Omitted implementations use the declared production reducers. Session turn
    authority always remains in Sessions A, including shared checkpoint handling.
    Implementations must preserve other owners' fields and reject unsupported
    events; kernel propagation validates their typed outputs as usual.
    """

    attempts: AttemptsReducer | None = None
    evaluation: EvaluationReducer | None = None
    session_inputs: sessions.Reducer | None = None
    ledger: Callable[[CoreState], CoreState] | None = None
    """What leaves the request ledger at the end of a step; ``compact_ledger`` when omitted.

    A test passes one that keeps everything, to compare a run with and without compaction.
    """


_DEFAULT_REDUCERS = CoreReducers()


@cache
def _event_adapter[E](event_type: type[E]) -> TypeAdapter[E]:
    """One validator per area event union: building a TypeAdapter costs about 5 ms."""
    return TypeAdapter(event_type)


def _context[C: AreaContext](state: CoreState, model: type[C]) -> C:
    return model(**{name: getattr(state, name) for name in model.model_fields})


def mid_turn_offers(state: CoreState) -> tuple[MidTurnOffer, ...]:
    """The running turn each pending interrupt-mode input can be offered to now.

    The shell offers each with ``InputOfferedMidTurn`` and asks the provider only once core
    has committed the offer.
    """
    return _mid_turn_offers(state.sessions, _context(state, SessionsContext))


def suspension_refusal(
    state: CoreState, invocation: InvocationRef, wait: Continuation | None = None
) -> SuspensionRefusal | None:
    """Why ``invocation`` cannot take a new suspension now, or None when it can.

    The one gate for a suspension: core applies it when a turn is observed with a wait (so
    a refused wait ends the turn before anything claims it), and a host applies the same
    rule when an agent asks to wait, so the agent gets a typed reason instead of a turn
    that silently ends. With ``wait`` it also checks the continuation's jobs and deadline.
    An invocation core does not hold is not active.
    """
    held = next((row for row in state.sessions.invocations if row.invocation == invocation), None)
    if held is None:
        return SuspensionRefusal.SCOPE_NOT_ACTIVE
    return wait_refusal_in(_context(state, EvaluationContext), state.evaluation, held, wait)


def _dispatch(
    state: CoreState, event: Signal, *, reducers: CoreReducers = _DEFAULT_REDUCERS
) -> AreaChange:
    area = event_area(event)
    match area:
        case Area.SCHEDULING:
            change = scheduling.schedule(
                state.scheduling,
                _context(state, SchedulingContext),
                _event_adapter(SchedulingEvent).validate_python(event),
            )
        case Area.ATTEMPTS:
            reducer = attempts.advance_attempt if reducers.attempts is None else reducers.attempts
            change = reducer(
                state.attempts,
                _context(state, AttemptsContext),
                _event_adapter(AttemptsEvent).validate_python(event),
            )
        case Area.SESSIONS:
            change = sessions.advance_session(
                state.sessions,
                _context(state, SessionsContext),
                _event_adapter(SessionsEvent).validate_python(event),
                input_reducer=reducers.session_inputs,
            )
        case Area.EVALUATION:
            reducer = (
                evaluation.advance_evaluation
                if reducers.evaluation is None
                else reducers.evaluation
            )
            change = reducer(
                state.evaluation,
                _context(state, EvaluationContext),
                _event_adapter(EvaluationEvent).validate_python(event),
            )
        case Area.SETTLEMENT:
            change = settlement.settle(
                state.settlement,
                _context(state, SettlementContext),
                _event_adapter(SettlementEvent).validate_python(event),
            )
        case Area.RECORDS:
            change = records.advance_record(
                state.records,
                _context(state, RecordsContext),
                _event_adapter(RecordsEvent).validate_python(event),
            )
        case _:
            assert_never(area)
    return change


def _same_request(previous: RequestRecord, proposal: Request) -> bool:
    """Whether a proposal names the request already prepared under its identity.

    A request has no deadline, so identity is the whole request: a host that restarts
    and proposes the same poll again names the same request.
    """
    return digest(previous.request) == digest(proposal)


def _started(state: CoreState, decision: DecisionId | None) -> RequestRecord | None:
    """The first request of ``decision`` the shell dispatched, whose limit is running."""
    return next(
        (
            row
            for row in state.records.records
            if row.request.decision_id == decision and row.dispatched_at is not None
        ),
        None,
    )


def _resolve(state: CoreState, outbound: Outbound, cause: DecisionId | None) -> Scheduled:
    """Resolve an outbound request's ``until``, capped at the logical-turn permit."""
    return _turn_permit.cap_scheduled(state, _scheduled(state, outbound, cause))


def _scheduled(state: CoreState, outbound: Outbound, cause: DecisionId | None) -> Scheduled:
    """The absolute until, before the logical-turn permit caps it."""
    match outbound.until:
        case Inherited():
            # A request of its own ends with the decision it names (an agent's or core's own
            # names none, and ends with the step's); any other, with the step that made it.
            own = outbound.request.decision_id
            owner = (own or cause) if outbound.attribution is Attribution.OWN else (cause or own)
            receipt = next((item for item in state.run.receipts if item.decision_id == owner), None)
            if receipt is None or receipt.until is None:
                raise ContractError(("until",), "an inherited until needs its accepted decision")
            until = receipt.until
            started = _started(state, owner)
            if started is not None:
                until = started.until
            elif receipt.limit is not None:
                unstarted = FromDispatch(limit=receipt.limit, watches=receipt.until.watches)
                return Scheduled(
                    request=outbound.request,
                    until=queued_until(state.run, unstarted),
                    limit_from_dispatch=receipt.limit,
                )
        case Within() as within:
            until = resolve_until(state.run, within)
        case Until() as until:
            pass
        case FromDispatch() as unstarted:
            return Scheduled(
                request=outbound.request,
                until=queued_until(state.run, unstarted),
                limit_from_dispatch=unstarted.limit,
            )
        case _ as unreachable:
            assert_never(unreachable)
    return Scheduled(request=outbound.request, until=until)


def register_requests(
    state: CoreState, requests: tuple[Scheduled, ...]
) -> tuple[CoreState, tuple[Request, ...]]:
    """Allocate stable identities and persist every proposed request in the outbox."""
    records = list(state.records.records)
    appended: dict[RequestId, RequestRecord] = {}
    appended_at: dict[RequestId, int] = {}
    allocated: list[Request] = []
    added: dict[DecisionId | None, list[RequestId]] = {}
    for index, scheduled in enumerate(requests):
        proposal = scheduled.request
        request_id = proposal.request_id or RequestId(
            root=f"{state.run.run_id.root}:{state.revision}:{index}:{digest(proposal)[:16]}"
        )
        request = proposal.model_copy(update={"request_id": request_id})
        payload_digest = digest(request)
        previous = state.records.record(request_id) or appended.get(request_id)
        if previous is not None:
            if not _same_request(previous, request):
                raise ContractError(("request_id", request_id.root), "request identity conflict")
            continue
        lifecycle = request_lifecycle(request)
        records.append(
            RequestRecord(
                request_id=request_id,
                request=request,
                payload_digest=payload_digest,
                lifecycle=lifecycle,
                phase=RequestPhase.PREPARED,
                until=scheduled.until,
                limit_from_dispatch=scheduled.limit_from_dispatch,
                addressed_by=(),
            )
        )
        appended[request_id] = records[-1]
        appended_at[request_id] = len(records) - 1
        _address(records, appended, request, state.records.positions, appended_at)
        allocated.append(request)
        added.setdefault(request.decision_id, []).append(request_id)
    if not allocated:
        return state, ()
    # A receipt no new request names is returned as the very object it was: the durable commit
    # remembers per-instance facts about unchanged values, and rebuilding every receipt on every
    # step would turn each into an equal but new object that misses them.
    receipts = tuple(
        _with_requests(receipt, added[receipt.decision_id])
        if receipt.decision_id in added
        else receipt
        for receipt in state.run.receipts
    )
    run = (
        state.run
        if all(new is old for new, old in zip(receipts, state.run.receipts, strict=True))
        else state.run.model_copy(update={"receipts": receipts})
    )
    return state.model_copy(
        update={
            "records": state.records.model_copy(update={"records": tuple(records)}),
            "run": run,
        }
    ), tuple(allocated)


def _address(
    records: list[RequestRecord],
    appended: dict[RequestId, RequestRecord],
    request: Request,
    held: Callable[[RequestId], tuple[int, ...]],
    appended_at: dict[RequestId, int],
) -> None:
    """Name a reconciliation command on the record it addresses, as the command is registered.

    A command about a request core holds no record of has no waiter to name it. *held* gives
    the positions of a request the state already held (``records`` extends that ledger in
    place), and *appended_at* the position of one this step appended, so a lookup reads an
    index instead of scanning the ledger once per registered command.
    """
    if (
        not isinstance(request, InspectRequest | CancelOwnedResource | BlockRequest)
        or request.request_id is None
    ):
        return
    positions = held(request.target)
    index = positions[0] if positions else appended_at.get(request.target)
    if index is None:
        return
    row = records[index]
    if request.request_id not in row.addressed_by:
        records[index] = row.model_copy(
            update={"addressed_by": (*row.addressed_by, request.request_id)}
        )
        if row.request_id in appended:
            appended[row.request_id] = records[index]


def _with_requests(receipt: DecisionReceipt, request_ids: list[RequestId]) -> DecisionReceipt:
    merged = tuple(dict.fromkeys((*receipt.request_ids, *request_ids)))
    if merged == receipt.request_ids:
        return receipt
    return receipt.model_copy(update={"request_ids": merged})


def _validate_reopen_episode(
    state: CoreState, request: AttemptReopenRequest, decision: Operation
) -> None:
    """Reentry uses its normalized target and the scheduler's recorded capacity lease."""
    normalization = decision.normalized_scope_reopen
    if normalization is None or request.attempt != normalization.attempt:
        raise ContractError(
            ("admission", "attempt"), "reopen target differs from canonical normalization"
        )
    occupancy = occupied_episode(state.scheduling.slots, request)
    if not isinstance(occupancy, Proven):
        raise ContractError(("admission", "pools"), "reopen requires its recorded capacity episode")


def _validate_initial_admission(request: AttemptRequest, decision: StartAttempt) -> None:
    """Queued registration and capacity acquisition use the accepted start payload."""
    expected = {
        "attempt_id": decision.attempt_id,
        "item_id": decision.item_id,
        "generation": decision.scope.generation,
        "admission_charge": decision.budget.admission_charge,
    }
    for name, value in expected.items():
        if getattr(request, name) != value:
            raise ContractError(("admission", name), "differs from canonical StartAttempt")


def _admission_signal(
    state: CoreState, signal: RegisterAttempt | AdmitAttempt
) -> tuple[Transition, tuple[Signal, ...]]:
    """Resolve original canonical decisions for initial admission and reentry."""
    origin = accepted_receipt_for(state.run.receipts, signal.request.decision_id, None)
    if not isinstance(origin, Proven):
        raise ContractError(("admission",), "signal requires accepted canonical admission")
    decision = origin.value.decision
    if decision is None or decision.scope != Scope(
        owner=state.run.run_id, generation=state.run.generation
    ):
        raise ContractError(
            ("admission", "scope"), "signal requires current-run canonical admission"
        )
    if isinstance(signal, AdmitAttempt) and isinstance(signal.request, AttemptReopenRequest):
        if not isinstance(decision, Operation) or decision.normalized_scope_reopen is None:
            raise ContractError(("admission",), "reopen has no registered normalized operation")
        original = _operation_prepared(state, decision)
        if (
            not isinstance(original, ContinuationReopenRequested)
            or original.request.request_id != signal.request.request_id
        ):
            raise ContractError(
                ("admission", "request_id"), "reopen differs from canonical operation"
            )
        _validate_reopen_episode(state, signal.request, decision)
        return Transition(state=state), (
            ScopeReopenAdmitted(
                attempt=signal.request.attempt,
                request_id=signal.request.request_id,
                admission_id=signal.request.decision_id,
            ),
        )
    if not isinstance(decision, StartAttempt):
        raise ContractError(("admission",), "signal has no registered StartAttempt")
    if not isinstance(signal.request, AttemptRequest):
        raise ContractError(("admission",), "initial admission requires a start request")
    _validate_initial_admission(signal.request, decision)
    if isinstance(signal, RegisterAttempt):
        return Transition(state=state), (
            AttemptRegistered(
                request=signal.request,
                workspace=decision.workspace,
                budget=decision.budget,
                initial_sessions=decision.initial_sessions,
            ),
        )
    return Transition(state=state), (
        AttemptAdmitted(
            request=signal.request,
            admission_id=signal.request.decision_id,
            workspace=decision.workspace,
            budget=decision.budget,
            initial_sessions=decision.initial_sessions,
        ),
    )


def _activation_signal(
    state: CoreState, signal: RegisterAttempt | AdmitAttempt | RecoveryReady
) -> tuple[Transition, tuple[Signal, ...]]:
    """Admissions resolve canonical decisions; recovery wakes scheduling.

    A recovered run asks again for the measurements it accepted and never answered
    (`evaluation.unanswered_measures`). One that closes under a cancel applies the cancel again first: a state
    saved before a stop escalated (``_escalated``) holds a drain Finish and a cancel
    recorded after it, with nothing cancelled. Applying a cancel twice changes nothing.
    """
    if isinstance(signal, RecoveryReady):
        barrier = state.records.recovery
        if signal.epoch != barrier.epoch:
            return Transition(state=state), ()
        if barrier.phase != RecoveryPhase.READY:
            raise ContractError(
                ("recovery", "phase"), "ready notification requires committed barrier proof"
            )
        wake = ClockAdvanced(now_at=state.run.now_at)
        asked_again = evaluation.unanswered_measures(state.run)
        if _closing_under_cancel(state):
            return Transition(state=state), (
                AdmissionControl(action="cancel"),
                wake,
                *asked_again,
            )
        return Transition(state=state), (wake, *asked_again)
    return _admission_signal(state, signal)


def validate_terminal_inputs(
    before: SessionsState, change: AreaChange[SessionsState], now_at: float
) -> None:
    """Validate the pure input-finalization output before terminal publication.

    Preserve every occurrence, payload, sibling field and existing receipt.
    Each pending occurrence gains one RUN_TERMINAL drop (DELIVERY_UNKNOWN for one a provider
    took mid-turn) at now_at and exactly one matching event. Finalization emits no requests or signals. Raises
    ContractError naming the offending output field without changing state.
    """
    if change.requests or change.signals:
        raise ContractError(("sessions", "inputs"), "terminal finalization cannot emit work")
    for name in type(before).model_fields:
        if name != "inputs" and getattr(before, name) != getattr(change.state, name):
            raise ContractError(("sessions", name), "input finalization changed sibling authority")
    if tuple(record.input for record in before.inputs) != tuple(
        record.input for record in change.state.inputs
    ):
        raise ContractError(("sessions", "inputs"), "finalization must preserve input occurrences")
    for old, new in zip(before.inputs, change.state.inputs, strict=True):
        if new.receipt is None or (old.receipt is not None and new != old):
            raise ContractError(
                ("sessions", "inputs"), "finalization requires immutable terminal receipts"
            )
        if old.receipt is None and (
            not isinstance(new.receipt, InputDropped)
            or new.receipt.reason
            != (
                InputDropReason.DELIVERY_UNKNOWN
                if may_have_been_read(old)
                else InputDropReason.RUN_TERMINAL
            )
            or new.receipt.at != now_at
            or new.receipt.target != old.input.target
            or new.receipt.input_id != old.input.input_id
        ):
            raise ContractError(
                ("sessions", "inputs"), "remaining inputs require exact RUN_TERMINAL disposal"
            )
    _validate_final_input_events(before, change)


def _validate_final_input_events(before: SessionsState, change: AreaChange[SessionsState]) -> None:
    """Every newly persisted terminal input receipt is published exactly once."""
    pending = {record.input.input_id for record in before.inputs if record.receipt is None}
    emitted = set()
    receipts = {record.input.input_id: record.receipt for record in change.state.inputs}
    for event in change.events:
        if not isinstance(event, InputDropped):
            raise ContractError(("sessions", "events"), "finalization emits only input receipts")
        if (
            event.input_id not in pending
            or event.input_id in emitted
            or receipts[event.input_id] != event
        ):
            raise ContractError(
                ("sessions", "events"), "receipt must match one finalized occurrence"
            )
        emitted.add(event.input_id)
    if emitted != pending:
        raise ContractError(
            ("sessions", "events"), "every finalized occurrence requires its receipt"
        )


def _finish_run_inputs(state: CoreState) -> Transition:
    """Inputs finalization is pure and precedes the sole terminal publication."""
    proposal = state.run.result
    if proposal is None:
        raise ContractError(("run", "result"), "input finalization requires a terminal proposal")
    terminal = state.model_copy(
        update={"run": state.run.model_copy(update={"status": RunStatus.TERMINAL})}
    )
    events: tuple[StrategyEvent, ...] = ()
    if any(record.receipt is None for record in state.sessions.inputs):
        change = sessions.finish_run(state.sessions, _context(terminal, SessionsContext))
        validate_terminal_inputs(state.sessions, change, state.run.now_at)
        terminal = terminal.model_copy(update={"sessions": change.state})
        events = change.events
    return Transition(state=terminal, events=(*events, RunEnded(result=proposal)))


def _kernel_signal(
    state: CoreState,
    signal: RegisterAttempt | AdmitAttempt | CloseAdmission | RunDrained | RecoveryReady,
    *,
    publishing: bool,
) -> tuple[Transition, tuple[Signal, ...]]:
    """Apply one kernel signal; ``publishing`` is whether this step has requests to register.

    A run is drained only once nothing is left to clean up, and a request this step publishes
    is cleanup the state does not hold yet: the run ends on a later tick, after they are done.
    """
    if isinstance(signal, RegisterAttempt | AdmitAttempt | RecoveryReady):
        return _activation_signal(state, signal)
    if isinstance(signal, CloseAdmission):
        return Transition(state=state), (AdmissionControl(action="drain"),)
    if state.run.result is None:
        raise ContractError(("run", "result"), "drain has no registered stop proposal")
    if publishing or cleanup_pending(state):
        return Transition(state=state), ()
    if state.run.status == RunStatus.TERMINAL:
        return Transition(state=state), ()
    return _finish_run_inputs(state), ()


class _LeafRejectionError(Exception):
    """Unaccepted area changes must roll back through the submission boundary."""

    def __init__(self, feedback: Rejected) -> None:
        self.feedback = feedback
        super().__init__(feedback.detail)


def _validate_area_outputs(
    state: CoreState, area: Area, change: AreaChange, cause: DecisionId | None
) -> None:
    for event in change.events:
        if isinstance(event, Accepted | RunEnded):
            raise ContractError(
                ("feedback",), "only the kernel issues acceptance and run termination"
            )
        if isinstance(event, Rejected) and event.decision_id == cause:
            raise _LeafRejectionError(event)
        if isinstance(event, OperationResult) and not event.outcome_is_registered:
            raise ContractError(("outcome",), "registered callback outcome proof required")
    if state.run.status == RunStatus.TERMINAL and change.requests:
        raise ContractError(("requests",), "terminal run cannot emit new requests")
    for outbound in change.requests:
        request = outbound.request
        if (
            isinstance(request, ExecuteRegisteredOperation)
            and operation_owner(state, request) != area
        ):
            raise ContractError(
                ("operation", "owner"), "registered execution bypassed owning lifecycle"
            )


def _signal_admission(
    state: CoreState, signal: Signal, inherited: DecisionId | None = None
) -> DecisionId | None:
    """Keep an observation's original episode through successor signal propagation."""
    observation = getattr(signal, "observation", None)
    identity = getattr(observation, "request_id", None)
    record = state.records.record(identity)
    if record is not None:
        return record.request.admission_id
    explicit = getattr(signal, "admission_id", None) or getattr(observation, "admission_id", None)
    scope = getattr(signal, "scope", None) or getattr(
        getattr(signal, "request", None), "scope", None
    )
    return explicit or inherited or scope_admission(state, scope)


def _stall_entries(
    state: CoreState, in_flight: Sequence[Request], seen: set[tuple[Area, str]]
) -> list[_Pending]:
    """One pending signal per wait that nothing can end, once per step.

    The waits are a checkpoint wait without a producer (``CheckpointStalled``), a turn acquiring
    with nothing to carry it (``AcquisitionStalled``) and a prepared
    request that can never be dispatched (``RequestUndispatchable``), and a blocked record a closing
    run waits on that no live request addresses (``BlockedRecordStalled``), and a job whose
    recorded wait names a request that ended without moving it (``JobWaitLapsed``), and the same
    for a session (``SessionWaitLapsed``) or a recovery check (``RecoveryCheckWaitLapsed``). A fired watch
    (``WhileOpen``, ``WhileAlive``) is also raised here, as the deadline of the waits that declared it. A signal already raised
    in this step is not raised again: if its reducer could not end the wait (an ambiguous
    record it refuses to act on), the wait stays visible to the orphan check instead of the
    step looping on it.
    """
    if state.run.status == RunStatus.TERMINAL:
        return []
    signals = (
        *(
            CheckpointStalled(invocation=invocation)
            for invocation in stalled_checkpoint_waits(state.sessions, state.records, in_flight)
        ),
        *(
            AcquisitionStalled(invocation=invocation)
            for invocation in stalled_acquisitions(state.sessions, state.records, in_flight)
        ),
        *(
            RequestUndispatchable(request_id=request_id)
            for request_id in undispatchable_requests(state)
        ),
        *(
            BlockedRecordStalled(request_id=request_id)
            for request_id in stalled_blocked_records(state)
        ),
        *lapsed_job_waits(state),
        *lapsed_session_waits(state),
        *lapsed_check_waits(state),
        *watch_deadlines(state),
    )
    return [
        (signal, None, _signal_admission(state, signal))
        for signal in signals
        if (event_area(signal), digest(signal)) not in seen
    ]


def _follow_ups(
    signal: Signal, signals: tuple[Signal, ...], seen: set[tuple[Area, str]]
) -> tuple[Signal, ...]:
    """What a kernel signal sends on: for an opened recovery, without a clock tick already taken.

    Opening recovery wakes the clock, but a tick that opened it (an inspection that lapsed
    unanswered ended the last wait) already ran in this step, and a tick repeats no signal of
    its own. The loop ticks again after any commit.
    """
    if not isinstance(signal, RecoveryReady):
        return signals
    return tuple(
        child
        for child in signals
        if not (isinstance(child, ClockAdvanced) and (event_area(child), digest(child)) in seen)
    )


def propagate(
    state: CoreState,
    initial: tuple[Signal, ...],
    dispatch: Dispatch = _dispatch,
    initial_cause: DecisionId | None = None,
) -> Transition:
    """Apply typed signals to quiescence in fixed area order, rejecting cycles."""
    pending = [(signal, initial_cause, _signal_admission(state, signal)) for signal in initial]
    seen: set[tuple[Area, str]] = set()
    requests: list[Scheduled] = []
    events: list[StrategyEvent] = []
    # At quiescence a suspension nothing can publish is ended, so the wait its turn opened
    # always has a resolver, whichever way the checkpoint went.
    while pending or (pending := _stall_entries(state, [item.request for item in requests], seen)):
        # The run drains last: only once every other signal has had its say can it know whether
        # the step leaves anything to clean up.
        pending.sort(
            key=lambda entry: (
                isinstance(entry[0], RunDrained),
                SIGNAL_ORDER.index(event_area(entry[0])),
            )
        )
        signal, cause, admission_id = pending.pop(0)
        admission_id = _signal_admission(state, signal, admission_id)
        observation = getattr(signal, "observation", None)
        if observation is not None and state.records.positions(observation.request_id):
            cause = _event_cause(state, signal)
        if isinstance(signal, DecisionCompleted):
            completed, notifications = _complete_decision(state, signal)
            state = completed.state
            events.extend(completed.events)
            pending.extend(
                (child, None, _signal_admission(state, child)) for child in notifications
            )
            continue
        if isinstance(signal, RegisterAttempt | AdmitAttempt):
            cause = signal.request.decision_id
            admission_id = signal.request.decision_id
            origin = accepted_receipt_for(state.run.receipts, cause, None)
            if not isinstance(origin, Proven) or origin.value.decision is None:
                raise ContractError(("admission",), "signal requires accepted canonical admission")
        key = (event_area(signal), digest(signal))
        if key in seen:
            raise SignalCycleError(signal.kind)
        seen.add(key)
        if len(seen) > MAX_SIGNALS:
            raise SignalCycleError("propagation-bound")
        if isinstance(
            signal, RegisterAttempt | AdmitAttempt | CloseAdmission | RunDrained | RecoveryReady
        ):
            result, signals = _kernel_signal(state, signal, publishing=bool(requests))
            state = result.state
            follow_ups = _follow_ups(signal, signals, seen)
            pending.extend((child, cause, admission_id) for child in follow_ups)
            events.extend(result.events)
            continue
        change = dispatch(state, signal)
        area = event_area(signal)
        expected = type(getattr(state, area.value))
        if type(change.state) is not expected:
            raise ContractError((area.value, "state"), "reducer returned another area state")
        state = state.model_copy(update={area.value: change.state})
        pending.extend((child, cause, admission_id) for child in change.signals)
        _validate_area_outputs(state, area, change, cause)
        requests.extend(
            _resolve(state, _attributed(outbound, cause, admission_id), cause)
            for outbound in change.requests
        )
        events.extend(change.events)
    return _end_fired_watches(_registered_transition(state, requests, events), dispatch)


def _attributed(
    outbound: Outbound, cause: DecisionId | None, admission_id: DecisionId | None
) -> Outbound:
    """``outbound`` with the decision and admission it belongs to (see ``Attribution``)."""
    if outbound.attribution is Attribution.OWN:
        return outbound
    request = outbound.request
    return outbound.model_copy(
        update={
            "request": request.model_copy(
                update={
                    "decision_id": request.decision_id or cause,
                    "admission_id": request.admission_id or admission_id,
                }
            )
        }
    )


def _end_fired_watches(transition: Transition, dispatch: Dispatch) -> Transition:
    """Also end the waits whose watch had already fired for work requested in this step.

    Such work only now has a record to end.
    """
    fired = watch_deadlines(transition.state)
    if not fired:
        return transition
    follow = propagate(transition.state, fired, dispatch)
    return Transition(
        state=follow.state,
        requests=(*transition.requests, *follow.requests),
        events=(*transition.events, *follow.events),
    )


def _registered_transition(
    state: CoreState, requests: list[Scheduled], events: list[StrategyEvent]
) -> Transition:
    """Register effects atomically and place terminal publication last."""
    if state.run.status == RunStatus.TERMINAL and requests:
        raise ContractError(("requests",), "terminal transition cannot publish new requests")
    state, allocated = register_requests(state, tuple(requests))
    ended = tuple(event for event in events if isinstance(event, RunEnded))
    ordinary_events = tuple(event for event in events if not isinstance(event, RunEnded))
    return Transition(state=state, requests=allocated, events=(*ordinary_events, *ended))


def _complete_decision(
    state: CoreState, event: DecisionCompleted
) -> tuple[Transition, tuple[Signal, ...]]:
    proof = accepted_receipt_for(state.run.receipts, event.decision_id, None)
    if not isinstance(proof, Proven):
        raise ContractError(("decision_id",), "completion requires accepted decision identity")
    receipt = proof.value
    if receipt.completion is not None:
        if receipt.completion != event.status:
            raise ContractError(("completion",), "decision completion conflict")
        return Transition(state=state), ()
    receipts = tuple(
        item.model_copy(update={"completion": event.status})
        if item.decision_id == event.decision_id
        else item
        for item in state.run.receipts
    )
    updated = state.model_copy(update={"run": state.run.model_copy(update={"receipts": receipts})})
    return Transition(state=updated), ()


def _event_cause(state: CoreState, event: Signal) -> DecisionId | None:
    observation = getattr(event, "observation", None)
    if observation is not None:
        record = _unique_record(state, observation.request_id)
        if record is not None:
            if not isinstance(observation_for(record, observation), Proven):
                raise ContractError(
                    ("observation",), "differs from canonical request scope or episode"
                )
            return record.request.decision_id
    return None


def _reject(
    decision: Command, code: RejectionCode, path: tuple[str | int, ...], detail: str
) -> Rejected:
    return Rejected(decision_id=decision.decision_id, code=code, path=path, detail=detail)


def _retirement_admission(state: CoreState, target: AttemptRef) -> DecisionId:
    """Queued retirement uses its accepted registration episode, never a fabricated one."""
    owner = next(
        (
            item
            for item in state.attempts.attempts
            if item.attempt_id == target.attempt_id and item.generation == target.generation
        ),
        None,
    )
    if owner is None:
        raise ContractError(("target",), "retirement requires the exact owned attempt generation")
    queued = next(
        (
            request
            for request in state.scheduling.queue
            if (isinstance(request, AttemptReopenRequest) and request.attempt == target)
            or (
                isinstance(request, AttemptRequest)
                and request.attempt_id == target.attempt_id
                and request.generation == target.generation
            )
        ),
        None,
    )
    if queued is not None:
        return queued.decision_id
    if owner.admission_id is not None:
        return owner.admission_id
    registrations = tuple(
        receipt
        for receipt in state.run.receipts
        if isinstance(receipt.decision, StartAttempt)
        and receipt.decision.scope.owner == state.run.run_id
        and receipt.decision.attempt_id == target.attempt_id
        and receipt.decision.scope.generation == target.generation
        and isinstance(accepted_receipt_for(state.run.receipts, receipt.decision_id, None), Proven)
    )
    if len(registrations) != 1:
        raise ContractError(
            ("target", "admission_id"),
            "queued attempt has no unique canonical accepted registration",
        )
    return registrations[0].decision_id


def _withdraw_signal(state: CoreState, decision: Withdraw) -> tuple[Signal, ...]:
    target = decision.target
    if isinstance(decision.disposition, Interrupt) and isinstance(target, InvocationRef):
        return (
            InterruptRequested(
                invocation=target,
                refund=decision.disposition.refund,
                authority=RequestId(root=f"withdraw:{decision.decision_id.root}"),
            ),
        )
    if isinstance(decision.disposition, Cancel) and isinstance(target, OperationRef):
        return (OperationRetireRequested(operation=target, scope=decision.scope),)
    if not isinstance(target, AttemptRef):
        raise ContractError(("target",), "retirement requires attempt target")
    if isinstance(decision.disposition, Settle):
        proposal = decision.disposition
        value = Settlement(
            settlement_id=SettlementId(root=f"settlement:{decision.decision_id.root}"),
            attempt=target,
            candidate=proposal.candidate,
            assessments=proposal.assessments,
            eligible=proposal.eligible,
            retention=proposal.retention,
            outcome=proposal.outcome,
        )
        return (AssessmentSubmitted(settlement=value),)
    if isinstance(decision.disposition, Interrupt):
        raise ContractError(("target",), "interrupt requires invocation target")
    admission_id = _retirement_admission(state, target)
    return (
        RetireRequested(
            attempt=target,
            disposition=decision.disposition.kind,
            authority=RequestId(root=f"withdraw:{decision.decision_id.root}"),
            admission_id=admission_id,
            requested_at=state.run.now_at,
        ),
    )


def _accepted_until(state: CoreState, decision: Command) -> Until:
    receipt = next(
        (item for item in state.run.receipts if item.decision_id == decision.decision_id), None
    )
    if receipt is None or receipt.until is None:
        raise ContractError(("until",), "an accepted wait stores its until before work starts")
    return receipt.until


def _decision_signal(state: CoreState, decision: Command) -> tuple[Signal, ...]:
    match decision:
        case StartAttempt():
            request = AttemptRequest(
                decision_id=decision.decision_id,
                attempt_id=decision.attempt_id,
                item_id=decision.item_id,
                generation=decision.scope.generation,
                admission_charge=decision.budget.admission_charge,
            )
            signals = (AttemptRequested(request=request),)
        case RequestTurn():
            signals = (
                TurnRequested(
                    scope=decision.scope,
                    turn=decision.turn,
                    until=_accepted_until(state, decision),
                ),
            )
        case Withdraw():
            signals = _withdraw_signal(state, decision)
        case Measure():
            signals = (
                MeasurementRequested(
                    decision_id=decision.decision_id,
                    scope=decision.scope,
                    plan=decision.plan,
                    compared=decision.compared,
                ),
            )
        case ProposeWinner():
            signals = (WinnerProposed(selection=decision.selection),)
        case Finish():
            # The search's Finish carries the mode it read from the first stop; a cancel the
            # run recorded since escalates it, so the closing run cancels from its start.
            mode = escalate_stop(requested_stop_mode(state.run), StopMode(decision.mode))
            signals = (AdmissionControl(action=mode.value),)
        case Operation():
            signals = (_operation_prepared(state, decision),)
        case _:
            assert_never(decision)

    return signals


def _operation_prepared(
    state: CoreState, decision: Operation
) -> RequestPrepared | ContinuationReopenRequested:
    wire = decision.registered_wire
    if wire is None:
        raise ContractError(("operation",), "missing registered ingress proof")
    request = ExecuteRegisteredOperation(
        scope=decision.scope,
        operation_id=OperationId(root=f"operation:{decision.decision_id.root}"),
        operation=wire,
        retry_limit=state.run.limits.request_retries.limit,
    )
    if decision.normalized_scope_reopen is not None:
        request = request.model_copy(
            update={"request_id": RequestId(root=f"operation:{decision.decision_id.root}")}
        )
        return ContinuationReopenRequested(
            request=request, normalization=decision.normalized_scope_reopen
        )
    return RequestPrepared(
        request=request,
        until=_accepted_until(state, decision),
        lifecycle=decision.request.lifecycle,
        normalized_turn=decision.normalized_turn,
        normalized_measurement=decision.normalized_measurement,
    )


def operation_owner(state: CoreState, request: ExecuteRegisteredOperation) -> Area:
    """One declared authority governs each registered execution request."""
    declarations = tuple(
        item for item in state.registry if item.kind == request.operation.schema_ref.kind
    )
    if len(declarations) != 1:
        raise ContractError(("operation",), "unregistered or ambiguous dispatch")
    declaration = descriptor_matches(
        state.registry,
        state.run.capabilities,
        request.operation,
        request.operation.schema_ref.lifecycle,
        declarations[0].normalization,
    )
    if not isinstance(declaration, Proven):
        raise ContractError(("operation",), "dispatch requires an exact offered declaration")
    descriptor = declaration.value
    if (
        descriptor.revision_authority != RevisionAuthority.NONE
        or descriptor.normalization == OperationNormalizationKind.SCOPE_REOPEN
    ):
        return Area.ATTEMPTS
    owners = {
        LifecycleClass.QUERY: Area.RECORDS,
        LifecycleClass.IDEMPOTENT_WRITE: Area.RECORDS,
        LifecycleClass.OWNED_JOB: Area.EVALUATION,
        LifecycleClass.SESSION_TURN: Area.SESSIONS,
    }
    return owners[descriptor.lifecycle]


def _decision_replay(state: CoreState, submission: Submission) -> Transition | None:
    decision = _command(submission)
    previous = next(
        (receipt for receipt in state.run.receipts if receipt.decision_id == decision.decision_id),
        None,
    )
    if previous is None:
        return None
    if previous.payload_digest == digest(decision):
        return Transition(state=state)
    return Transition(
        state=state,
        events=(
            _reject(
                decision,
                RejectionCode.IDENTITY_CONFLICT,
                ("decision_id",),
                "decision payload changed",
            ),
        ),
    )


def _command(submission: Submission) -> Command:
    return submission.request if isinstance(submission, Wait) else submission


def _submitted(
    state: CoreState, event: DecisionSubmitted, dispatch: Dispatch, *, check_revision: bool = True
) -> Transition:
    submission = event.decision
    decision = event.command
    payload_digest = digest(decision)
    replay = _decision_replay(state, submission)
    if replay is not None:
        return replay
    rejection = validate_decision(state, event, check_revision=check_revision)
    feedback = rejection or Accepted(decision_id=decision.decision_id)
    until = (
        resolve_until(state.run, submission.until)
        if rejection is None and isinstance(submission, Wait)
        else None
    )
    bound = _run_park.drain_bound(state, decision)
    if until is not None and bound is not None:
        until = until.model_copy(update={"at": min(until.at, bound)})
    receipt = DecisionReceipt(
        decision_id=decision.decision_id,
        decision=None if rejection is not None else decision,
        until=until,
        limit=None if until is None else max(until.at - state.run.now_at, 0.0),
        payload_digest=payload_digest,
        feedback=feedback,
    )
    updated = state.model_copy(
        update={"run": state.run.model_copy(update={"receipts": (*state.run.receipts, receipt)})}
    )
    if rejection is not None:
        return Transition(state=updated, events=(rejection,))
    if bound is not None:
        updated = updated.model_copy(
            update={"run": updated.run.model_copy(update={"status": RunStatus.DRAINING})}
        )
    if isinstance(decision, Finish):
        updated = updated.model_copy(
            update={
                "run": updated.run.model_copy(
                    update={"result": decision.result, "status": RunStatus.CLOSING}
                )
            }
        )
    try:
        result = propagate(
            updated, _decision_signal(updated, decision), dispatch, decision.decision_id
        )
    except _LeafRejectionError as error:
        rejected_receipt = receipt.model_copy(update={"feedback": error.feedback, "decision": None})
        run = state.run.model_copy(update={"receipts": (*state.run.receipts, rejected_receipt)})
        return Transition(state=state.model_copy(update={"run": run}), events=(error.feedback,))
    except KernelNotImplementedError as error:
        rejection = _reject(decision, error.code, (error.area.value,), str(error))
        receipt = receipt.model_copy(update={"feedback": rejection, "decision": None})
        run = state.run.model_copy(update={"receipts": (*state.run.receipts, receipt)})
        return Transition(state=state.model_copy(update={"run": run}), events=(rejection,))
    request_ids = tuple(
        request.request_id for request in result.requests if request.request_id is not None
    )
    feedback = Accepted(
        decision_id=decision.decision_id,
        allocated_ids=tuple(identity.root for identity in request_ids),
        request_ids=request_ids,
    )
    receipt = next(
        item for item in result.state.run.receipts if item.decision_id == decision.decision_id
    ).model_copy(update={"feedback": feedback, "request_ids": request_ids})
    run = result.state.run.model_copy(
        update={"receipts": (*result.state.run.receipts[:-1], receipt)}
    )
    return result.model_copy(
        update={
            "state": result.state.model_copy(update={"run": run}),
            "events": (feedback, *result.events),
        }
    )


def _stale_proposal(state: CoreState, event: ProposalSubmitted) -> Transition:
    events: list[StrategyEvent] = []
    for submission in event.decisions:
        decision = _command(submission)
        replay = _decision_replay(state, submission)
        if replay is not None:
            events.extend(replay.events)
            continue
        rejection = _reject(
            decision, RejectionCode.STALE_VIEW, ("expected_revision",), "view revision changed"
        )
        receipt = DecisionReceipt(
            decision_id=decision.decision_id,
            payload_digest=digest(decision),
            feedback=rejection,
        )
        state = state.model_copy(
            update={
                "run": state.run.model_copy(update={"receipts": (*state.run.receipts, receipt)})
            }
        )
        events.append(rejection)
    return Transition(state=state, events=tuple(events))


def _proposal(state: CoreState, event: ProposalSubmitted, dispatch: Dispatch) -> Transition:
    if event.expected_revision != state.revision:
        return _stale_proposal(state, event)
    requests: list[Request] = []
    events: list[StrategyEvent] = []
    for decision in event.decisions:
        result = _submitted(
            state, DecisionSubmitted(decision=decision, expected_revision=state.revision), dispatch
        )
        state = result.state
        requests.extend(result.requests)
        events.extend(result.events)
    return Transition(state=state, requests=tuple(requests), events=tuple(events))


def _stop_control(state: CoreState, event: RunControlEvent, dispatch: Dispatch) -> Transition:
    """Use the ordinary accepted Finish receipt as run-drain authority."""
    if event.result is None:
        return _soft_stop(state, event)
    decision = Finish(
        decision_id=DecisionId(root=f"control:{event.control.control_id.root}"),
        scope=Scope(owner=state.run.run_id, generation=state.run.generation),
        mode=_finish_mode(event.control),
        result=event.result,
    )
    previous = next(
        (receipt for receipt in state.run.receipts if receipt.decision_id == decision.decision_id),
        None,
    )
    if previous is not None:
        if previous.payload_digest != digest(decision):
            raise ContractError(("control_id",), "control identity result conflict")
        return Transition(state=state)
    timed = _advance_event_time(state, event)
    result = _submitted(
        timed, DecisionSubmitted(decision=decision, expected_revision=state.revision), dispatch
    )
    if not any(isinstance(feedback, Accepted) for feedback in result.events):
        return result
    updated_run = result.state.run.model_copy(
        update={"controls": (*result.state.run.controls, event.control)}
    )
    return result.model_copy(
        update={
            "state": result.state.model_copy(update={"run": updated_run}),
            "events": (ControlChanged(control=event.control), *result.events),
        }
    )


def _escalated(before: CoreState, result: Transition, dispatch: Dispatch) -> Transition:
    """Apply a stop control that turned a closing run's drain into a cancel.

    A stop only escalates (``stop_mode``). A cancel recorded once a drain Finish committed
    cancels at once what the drain was letting finish: the run's queued and running
    episodes, its own jobs and its sessions, as a cancel Finish does when it commits. A
    stop recorded before any Finish needs nothing here: the Finish applies the mode in
    force when it commits (``_decision_signal``).
    """
    after = result.state
    if stop_mode(before.run) is not StopMode.DRAIN or not _closing_under_cancel(after):
        return result
    cancelled = propagate(after, (AdmissionControl(action="cancel"),), dispatch)
    return cancelled.model_copy(
        update={
            "requests": (*result.requests, *cancelled.requests),
            "events": (*result.events, *cancelled.events),
        }
    )


def _closing_under_cancel(state: CoreState) -> bool:
    """Whether the run is closing on a committed Finish, and its stop in force cancels."""
    return (
        state.run.status == RunStatus.CLOSING
        and isinstance(committed_stop(state.run), Proven)
        and stop_mode(state.run) is StopMode.CANCEL
    )


def _finish_mode(control: ControlInput) -> Literal["drain", "cancel"]:
    """The operator's choice, given with the stop; core never picks one."""
    if control.mode is None:
        raise ContractError(("control", "mode"), "a stop names drain or cancel")
    return control.mode.value


def _soft_stop(state: CoreState, event: RunControlEvent) -> Transition:
    """Record a stop request that carries no result: the search is told and ends the run."""
    if any(control.control_id == event.control.control_id for control in state.run.controls):
        return Transition(state=state)
    refusal = control_refusal(state, event.control)
    if refusal is not None:
        raise refusal
    state = _advance_run_time(state, event.now_at)
    run = state.run.model_copy(update={"controls": (*state.run.controls, event.control)})
    return Transition(
        state=state.model_copy(update={"run": run}),
        events=(ControlChanged(control=event.control),),
    )


def _lifted(state: CoreState, control: ControlInput) -> tuple[CoreState, bool]:
    """The state a resume starts from, and whether it lifts a standing stop.

    A successor clears the previous epoch's closure and ending. Adoption authority is
    reopened independently; the workspace boundary guards writes to the operator root.
    """
    if control.action != "resume":
        return state, False
    lifting = stop_standing(state.run) or state.run.status in (RunStatus.TERMINAL, RunStatus.PARKED)
    if state.run.status == RunStatus.TERMINAL:
        state = state.model_copy(update={"run": reopened(state.run)})
    if lifting:
        state = state.model_copy(update={"settlement": reopen_adoption(state.settlement)})
    return state, lifting


def _control(state: CoreState, event: RunControlEvent, dispatch: Dispatch) -> Transition:
    previous = next(
        (
            control
            for control in state.run.controls
            if control.control_id == event.control.control_id
        ),
        None,
    )
    if previous is not None:
        if previous != event.control:
            raise ContractError(("control_id",), "control identity payload conflict")
        if event.control.action == "stop":
            return _escalated(state, _stop_control(state, event, dispatch), dispatch)
        return Transition(state=state)
    if event.control.action == "stop":
        return _escalated(state, _stop_control(state, event, dispatch), dispatch)
    refusal = control_refusal(state, event.control)
    if refusal is not None:
        raise refusal
    check_declared_target(event.control.target, state.run.declaration)
    new_epoch = state.run.status in (RunStatus.TERMINAL, RunStatus.PARKED)
    state, lifting = _lifted(state, event.control)
    # A capacity wait leaves the run paused: it only records when the run resumes itself.
    waits = event.control.when == "capacity_returns"
    # A stop returned above; park closes admission without ending the search.
    statuses = {
        "pause": (
            RunStatus.PAUSED if event.control.owner is PauseOwner.QUOTA else RunStatus.DRAINING
        ),
        "pause_now": RunStatus.DRAINING,
        "park": RunStatus.DRAINING,
        "resume": state.run.status if waits else RunStatus.RUNNING,
    }
    state = _advance_run_time(state, event.now_at)
    run = state.run.model_copy(
        update={
            "controls": (*state.run.controls, event.control),
            "status": statuses.get(event.control.action, state.run.status),
        }
    )
    run = _park_authority(state, run, event)
    updated = state.model_copy(update={"run": run})
    if run.status is RunStatus.DRAINING and run.park is not None:
        updated = _run_park.cap_to_drain_deadline(_turn_permit.cap_to_permits(updated))
    if event.control.action == "steer" or waits:
        result = Transition(state=updated)
    elif event.control.action in OPERATOR_ACTIONS:
        result = _operator_action(updated, event, dispatch)
    elif event.control.withdraws is not None:
        result = propagate(
            updated, (InputWithdrawRequested(input_id=event.control.withdraws),), dispatch
        )
    else:
        # Park closes intake while preserving queued work for relaunch.
        actions: dict[str, Literal["pause", "drain", "resume"]] = {
            "pause": "pause" if event.control.owner is PauseOwner.QUOTA else "drain",
            "pause_now": "drain",
            "park": "drain",
        }
        action = actions.get(event.control.action, "resume")
        result = propagate(updated, (AdmissionControl(action=action),), dispatch)
    lifted = (StopLiftRecorded(control=event.control, new_epoch=new_epoch),) if lifting else ()
    return result.model_copy(
        update={"events": (*result.events, ControlChanged(control=event.control), *lifted)}
    )


def _park_authority(state: CoreState, run: RunState, event: RunControlEvent) -> RunState:
    control = event.control
    if control.action in ("park", "pause_now") or (
        control.action == "pause" and control.owner is PauseOwner.OPERATOR
    ):
        return run.model_copy(
            update={
                "park": RunPark(
                    control_id=control.control_id,
                    epoch=state.records.recovery.epoch,
                    immediate=control.action == "pause_now",
                    admitted_turns=(
                        state.run.park.admitted_turns
                        if state.run.park is not None
                        else _run_park.admitted_turns(state)
                    ),
                    deadline_at=_run_park.drain_deadline(
                        event, state.run.now_at, state.run.limits.cleanup_bound
                    ),
                )
            }
        )
    if control.action == "resume":
        return run.model_copy(update={"park": None})
    return run


def _operator_action(state: CoreState, event: RunControlEvent, dispatch: Dispatch) -> Transition:
    """Apply an operator action core has recorded: its notice, then what it changes.

    A cancel withdraws each attempt it names as a search's own ``Withdraw`` would, with a
    ``Cancel`` disposition and the cleanup bound as its limit, and retires each queued
    reopening it ends; a new parallel limit retries admission. ``_operator_actions`` says what each action changes.
    """
    control = event.control
    signals: tuple[Signal, ...] = (notice(state, control, event.now_at),)
    if control.action == "set_max_parallel":
        signals += (ParallelLimitChanged(),)
    signals += reopen_retirements(state, control)
    result = propagate(state, signals, dispatch)
    within = Within(limit=state.run.limits.cleanup_bound)
    for withdraw in cancellations(state, control):
        current = result.state
        submitted = _submitted(
            current,
            DecisionSubmitted(
                decision=Wait(request=withdraw, until=within), expected_revision=current.revision
            ),
            dispatch,
        )
        result = Transition(
            state=submitted.state,
            requests=(*result.requests, *submitted.requests),
            events=(*result.events, *submitted.events),
        )
    return result


def _validate_observation_ingress(state: CoreState, event: CoreEvent) -> None:
    """Preserve registered root/target proofs before any state transition."""
    if (
        isinstance(event, RequestObserved)
        and any(
            value is not None
            for value in (
                event.outcome,
                event.operation_schema,
                event.outcome_schema,
                event.outcome_json,
            )
        )
        and not event.outcome_is_registered
    ):
        raise ContractError(("outcome",), "registered observation outcome proof required")
    if isinstance(event, RequestObserved) and event.target is not None:
        target = event.target
        if (
            any(
                value is not None
                for value in (
                    target.outcome,
                    target.operation_schema,
                    target.outcome_schema,
                    target.outcome_json,
                )
            )
        ) and not target.outcome_is_registered:
            raise ContractError(("target", "outcome"), "registered target outcome proof required")
        validate_inspection_target(state, event)
    if isinstance(event, RequestObserved):
        validate_registered_owner(state, event)


def _validate_dispatch_episode(state: CoreState, request: Request) -> None:
    """A prepared ordinary mutation never gains authority over a later episode."""
    if not dispatch_episode_current(state, request):
        raise ContractError(
            ("admission_id",), "ordinary mutation requires the current owned admission episode"
        )


def _registered_session_turn(state: CoreState, request: ExecuteRegisteredOperation) -> TurnSpec:
    """Resolve dispatch authority from the accepted canonical registered payload."""
    origin = operation_for(state.run.receipts, request)
    declaration = descriptor_matches(
        state.registry,
        state.run.capabilities,
        request.operation,
        LifecycleClass.SESSION_TURN,
        OperationNormalizationKind.NONE,
    )
    if (
        not isinstance(origin, Proven)
        or not isinstance(declaration, Proven)
        or origin.value.normalized_turn is None
    ):
        raise ContractError(
            ("decision_id",), "registered session dispatch requires canonical turn proof"
        )
    return origin.value.normalized_turn


def _builtin_session_turn(state: CoreState, request: DispatchTurn | ResumeSessionTurn) -> TurnSpec:
    """Resolve dispatch authority before classifying a caller-supplied turn.

    Paid and correction lifecycle policy remains owned by Sessions. A request
    cannot erase its decision origin or downgrade a resume to bypass proof fences.
    """
    origin = accepted_receipt_for(state.run.receipts, request.decision_id, None)
    receipt = origin.value if isinstance(origin, Proven) else None
    decision = receipt.decision if receipt is not None else None
    if (
        receipt is None
        or not isinstance(receipt.feedback, Accepted)
        or receipt.feedback.decision_id != receipt.decision_id
        or not isinstance(decision, RequestTurn)
        or decision.decision_id != receipt.decision_id
        or request.request_id not in receipt.request_ids
        or decision.scope != request.scope
        or decision.turn != request.turn
    ):
        raise ContractError(
            ("decision_id",), "builtin session dispatch requires canonical turn proof"
        )
    return decision.turn


def _validate_resume_authority(state: CoreState, request: Request) -> None:
    """Missing history/publication/checkpoint values never authorize a resume.

    These are shared proof fences. Evaluation B owns scientific exhaustion and
    timeout policy, while Sessions A owns charge, lease and checkpoint issuance.
    """
    if isinstance(request, DispatchTurn | ResumeSessionTurn):
        turn = _builtin_session_turn(state, request)
    elif (
        isinstance(request, ExecuteRegisteredOperation)
        and request.operation.schema_ref.lifecycle == LifecycleClass.SESSION_TURN
    ):
        turn = _registered_session_turn(state, request)
    else:
        return
    if turn.charge_class != "resume":
        if isinstance(request, ResumeSessionTurn):
            raise ContractError(("turn", "charge_class"), "resume request requires resume charge")
        return
    continuation = next(
        (
            row
            for row in state.evaluation.continuations
            if row.continuation_id == turn.continuation_id
        ),
        None,
    )
    successor = InvocationRef(
        session_id=turn.session.session_id,
        invocation_id=turn.invocation_id,
        generation=request.scope.generation,
    )
    receipt = continuation.authorization_receipt if continuation is not None else None
    preceding = next(
        (
            row
            for row in state.sessions.invocations
            if continuation is not None and row.invocation == continuation.invocation
        ),
        None,
    )
    if (
        continuation is None
        or receipt is None
        or preceding is None
        or preceding.scope != request.scope
        or preceding.turn.session != turn.session
        or continuation.invocation.session_id != successor.session_id
        or continuation.invocation == successor
        or receipt.continuation_id != turn.continuation_id
        or receipt.next_invocation != successor
        or receipt.timeout != continuation.timeout
        or continuation.next_invocation != successor
        or continuation.invocation.generation != request.scope.generation
        or continuation.phase not in (ContinuationPhase.AUTHORIZED, ContinuationPhase.RESUMED)
        or (
            isinstance(request, ResumeSessionTurn)
            and request.continuation_id != turn.continuation_id
        )
    ):
        raise ContractError(
            ("authorization_receipt",), "resume requires exact published successor proof"
        )
    _validate_resume_owner(state, request, turn, preceding, receipt.history_cursor)


def _validate_resume_owner(
    state: CoreState,
    request: Request,
    turn: TurnSpec,
    preceding: Invocation,
    publication_cursor: EvaluationHistoryCursor | None,
) -> None:
    """History belongs to the current attempt; run writer proof names its predecessor."""
    if isinstance(request.scope.owner, AttemptId):
        owner = next(
            (
                row
                for row in state.attempts.attempts
                if row.attempt_id == request.scope.owner
                and row.generation == request.scope.generation
            ),
            None,
        )
        if owner is None or exhausted(state.attempts, request.scope):
            raise ContractError(
                ("evaluation_history",), "resume requires complete unexhausted attempt history"
            )
        prefix = preceding.evaluation_prefix
        history = owner.evaluation_history
        # Coverage only grows, so the paid-cycle prefix is checked on the stored history.
        if prefix is None or paid_prefix_refusal(history, prefix) is not None:
            raise ContractError(("evaluation_prefix",), SuspensionRefusal.PREFIX_MISMATCH.detail)
        if (
            publication_cursor is None
            or publication_cursor.ordinal < prefix.ordinal
            or publication_cursor.ordinal > len(history.covered_submissions)
            or (
                publication_cursor.ordinal
                and history.covered_submissions[publication_cursor.ordinal - 1]
                != publication_cursor.submission_id
            )
        ):
            raise ContractError(
                ("authorization_receipt", "history_cursor"),
                "resume requires exact publication history prefix after paid-cycle start",
            )
        # The rule authorization applied, to the history it published (``published_history``).
        refusal = resume_refusal(published_history(history, publication_cursor), prefix)
        if refusal is not None:
            raise ContractError(
                ("evaluation_history",), "resume requires complete unexhausted attempt history"
            )
    else:
        if (
            request.scope.owner != state.run.run_id
            or request.scope.generation != state.run.generation
        ):
            raise ContractError(("scope",), "resume requires current run generation")
        if turn.session.access == Access.WRITE_CANDIDATE and not any(
            proof.scope == request.scope and proof.invocation == preceding.invocation
            for proof in state.sessions.run_checkpoints
        ):
            raise ContractError(
                ("run_checkpoints",), "run writer resume requires exact predecessor checkpoint"
            )


def _unique_record(state: CoreState, identity: RequestId) -> RequestRecord | None:
    candidates = state.records.find(identity)
    if len(candidates) > 1:
        raise ContractError(("request_id",), "ambiguous canonical request identity")
    return candidates[0] if candidates else None


def _validate_authorization(state: CoreState, event: CoreEvent) -> None:
    """Only dependencies and recovery proof authorize ordinary dispatch."""
    if isinstance(event, DispatchAuthorized):
        record = _unique_record(state, event.request_id)
        if (
            record is not None
            and state.records.recovery.phase != RecoveryPhase.READY
            and not retirement_dispatch(record.request)
            and not registered_retirement(state, record.request)
        ):
            raise ContractError(("recovery",), "ordinary dispatch requires ready recovery")
        if record is None or (
            request_dependency_status(record.request, state.run.receipts, state.records)
            != DependencyStatus.SUCCEEDED
        ):
            raise ContractError(
                ("dependency",), "dispatch requires successful dependency completion"
            )
        if not _run_park.dispatch_allowed(state, record.request):
            raise ContractError(("dependency",), "park holds new work until relaunch")
        _validate_dispatch_episode(state, record.request)
        _validate_resume_authority(state, record.request)


def _advance_event_time(state: CoreState, event: CoreEvent) -> CoreState:
    """Every supplied timestamp advances run time monotonically, without a clock."""
    supplied_times = [state.run.now_at]
    supplied_times.extend(
        stamp
        for field in ("now_at", "reached_at", "ended_at", "requested_at")
        if (stamp := getattr(event, field, None)) is not None
    )
    session_input = getattr(event, "input", None)
    if session_input is not None:
        supplied_times.append(session_input.received_at)
    if isinstance(event, SteerReceived):
        supplied_times.extend(item.received_at for item in event.inputs)
    observation = getattr(event, "observation", None)
    if observation is not None:
        supplied_times.append(observation.observed_at)
    target = getattr(event, "target", None)
    if target is not None and hasattr(target, "observation"):
        supplied_times.append(target.observation.observed_at)
    return _advance_run_time(state, max(supplied_times))


def _advance_run_time(state: CoreState, now_at: float) -> CoreState:
    """``state`` at run time ``now_at``, which never moves back (see ``credit_quota_pause``)."""
    now_at = max(now_at, state.run.now_at)
    if now_at == state.run.now_at:
        return state
    state = credit_quota_pause(state, now_at)
    return state.model_copy(update={"run": state.run.model_copy(update={"now_at": now_at})})


def _recover_writer(state: CoreState, event: RecoveryStarted, dispatch: Dispatch) -> Transition:
    """Reconcile a replacement writer without reopening the run or its flow journal."""
    park = state.run.park
    if park is not None and event.epoch > state.records.recovery.epoch:
        state = state.model_copy(
            update={
                "run": state.run.model_copy(
                    update={"park": park.model_copy(update={"epoch": event.epoch})}
                )
            }
        )
    opened = records.open_host_epoch(state.records, state.run, event)
    credit = downtime(opened.hosts) if opened.hosts != state.records.hosts else 0.0
    waits = evaluation.credit_downtime(state.evaluation, state.run, credit)
    state = state.model_copy(update={"records": opened, "evaluation": waits})
    state = _advance_event_time(state, event)
    return propagate(state, (event,), dispatch, _event_cause(state, event))


def _resume_epoch(state: CoreState, event: EpochResumed, dispatch: Dispatch) -> Transition:
    """Construct fresh run authority only for an explicit operator Resume."""
    previous_epoch = state.records.recovery.epoch
    restart_search = (
        state.run.park is not None or state.run.result is not None or stop_standing(state.run)
    )
    successor = previous_epoch > 0 and event.epoch > previous_epoch
    control = ControlInput(
        control_id=ControlId(root=f"epoch:{event.epoch}"), action="resume", when="now"
    )
    if successor:
        state = state.model_copy(
            update={
                "run": EpochFacts.of(state.run)
                .new_run()
                .model_copy(update={"controls": (control,)}),
                "scheduling": type(state.scheduling)(
                    queue=state.scheduling.queue,
                    slots=state.scheduling.slots,
                    released_slot_seconds=state.scheduling.released_slot_seconds,
                ),
                "settlement": reopen_adoption(state.settlement),
            }
        )
    result = _recover_writer(
        state, RecoveryStarted(epoch=event.epoch, now_at=event.now_at), dispatch
    )
    if successor and restart_search:
        result = result.model_copy(
            update={
                "events": (
                    *result.events,
                    StopLiftRecorded(
                        control=control,
                        new_epoch=True,
                    ),
                )
            }
        )
    return result


def consume(
    state: CoreState,
    event: CoreEvent,
    dispatch: Dispatch,
    compaction: Callable[[CoreState], CoreState] = compact_ledger,
) -> Transition:
    """Consume one top-level event, advancing the sole revision exactly once.

    The ledger is compacted (``compaction``) last, so what a step leaves durable holds no
    spent poll; compaction never changes what the step produced, only what it keeps.

    Whatever the event leaves equal comes back as the very object it was given. Every area's
    reducer builds its part of the next state its own way, and a part rebuilt with the same
    content would be a new object that every per-instance memo (immutability verdicts, JSON
    text) has never seen. One owner at the exit keeps that true for all of them.
    """
    given = state
    if parked_epoch_drops(state, event):
        return Transition(state=state)
    _validate_observation_ingress(state, event)
    _validate_authorization(state, event)
    if isinstance(event, DecisionCompleted | RecoveryReady | InputWithdrawRequested):
        raise ContractError(
            ("event",), "completion, readiness and withdrawal are internal lifecycle signals"
        )
    if isinstance(event, ProposalSubmitted):
        result = _proposal(state, event, dispatch)
    elif isinstance(event, DecisionSubmitted):
        result = _submitted(state, event, dispatch)
    elif isinstance(event, RunControlEvent):
        result = _control(state, event, dispatch)
    elif isinstance(event, RecoveryStarted):
        result = _recover_writer(state, event, dispatch)
    elif isinstance(event, EpochResumed):
        result = _resume_epoch(state, event, dispatch)
    else:
        state = _advance_event_time(state, event)
        result = propagate(state, (event,), dispatch, _event_cause(state, event))
    parked, cancellations = _run_park.advance(result.state)
    if cancellations:
        registered = _registered_transition(parked, list(cancellations), list(result.events))
        result = registered.model_copy(
            update={"requests": (*result.requests, *registered.requests)}
        )
    else:
        result = result.model_copy(update={"state": parked})
    unmet = unmet_waits(given, result.state)
    if unmet:
        raise ContractError(("wait",), f"a recorded wait breaks its contract: {unmet[0]}")
    advanced = compaction(result.state).model_copy(update={"revision": state.revision + 1})
    return result.model_copy(update={"state": share_unchanged(given, advanced)})


def step(state: CoreState, event: CoreEvent, *, reducers: CoreReducers | None = None) -> Transition:
    """Consume one event through explicit pure lifecycle implementations.

    Defaults use every production reducer. Optional sibling implementations keep
    the same kernel authority, validation, propagation and durable record path.
    """
    dispatch = _dispatch if reducers is None else partial(_dispatch, reducers=reducers)
    compaction = compact_ledger if reducers is None or reducers.ledger is None else reducers.ledger
    return consume(state, event, dispatch, compaction)
