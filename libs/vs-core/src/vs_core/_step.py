"""Single revision authority, deterministic propagation and durable registration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, assert_never

from pydantic import TypeAdapter

from . import attempts, evaluation, intents, scheduling, sessions, settlement
from ._inspection import validate_inspection_target, validate_registered_owner
from ._ownership import cleanup_pending
from ._proofs import (
    Missing,
    ProofReason,
    Proven,
    accepted_receipt_for,
    committed_stop,
    current_admission,
    dependencies_for,
    descriptor_matches,
    observation_for,
    occupied_episode,
    operation_for,
)
from ._registry import ContractError
from ._routing import SIGNAL_ORDER, event_area
from ._validation import validate_decision
from ._values import digest
from .types.attempts import (
    AttemptAdmitted,
    AttemptPhase,
    AttemptRegistered,
    AttemptsEvent,
    CloseAttemptScope,
    DiscardWorkspace,
    RetainRevision,
    RetireRequested,
    ScopeReopenAdmitted,
    SnapshotAndRetain,
)
from .types.common import (
    Area,
    AttemptId,
    AttemptRef,
    CompletionStatus,
    DecisionId,
    DependencyRef,
    DependencyStatus,
    InvocationRef,
    KernelNotImplementedError,
    LifecycleClass,
    OperationId,
    OperationNormalizationKind,
    OperationRef,
    RejectionCode,
    RequestBase,
    RequestId,
    RevisionAuthority,
    RunStatus,
    Scope,
    SessionId,
    SettlementId,
    SignalCycleError,
)
from .types.evaluation import (
    CancelOwnedJob,
    ContinuationPhase,
    ContinuationReopenRequested,
    EvaluationEvent,
    InspectOwnedJob,
    MeasurementRequested,
    ObserveOwnedJob,
)
from .types.evaluation_history import EvaluationHistoryAvailability, EvaluationHistoryCursor
from .types.intents import (
    BlockIntent,
    CancelOwnedResource,
    DecisionDependencyResolved,
    DispatchAuthorized,
    ExecuteRegisteredOperation,
    InspectRequest,
    Intent,
    IntentPhase,
    IntentsEvent,
    OperationResult,
    OperationRetireRequested,
    RecoveryPhase,
    RecoveryReady,
    Request,
    RequestObserved,
    RequestPrepared,
    request_lifecycle,
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
    EvaluationContext,
    IntentsContext,
    ProposalSubmitted,
    RunControlEvent,
    RunEnded,
    SchedulingContext,
    SessionsContext,
    SettlementContext,
    Signal,
    StrategyEvent,
    Transition,
)
from .types.scheduling import (
    AdmissionControl,
    AdmitAttempt,
    AttemptReopenRequest,
    AttemptRequest,
    AttemptRequested,
    ClockAdvanced,
    CloseAdmission,
    RegisterAttempt,
    RunDrained,
    SchedulingEvent,
)
from .types.session_inputs import InputDropped, InputDropReason
from .types.sessions import (
    Access,
    CancelTurn,
    CloseSession,
    DispatchTurn,
    InspectTurn,
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
    SettlementDependencyResolved,
    SettlementEvent,
    WinnerProposed,
)
from .types.strategy import (
    Accepted,
    Cancel,
    Decision,
    Interrupt,
    Measure,
    Operation,
    ProposeWinner,
    Rejected,
    RequestTurn,
    Settle,
    StartAttempt,
    Stop,
    Withdraw,
)

if TYPE_CHECKING:
    from .attempts import Reducer as AttemptsReducer
    from .evaluation import Reducer as EvaluationReducer

MAX_SIGNALS = 1024

type Dispatch = Callable[[CoreState, Signal], AreaChange]


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


_DEFAULT_REDUCERS = CoreReducers()


def _context[C: AreaContext](state: CoreState, model: type[C]) -> C:
    return model(**{name: getattr(state, name) for name in model.model_fields})


def _dispatch(
    state: CoreState, event: Signal, *, reducers: CoreReducers = _DEFAULT_REDUCERS
) -> AreaChange:
    area = event_area(event)
    match area:
        case Area.SCHEDULING:
            change = scheduling.schedule(
                state.scheduling,
                _context(state, SchedulingContext),
                TypeAdapter(SchedulingEvent).validate_python(event),
            )
        case Area.ATTEMPTS:
            reducer = attempts.advance_attempt if reducers.attempts is None else reducers.attempts
            change = reducer(
                state.attempts,
                _context(state, AttemptsContext),
                TypeAdapter(AttemptsEvent).validate_python(event),
            )
        case Area.SESSIONS:
            change = sessions.advance_session(
                state.sessions,
                _context(state, SessionsContext),
                TypeAdapter(SessionsEvent).validate_python(event),
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
                TypeAdapter(EvaluationEvent).validate_python(event),
            )
        case Area.SETTLEMENT:
            change = settlement.settle(
                state.settlement,
                _context(state, SettlementContext),
                TypeAdapter(SettlementEvent).validate_python(event),
            )
        case Area.INTENTS:
            change = intents.advance_intent(
                state.intents,
                _context(state, IntentsContext),
                TypeAdapter(IntentsEvent).validate_python(event),
            )
        case _:
            assert_never(area)
    return change


def _reconcile_deadline(state: CoreState, request: Request) -> float:
    """Cap work before run expiry, preserving bounded cleanup after expiry.

    New timers never precede the supplied transition time. Existing intents and
    the request's execution deadline are unchanged; elapsed work deadlines do
    not cancel the separate bounded reconciliation and cleanup authority.
    """
    deadline = request.deadline_at
    if state.run.deadline_at > state.run.now_at:
        deadline = min(deadline, state.run.deadline_at)
    return max(state.run.now_at, deadline)


def register_requests(
    state: CoreState, requests: tuple[Request, ...]
) -> tuple[CoreState, tuple[Request, ...]]:
    """Allocate stable identities and persist every proposed request in the outbox."""
    records = list(state.intents.intents)
    allocated: list[Request] = []
    for index, proposal in enumerate(requests):
        request_id = proposal.request_id or RequestId(
            root=f"{state.run.run_id.root}:{state.revision}:{index}:{digest(proposal)[:16]}"
        )
        request = proposal.model_copy(update={"request_id": request_id})
        payload_digest = digest(request)
        previous = next((record for record in records if record.request_id == request_id), None)
        if previous is not None:
            if previous.payload_digest != payload_digest:
                raise ContractError(("request_id", request_id.root), "request identity conflict")
            continue
        lifecycle = request_lifecycle(request)
        records.append(
            Intent(
                request_id=request_id,
                request=request,
                payload_digest=payload_digest,
                lifecycle=lifecycle,
                phase=IntentPhase.PREPARED,
                reconcile_deadline_at=_reconcile_deadline(state, request),
            )
        )
        allocated.append(request)
    receipts = tuple(
        receipt.model_copy(
            update={
                "request_ids": tuple(
                    dict.fromkeys(
                        (
                            *receipt.request_ids,
                            *(
                                request.request_id
                                for request in allocated
                                if request.decision_id == receipt.decision_id
                            ),
                        )
                    )
                )
            }
        )
        for receipt in state.run.receipts
    )
    return state.model_copy(
        update={
            "intents": state.intents.model_copy(update={"intents": tuple(records)}),
            "run": state.run.model_copy(update={"receipts": receipts}),
        }
    ), tuple(allocated)


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
    """Admissions resolve canonical decisions; recovery only wakes scheduling."""
    if isinstance(signal, RecoveryReady):
        barrier = state.intents.recovery
        if signal.epoch != barrier.epoch:
            return Transition(state=state), ()
        if barrier.phase != RecoveryPhase.READY:
            raise ContractError(
                ("recovery", "phase"), "ready notification requires committed barrier proof"
            )
        return Transition(state=state), (ClockAdvanced(now_at=state.run.now_at),)
    return _admission_signal(state, signal)


def validate_terminal_inputs(
    before: SessionsState, change: AreaChange[SessionsState], now_at: float
) -> None:
    """Validate the pure input-finalization output before terminal publication.

    Preserve every occurrence, payload, sibling field and existing receipt.
    Each pending occurrence gains one RUN_TERMINAL drop at now_at and exactly
    one matching event. Finalization emits no requests or signals. Raises
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
            or new.receipt.reason != InputDropReason.RUN_TERMINAL
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


