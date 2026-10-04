"""Single revision authority, deterministic propagation and durable registration."""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from typing import assert_never

from pydantic import TypeAdapter

from . import attempts, evaluation, intents, scheduling, sessions, settlement
from ._registry import ContractError
from ._routing import SIGNAL_ORDER, event_area
from ._validation import validate_decision
from .types.attempts import (
    AttemptAdmitted,
    AttemptsEvent,
    CloseAttemptScope,
    DiscardWorkspace,
    EnsureWorkspace,
    RestoreRevision,
    RetainRevision,
    RetireRequested,
    SnapshotAndRetain,
)
from .types.common import (
    Area,
    AttemptRef,
    DependencyRef,
    InvocationRef,
    KernelNotImplementedError,
    LifecycleClass,
    OperationId,
    RejectionCode,
    RequestId,
    RunStatus,
    SettlementId,
    SignalCycleError,
    Value,
)
from .types.evaluation import (
    CancelOwnedJob,
    CollectEvidence,
    EvaluationEvent,
    InspectOwnedJob,
    MeasurementRequested,
    ObserveOwnedJob,
    SubmitMeasurement,
)
from .types.intents import (
    BlockIntent,
    CancelOwnedResource,
    ExecuteRegisteredOperation,
    InspectRequest,
    Intent,
    IntentPhase,
    IntentsEvent,
    IntentsState,
    Request,
)
from .types.kernel import (
    AreaChange,
    AreaContext,
    AttemptsContext,
    ControlChanged,
    CoreEvent,
    CoreState,
    DecisionReceipt,
    DecisionSubmitted,
    EvaluationContext,
    IntentsContext,
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
    AttemptRequest,
    AttemptRequested,
    ClockAdvanced,
    CloseAdmission,
    RunDrained,
    SchedulingEvent,
)
from .types.sessions import (
    CancelTurn,
    CloseSession,
    DispatchTurn,
    EnsureSession,
    InspectTurn,
    InterruptRequested,
    ResumeSessionTurn,
    SessionsEvent,
    TurnRequested,
)
from .types.settlement import (
    AdoptRevision,
    AssessmentSubmitted,
    Settlement,
    SettlementEvent,
    VerifyAdoption,
    WinnerProposed,
)
from .types.strategy import (
    Accepted,
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

MAX_SIGNALS = 1024

type Dispatch = Callable[[CoreState, Signal], AreaChange]


def digest(value: Value) -> str:
    """Deterministic value fingerprint, with no clock or random identity source."""
    return sha256(value.model_dump_json().encode()).hexdigest()


def _context[C: AreaContext](state: CoreState, model: type[C]) -> C:
    return model(**{name: getattr(state, name) for name in model.model_fields})


def _dispatch(state: CoreState, event: Signal) -> AreaChange:
    area = event_area(event)
    match area:
        case Area.SCHEDULING:
            change = scheduling.schedule(
                state.scheduling,
                _context(state, SchedulingContext),
                TypeAdapter(SchedulingEvent).validate_python(event),
            )
        case Area.ATTEMPTS:
            change = attempts.advance_attempt(
                state.attempts,
                _context(state, AttemptsContext),
                TypeAdapter(AttemptsEvent).validate_python(event),
            )
        case Area.SESSIONS:
            change = sessions.advance_session(
                state.sessions,
                _context(state, SessionsContext),
                TypeAdapter(SessionsEvent).validate_python(event),
            )
        case Area.EVALUATION:
            change = evaluation.advance_evaluation(
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


def _request_lifecycle(request: Request) -> LifecycleClass:
    match request:
        case ExecuteRegisteredOperation():
            lifecycle = request.operation.schema_ref.lifecycle
        case DispatchTurn() | ResumeSessionTurn():
            lifecycle = LifecycleClass.SESSION_TURN
        case SubmitMeasurement():
            lifecycle = LifecycleClass.OWNED_JOB
        case (
            InspectTurn()
            | ObserveOwnedJob()
            | InspectOwnedJob()
            | CollectEvidence()
            | InspectRequest()
            | VerifyAdoption()
        ):
            lifecycle = LifecycleClass.QUERY
        case (
            EnsureWorkspace()
            | RestoreRevision()
            | SnapshotAndRetain()
            | RetainRevision()
            | DiscardWorkspace()
            | CloseAttemptScope()
            | EnsureSession()
            | CancelTurn()
            | CloseSession()
            | CancelOwnedJob()
            | AdoptRevision()
            | CancelOwnedResource()
            | BlockIntent()
        ):
            lifecycle = LifecycleClass.IDEMPOTENT_WRITE
        case _:
            assert_never(request)
    return lifecycle


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
        lifecycle = _request_lifecycle(request)
        records.append(
            Intent(
                request_id=request_id,
                request=request,
                payload_digest=payload_digest,
                lifecycle=lifecycle,
                phase=IntentPhase.PREPARED,
                reconcile_deadline_at=min(request.deadline_at, state.run.deadline_at),
            )
        )
        allocated.append(request)
    return state.model_copy(update={"intents": IntentsState(intents=tuple(records))}), tuple(
        allocated
    )


def _kernel_signal(
    state: CoreState, signal: AdmitAttempt | CloseAdmission | RunDrained
) -> tuple[Transition, tuple[Signal, ...]]:
    if isinstance(signal, AdmitAttempt):
        decision = next(
            (
                receipt.decision
                for receipt in state.run.receipts
                if receipt.decision_id == signal.request.decision_id
            ),
            None,
        )
        if not isinstance(decision, StartAttempt):
            raise ContractError(("admission",), "signal has no registered StartAttempt")
        return Transition(state=state), (
            AttemptAdmitted(
                request=signal.request,
                workspace=decision.workspace,
                budget=decision.budget,
                initial_sessions=decision.initial_sessions,
            ),
        )
    if isinstance(signal, CloseAdmission):
        return Transition(state=state), (AdmissionControl(action="drain"),)
    if state.run.result is None:
        raise ContractError(("run", "result"), "drain has no registered stop proposal")
    if state.scheduling.queue or state.scheduling.slots:
        raise ContractError(("run", "drained"), "admission still owns queued or active work")
    pending_stop = next(
        (
            receipt.decision
            for receipt in reversed(state.run.receipts)
            if isinstance(receipt.decision, Stop)
        ),
        None,
    )
    if isinstance(pending_stop, Stop):
        dependencies = _decision_dependencies(state, pending_stop)
        phases = {intent.request_id: intent.phase for intent in state.intents.intents}
        if any(phases.get(identity) != IntentPhase.COMPLETED for identity in dependencies):
            return Transition(state=state), ()
    if state.run.status == RunStatus.TERMINAL:
        return Transition(state=state), ()
    run = state.run.model_copy(update={"status": RunStatus.TERMINAL})
    return Transition(
        state=state.model_copy(update={"run": run}), events=(RunEnded(result=state.run.result),)
    ), ()


def propagate(
    state: CoreState,
    initial: tuple[Signal, ...],
    dispatch: Dispatch = _dispatch,
    dependencies: tuple[RequestId, ...] = (),
) -> Transition:
    """Apply typed signals to quiescence in fixed area order, rejecting cycles."""
    pending = list(initial)
    seen: set[tuple[Area, str]] = set()
    requests: list[Request] = []
    events: list[StrategyEvent] = []
    while pending:
        pending.sort(key=lambda signal: SIGNAL_ORDER.index(event_area(signal)))
        signal = pending.pop(0)
        key = (event_area(signal), digest(signal))
        if key in seen:
            raise SignalCycleError(signal.kind)
        seen.add(key)
        if len(seen) > MAX_SIGNALS:
            raise SignalCycleError("propagation-bound")
        if isinstance(signal, AdmitAttempt | CloseAdmission | RunDrained):
            result, signals = _kernel_signal(state, signal)
            state = result.state
            pending.extend(signals)
            events.extend(result.events)
            continue
        change = dispatch(state, signal)
        area = event_area(signal)
        expected = type(getattr(state, area.value))
        if type(change.state) is not expected:
            raise ContractError((area.value, "state"), "reducer returned another area state")
        state = state.model_copy(update={area.value: change.state})
        pending.extend(change.signals)
        requests.extend(change.requests)
        events.extend(change.events)
    proposed = tuple(
        request.model_copy(
            update={"depends_on": tuple(dict.fromkeys((*dependencies, *request.depends_on)))}
        )
        for request in requests
    )
    state, allocated = register_requests(state, proposed)
    return Transition(state=state, requests=allocated, events=tuple(events))


def _reject(
    decision: Decision, code: RejectionCode, path: tuple[str | int, ...], detail: str
) -> Rejected:
    return Rejected(decision_id=decision.decision_id, code=code, path=path, detail=detail)


def _withdraw_signal(decision: Withdraw) -> tuple[Signal, ...]:
    target = decision.target
    if isinstance(decision.disposition, Interrupt) and isinstance(target, InvocationRef):
        return (InterruptRequested(invocation=target, refund=decision.disposition.refund),)
    if not isinstance(target, AttemptRef):
        raise ContractError(("target",), "retirement requires attempt target")
    if isinstance(decision.disposition, Settle):
        proposal = decision.disposition
        value = Settlement(
            settlement_id=SettlementId(root=f"settlement:{decision.decision_id.root}"),
            attempt=target,
            candidate=None,
            assessments=proposal.assessments,
            eligible=proposal.eligible,
            retention=proposal.retention,
            outcome=proposal.outcome,
        )
        return (AssessmentSubmitted(settlement=value),)
    if isinstance(decision.disposition, Interrupt):
        raise ContractError(("target",), "interrupt requires invocation target")
    return (RetireRequested(attempt=target, disposition=decision.disposition.kind),)


def _decision_signal(decision: Decision) -> tuple[Signal, ...]:
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
            signals = _withdraw_signal(decision)
        case Measure():
            signals = (MeasurementRequested(scope=decision.scope, plan=decision.plan),)
        case ProposeWinner():
            signals = (WinnerProposed(selection=decision.selection),)
        case Stop():
            signals = (AdmissionControl(action=decision.mode),)
        case Operation():
            # Custom operations enter the same registered outbox before shell dispatch.
            signals = ()
        case _:
            assert_never(decision)

    return signals


def _decision_dependencies(state: CoreState, decision: Decision) -> tuple[RequestId, ...]:
    requests: list[RequestId] = []
    for identity in decision.depends_on:
        receipt = next(receipt for receipt in state.run.receipts if receipt.decision_id == identity)
        if isinstance(receipt.feedback, Accepted):
            requests.extend(receipt.feedback.request_ids)
    return tuple(dict.fromkeys(requests))


def _submitted(state: CoreState, event: DecisionSubmitted, dispatch: Dispatch) -> Transition:
    decision = event.decision
    payload_digest = digest(decision)
    previous = next(
        (receipt for receipt in state.run.receipts if receipt.decision_id == decision.decision_id),
        None,
    )
    if previous is not None:
        if previous.payload_digest == payload_digest:
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
    rejection = validate_decision(state, event)
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
    dependencies = _decision_dependencies(state, decision)
    try:
        result = propagate(updated, _decision_signal(decision), dispatch, dependencies)
    except KernelNotImplementedError as error:
        rejection = _reject(decision, error.code, (error.area.value,), str(error))
        receipt = receipt.model_copy(update={"feedback": rejection, "decision": None})
        run = state.run.model_copy(update={"receipts": (*state.run.receipts, receipt)})
        return Transition(state=state.model_copy(update={"run": run}), events=(rejection,))
    if isinstance(decision, Operation):
        wire = decision.registered_wire
        if wire is None:
            raise ContractError(("operation",), "missing registered ingress proof")
        request = ExecuteRegisteredOperation(
            scope=decision.scope,
            deadline_at=decision.deadline_at,
            operation_id=OperationId(root=f"operation:{decision.decision_id.root}"),
            operation=wire,
            depends_on=dependencies,
            retry_limit=state.run.limits.max_retries,
        )
        registered, requests = register_requests(result.state, (request,))
        result = Transition(state=registered, requests=requests, events=result.events)
    request_ids = tuple(
        request.request_id for request in result.requests if request.request_id is not None
    )
    feedback = Accepted(
        decision_id=decision.decision_id,
        allocated_ids=tuple(identity.root for identity in request_ids),
        request_ids=request_ids,
        dependencies=tuple(DependencyRef(request_id=identity) for identity in dependencies),
    )
    receipt = receipt.model_copy(update={"feedback": feedback})
    run = result.state.run.model_copy(
        update={"receipts": (*result.state.run.receipts[:-1], receipt)}
    )
    return result.model_copy(
        update={
            "state": result.state.model_copy(update={"run": run}),
            "events": (feedback, *result.events),
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
        return Transition(state=state)
    if state.run.status == RunStatus.TERMINAL:
        raise ContractError(("run", "status"), "terminal run rejects controls")
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


def consume(state: CoreState, event: CoreEvent, dispatch: Dispatch) -> Transition:
    """Consume one top-level event, advancing the sole revision exactly once."""
    if isinstance(event, DecisionSubmitted):
        result = _submitted(state, event, dispatch)
    elif isinstance(event, RunControlEvent):
        result = _control(state, event, dispatch)
    else:
        if isinstance(event, ClockAdvanced):
            state = state.model_copy(
                update={
                    "run": state.run.model_copy(
                        update={"now_at": max(state.run.now_at, event.now_at)}
                    )
                }
            )
        result = propagate(state, (event,), dispatch)
    return result.model_copy(
        update={"state": result.state.model_copy(update={"revision": state.revision + 1})}
    )


def step(state: CoreState, event: CoreEvent) -> Transition:
    """Consume one event through the declared area reducers, without I/O."""
    return consume(state, event, _dispatch)