def _stop_dependencies_succeeded(state: CoreState) -> bool:
    """Finality uses one canonical Stop and its exact successful dependencies."""
    stop = committed_stop(state.run)
    if not isinstance(stop, Proven):
        return False
    prerequisites = RequestBase(
        scope=stop.value.scope,
        deadline_at=state.run.deadline_at,
        decision_dependencies=stop.value.depends_on,
    )
    return isinstance(dependencies_for(prerequisites, state.run.receipts, state.intents), Proven)


def _kernel_signal(
    state: CoreState,
    signal: RegisterAttempt | AdmitAttempt | CloseAdmission | RunDrained | RecoveryReady,
) -> tuple[Transition, tuple[Signal, ...]]:
    if isinstance(signal, RegisterAttempt | AdmitAttempt | RecoveryReady):
        return _activation_signal(state, signal)
    if isinstance(signal, CloseAdmission):
        return Transition(state=state), (AdmissionControl(action="drain"),)
    if state.run.result is None:
        raise ContractError(("run", "result"), "drain has no registered stop proposal")
    if cleanup_pending(state):
        return Transition(state=state), ()
    if not _stop_dependencies_succeeded(state):
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
    for request in change.requests:
        if (
            isinstance(request, ExecuteRegisteredOperation)
            and operation_owner(state, request) != area
        ):
            raise ContractError(
                ("operation", "owner"), "registered execution bypassed owning lifecycle"
            )


def _scope_admission(state: CoreState, scope: Scope | None) -> DecisionId | None:
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


def _signal_admission(
    state: CoreState, signal: Signal, inherited: DecisionId | None = None
) -> DecisionId | None:
    """Keep an observation's original episode through successor signal propagation."""
    observation = getattr(signal, "observation", None)
    identity = getattr(observation, "request_id", None)
    intent = next((row for row in state.intents.intents if row.request_id == identity), None)
    if intent is not None:
        return intent.request.admission_id
    explicit = getattr(signal, "admission_id", None) or getattr(observation, "admission_id", None)
    scope = getattr(signal, "scope", None) or getattr(
        getattr(signal, "request", None), "scope", None
    )
    return explicit or inherited or _scope_admission(state, scope)


def propagate(
    state: CoreState,
    initial: tuple[Signal, ...],
    dispatch: Dispatch = _dispatch,
    dependencies: tuple[RequestId, ...] = (),
    initial_cause: tuple[DecisionId | None, tuple[DecisionId, ...]] = (None, ()),
) -> Transition:
    """Apply typed signals to quiescence in fixed area order, rejecting cycles."""
    pending = [(signal, *initial_cause, _signal_admission(state, signal)) for signal in initial]
    seen: set[tuple[Area, str]] = set()
    requests: list[Request] = []
    events: list[StrategyEvent] = []
    while pending:
        pending.sort(key=lambda entry: SIGNAL_ORDER.index(event_area(entry[0])))
        signal, cause, requires, admission_id = pending.pop(0)
        admission_id = _signal_admission(state, signal, admission_id)
        observation = getattr(signal, "observation", None)
        if observation is not None and any(
            intent.request_id == observation.request_id for intent in state.intents.intents
        ):
            cause, requires = _event_cause(state, signal)
        if isinstance(signal, DecisionCompleted):
            completed, notifications = _complete_decision(state, signal)
            state = completed.state
            events.extend(completed.events)
            pending.extend(
                (child, None, (), _signal_admission(state, child)) for child in notifications
            )
            continue
        if isinstance(signal, RegisterAttempt | AdmitAttempt):
            cause = signal.request.decision_id
            admission_id = signal.request.decision_id
            origin = accepted_receipt_for(state.run.receipts, cause, None)
            if not isinstance(origin, Proven) or origin.value.decision is None:
                raise ContractError(("admission",), "signal requires accepted canonical admission")
            requires = origin.value.decision.depends_on
        key = (event_area(signal), digest(signal))
        if key in seen:
            raise SignalCycleError(signal.kind)
        seen.add(key)
        if len(seen) > MAX_SIGNALS:
            raise SignalCycleError("propagation-bound")
        if isinstance(
            signal, RegisterAttempt | AdmitAttempt | CloseAdmission | RunDrained | RecoveryReady
        ):
            result, signals = _kernel_signal(state, signal)
            state = result.state
            pending.extend((child, cause, requires, admission_id) for child in signals)
            events.extend(result.events)
            continue
        change = dispatch(state, signal)
        area = event_area(signal)
        expected = type(getattr(state, area.value))
        if type(change.state) is not expected:
            raise ContractError((area.value, "state"), "reducer returned another area state")
        state = state.model_copy(update={area.value: change.state})
        pending.extend((child, cause, requires, admission_id) for child in change.signals)
        _validate_area_outputs(state, area, change, cause)
        requests.extend(
            request.model_copy(
                update={
                    "decision_id": request.decision_id or cause,
                    "admission_id": request.admission_id or admission_id,
                    "decision_dependencies": tuple(
                        dict.fromkeys((*requires, *request.decision_dependencies))
                    ),
                }
            )
            for request in change.requests
        )
        events.extend(change.events)
    return _registered_transition(state, requests, events, dependencies)


def _registered_transition(
    state: CoreState,
    requests: list[Request],
    events: list[StrategyEvent],
    dependencies: tuple[RequestId, ...],
) -> Transition:
    """Register effects atomically and place terminal publication last."""
    if state.run.status == RunStatus.TERMINAL and requests:
        raise ContractError(("requests",), "terminal transition cannot publish new requests")
    proposed = tuple(
        request.model_copy(
            update={"depends_on": tuple(dict.fromkeys((*dependencies, *request.depends_on)))}
        )
        for request in requests
    )
    state, allocated = register_requests(state, proposed)
    ended = tuple(event for event in events if isinstance(event, RunEnded))
    ordinary_events = tuple(event for event in events if not isinstance(event, RunEnded))
    return Transition(state=state, requests=allocated, events=(*ordinary_events, *ended))


def dependency_status(state: CoreState, request: Request) -> DependencyStatus:
    """Prepared requests remain fenced until all semantic prerequisites succeed."""
    if request.decision_id is not None and not isinstance(
        accepted_receipt_for(state.run.receipts, request.decision_id, None), Proven
    ):
        return DependencyStatus.FAILED
    proof = dependencies_for(request, state.run.receipts, state.intents)
    if isinstance(proof, Proven):
        return DependencyStatus.SUCCEEDED
    if isinstance(proof, Missing) and proof.reason in (
        ProofReason.UNRESOLVED,
        ProofReason.ABSENT_REQUEST,
    ):
        return DependencyStatus.PENDING
    return DependencyStatus.FAILED


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
    receipts: list[DecisionReceipt] = []
    events: list[StrategyEvent] = []
    failed = set() if event.status == CompletionStatus.SUCCEEDED else {event.decision_id}
    for original in state.run.receipts:
        item = original
        if item.decision_id == event.decision_id:
            item = item.model_copy(update={"completion": event.status})
        elif (
            item.decision is not None
            and isinstance(accepted_receipt_for(state.run.receipts, item.decision_id, None), Proven)
            and failed.intersection(item.decision.depends_on)
        ):
            rejection = _reject(
                item.decision,
                RejectionCode.DEPENDENCY,
                ("depends_on",),
                "dependency failed or cancelled",
            )
            item = item.model_copy(
                update={"feedback": rejection, "completion": CompletionStatus.FAILED}
            )
            failed.add(item.decision_id)
            events.append(rejection)
        receipts.append(item)
    updated = state.model_copy(
        update={"run": state.run.model_copy(update={"receipts": tuple(receipts)})}
    )
    notifications = (
        (DecisionDependencyResolved(decision_id=event.decision_id, status=event.status),)
        if any(
            event.decision_id in intent.request.decision_dependencies
            for intent in state.intents.intents
        )
        else ()
    )
    completed = (
        (event.decision_id, event.status),
        *(
            (identity, CompletionStatus.FAILED)
            for identity in sorted(failed - {event.decision_id}, key=lambda identity: identity.root)
        ),
    )
    settlement_notifications = tuple(
        SettlementDependencyResolved(decision_id=identity, status=status)
        for identity, status in completed
        if any(
            receipt.decision is not None
            and isinstance(
                accepted_receipt_for(state.run.receipts, receipt.decision_id, None), Proven
            )
            and identity in receipt.decision.depends_on
            and isinstance(receipt.decision, Withdraw)
            and isinstance(receipt.decision.disposition, Settle)
            for receipt in state.run.receipts
        )
    )
    return Transition(state=updated, events=tuple(events)), (
        *notifications,
        *settlement_notifications,
    )


def _event_cause(
    state: CoreState, event: Signal
) -> tuple[DecisionId | None, tuple[DecisionId, ...]]:
    observation = getattr(event, "observation", None)
    if observation is not None:
        intent = _unique_intent(state, observation.request_id)
        if intent is not None:
            if not isinstance(observation_for(intent, observation), Proven):
                raise ContractError(
                    ("observation",), "differs from canonical request scope or episode"
                )
            return intent.request.decision_id, intent.request.decision_dependencies
    return None, ()


def _reject(
    decision: Decision, code: RejectionCode, path: tuple[str | int, ...], detail: str
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


def _decision_signal(state: CoreState, decision: Decision) -> tuple[Signal, ...]:
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
            signals = (TurnRequested(scope=decision.scope, turn=decision.turn),)
        case Withdraw():
            signals = _withdraw_signal(state, decision)
        case Measure():
            signals = (MeasurementRequested(scope=decision.scope, plan=decision.plan),)
        case ProposeWinner():
            signals = (WinnerProposed(selection=decision.selection),)
        case Stop():
            signals = (AdmissionControl(action=decision.mode),)
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
        deadline_at=decision.deadline_at,
        operation_id=OperationId(root=f"operation:{decision.decision_id.root}"),
        operation=wire,
        retry_limit=state.run.limits.max_retries,
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
        LifecycleClass.QUERY: Area.INTENTS,
        LifecycleClass.IDEMPOTENT_WRITE: Area.INTENTS,
        LifecycleClass.OWNED_JOB: Area.EVALUATION,
        LifecycleClass.SESSION_TURN: Area.SESSIONS,
    }
    return owners[descriptor.lifecycle]


def _decision_replay(state: CoreState, decision: Decision) -> Transition | None:
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


def _submitted(
    state: CoreState, event: DecisionSubmitted, dispatch: Dispatch, *, check_revision: bool = True
) -> Transition:
    decision = event.decision
    payload_digest = digest(decision)
    replay = _decision_replay(state, decision)
    if replay is not None:
        return replay
    rejection = validate_decision(state, event, check_revision=check_revision)
    feedback = rejection or Accepted(decision_id=decision.decision_id)
    receipt = DecisionReceipt(
        decision_id=decision.decision_id,
        decision=None if rejection is not None else decision,
        payload_digest=payload_digest,
        feedback=feedback,
    )
    updated = state.model_copy(
        update={"run": state.run.model_copy(update={"receipts": (*state.run.receipts, receipt)})}
    )
    if rejection is not None:
        return Transition(state=updated, events=(rejection,))
    if isinstance(decision, Stop):
        updated = updated.model_copy(
            update={
                "run": updated.run.model_copy(
                    update={"result": decision.result, "status": RunStatus.CLOSING}
                )
            }
        )
    dependencies: tuple[RequestId, ...] = ()
    try:
        result = propagate(
            updated,
            _decision_signal(updated, decision),
            dispatch,
            dependencies,
            (decision.decision_id, decision.depends_on),
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
        dependencies=tuple(DependencyRef(decision_id=identity) for identity in decision.depends_on),
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
    for decision in event.decisions:
        replay = _decision_replay(state, decision)
        if replay is not None:
            events.extend(replay.events)
            continue
        rejection = _reject(
            decision, RejectionCode.STALE_VIEW, ("expected_revision",), "view revision changed"
        )
        receipt = DecisionReceipt(
            decision_id=decision.decision_id, payload_digest=digest(decision), feedback=rejection
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
    """Use the ordinary accepted Stop receipt as run-drain authority."""
    if event.result is None:
        raise ContractError(("result",), "stop requires a run result proposal")
    decision = Stop(
        decision_id=DecisionId(root=f"control:{event.control.control_id.root}"),
        scope=Scope(owner=state.run.run_id, generation=state.run.generation),
        mode="drain",
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
            return _stop_control(state, event, dispatch)
        return Transition(state=state)
    if event.control.action == "stop":
        return _stop_control(state, event, dispatch)
    if state.run.status == RunStatus.TERMINAL:
        raise ContractError(("run", "status"), "terminal run rejects controls")
    if event.control.action in ("resume", "pause") and (
        state.run.status in (RunStatus.CLOSING, RunStatus.BLOCKED) or state.run.result is not None
    ):
        raise ContractError(("run", "cleanup"), "cleanup must finish before pause or resume")
    statuses = {
        "pause": RunStatus.PAUSED,
        "resume": RunStatus.RUNNING,
        "stop": RunStatus.CLOSING,
        "steer": state.run.status,
    }
    run = state.run.model_copy(
        update={
            "controls": (*state.run.controls, event.control),
            "now_at": max(state.run.now_at, event.now_at),
            "status": statuses[event.control.action],
        }
    )
    updated = state.model_copy(update={"run": run})
    if event.control.action == "steer":
        result = Transition(state=updated)
    else:
        action = "drain" if event.control.action == "stop" else event.control.action
        result = propagate(updated, (AdmissionControl(action=action),), dispatch)
    return result.model_copy(
        update={"events": (*result.events, ControlChanged(control=event.control))}
    )


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


def _retirement_dispatch(request: Request) -> bool:
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
        | BlockIntent,
    )


def _episode_recorded(state: CoreState, request: Request) -> bool:
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
    ) or (
        isinstance(decision, Operation)
        and decision.scope.owner == state.run.run_id
        and decision.normalized_scope_reopen is not None
        and decision.normalized_scope_reopen.attempt == target
    )


def _retirement_target_matches(
    state: CoreState, request: Request, decision: Withdraw | Stop
) -> bool:
    if not _episode_recorded(state, request):
        return False
    if isinstance(decision, Stop):
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
        isinstance(intent.request, ExecuteRegisteredOperation)
        and intent.request.operation_id == target.operation_id
        and intent.request.scope == request.scope
        for intent in state.intents.intents
    )


def _session_retirement_matches(state: CoreState, request: CloseSession) -> bool:
    """A reusable conversation cannot be closed by a previous admission's request."""
    if (
        isinstance(request.scope.owner, AttemptId)
        and _scope_admission(state, request.scope) != request.admission_id
    ):
        return False
    return any(
        session.spec.session_id == request.session_id and session.scope == request.scope
        for session in state.sessions.sessions
    )


def _closure_retirement(state: CoreState, request: Request) -> bool:
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


def _registered_retirement(state: CoreState, request: Request) -> bool:
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
        and isinstance(authority.value.decision, Withdraw | Stop)
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
            or _scope_admission(state, request.scope) == request.admission_id
        )
        and (not isinstance(request, CloseSession) or _session_retirement_matches(state, request))
    )


def _validate_dispatch_episode(state: CoreState, request: Request) -> None:
    """A prepared ordinary mutation never gains authority over a later episode."""
    if not isinstance(request.scope.owner, AttemptId):
        return
    if (
        request_lifecycle(request) == LifecycleClass.QUERY
        or _retirement_dispatch(request)
        or _registered_retirement(state, request)
    ):
        return
    owner = next(
        (
            attempt
            for attempt in state.attempts.attempts
            if attempt.attempt_id == request.scope.owner
            and attempt.generation == request.scope.generation
        ),
        None,
    )
    if (
        not isinstance(current_admission(owner, request.scope, request.admission_id), Proven)
        or owner is None
        or owner.phase not in (AttemptPhase.ACQUIRING, AttemptPhase.ACTIVE)
    ):
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
        or decision.turn.deadline_at != request.deadline_at
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
        if (
            owner is None
            or owner.evaluation_history.availability != EvaluationHistoryAvailability.COMPLETE
            or owner.terminal_reason is not None
        ):
            raise ContractError(
                ("evaluation_history",), "resume requires complete unexhausted attempt history"
            )
        prefix = preceding.evaluation_prefix
        history = owner.evaluation_history
        if (
            prefix is None
            or prefix.ordinal > len(history.covered_submissions)
            or (
                prefix.ordinal
                and history.covered_submissions[prefix.ordinal - 1] != prefix.submission_id
            )
        ):
            raise ContractError(
                ("evaluation_prefix",), "attempt resume requires exact paid-cycle history prefix"
            )
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


def _unique_intent(state: CoreState, identity: RequestId) -> Intent | None:
    candidates = tuple(item for item in state.intents.intents if item.request_id == identity)
    if len(candidates) > 1:
        raise ContractError(("request_id",), "ambiguous canonical request identity")
    return candidates[0] if candidates else None


def _validate_authorization(state: CoreState, event: CoreEvent) -> None:
    """Only dependencies and recovery proof authorize ordinary dispatch."""
    if isinstance(event, DispatchAuthorized):
        intent = _unique_intent(state, event.request_id)
        if (
            intent is not None
            and state.intents.recovery.phase != RecoveryPhase.READY
            and not _retirement_dispatch(intent.request)
            and not _registered_retirement(state, intent.request)
        ):
            raise ContractError(("recovery",), "ordinary dispatch requires ready recovery")
        if intent is None or dependency_status(state, intent.request) != DependencyStatus.SUCCEEDED:
            raise ContractError(
                ("dependency",), "dispatch requires successful dependency completion"
            )
        _validate_dispatch_episode(state, intent.request)
        _validate_resume_authority(state, intent.request)


def _advance_event_time(state: CoreState, event: CoreEvent) -> CoreState:
    """Every supplied timestamp advances run time monotonically, without a clock."""
    supplied_times = [state.run.now_at]
    supplied_times.extend(
        getattr(event, field)
        for field in ("now_at", "reached_at", "ended_at", "requested_at")
        if hasattr(event, field)
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
    return state.model_copy(
        update={"run": state.run.model_copy(update={"now_at": max(supplied_times)})}
    )


def consume(state: CoreState, event: CoreEvent, dispatch: Dispatch) -> Transition:
    """Consume one top-level event, advancing the sole revision exactly once."""
    _validate_observation_ingress(state, event)
    _validate_authorization(state, event)
    if isinstance(event, DecisionCompleted | RecoveryReady):
        raise ContractError(("event",), "completion/readiness is an internal lifecycle signal")
    if isinstance(event, ProposalSubmitted):
        result = _proposal(state, event, dispatch)
    elif isinstance(event, DecisionSubmitted):
        result = _submitted(state, event, dispatch)
    elif isinstance(event, RunControlEvent):
        result = _control(state, event, dispatch)
    else:
        state = _advance_event_time(state, event)
        cause, requires = _event_cause(state, event)
        result = propagate(state, (event,), dispatch, initial_cause=(cause, requires))
    return result.model_copy(
        update={"state": result.state.model_copy(update={"revision": state.revision + 1})}
    )


def step(state: CoreState, event: CoreEvent, *, reducers: CoreReducers | None = None) -> Transition:
    """Consume one event through explicit pure lifecycle implementations.

    Defaults use every production reducer. Optional sibling implementations keep
    the same kernel authority, validation, propagation and durable intent path.
    """
    dispatch = _dispatch if reducers is None else partial(_dispatch, reducers=reducers)
    return consume(state, event, dispatch)
