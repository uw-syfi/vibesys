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
from .types.attempts import AttemptAdmitted, AttemptsEvent, RetireRequested
from .types.common import (
    Area,
    AttemptRef,
    InvocationRef,
    KernelNotImplementedError,
    LifecycleClass,
    OperationId,
    OperationSchemaRef,
    RejectionCode,
    RequestId,
    RunStatus,
    SettlementId,
    SignalCycleError,
    Value,
)
from .types.evaluation import EvaluationEvent, MeasurementRequested
from .types.intents import (
    ExecuteRegisteredOperation,
    Intent,
    IntentPhase,
    IntentsEvent,
    IntentsState,
    OperationWire,
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
    CloseAdmission,
    RunDrained,
    SchedulingEvent,
)
from .types.sessions import InterruptRequested, SessionsEvent, TurnRequested
from .types.settlement import AssessmentSubmitted, Settlement, SettlementEvent, WinnerProposed
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
    return model(
        run=state.run,
        registry=state.registry,
        scheduling=state.scheduling,
        attempts=state.attempts,
        sessions=state.sessions,
        evaluation=state.evaluation,
        settlement=state.settlement,
        intents=state.intents,
    )


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


def register_requests(
    state: CoreState, requests: tuple[Request, ...]
) -> tuple[CoreState, tuple[Request, ...]]:
    """Allocate stable identities and persist every proposed request in the outbox."""
    records = list(state.intents.intents)
    allocated: list[Request] = []
    for index, proposal in enumerate(requests):
        request_id = proposal.request_id or RequestId(
            f"{state.run.run_id.root}:{state.revision}:{index}:{digest(proposal)[:16]}"
        )
        request = proposal.model_copy(update={"request_id": request_id})
        payload_digest = digest(request)
        previous = next((record for record in records if record.request_id == request_id), None)
        if previous is not None:
            if previous.payload_digest != payload_digest:
                raise ContractError(("request_id", request_id.root), "request identity conflict")
            continue
        lifecycle = (
            request.operation.schema_ref.lifecycle
            if isinstance(request, ExecuteRegisteredOperation)
            else LifecycleClass.IDEMPOTENT_WRITE
        )
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
                if receipt.decision.decision_id == signal.request.decision_id
            ),
            None,
        )
        if not isinstance(decision, StartAttempt):
            raise ContractError(("admission",), "signal has no registered StartAttempt")
        return Transition(state=state), (
            AttemptAdmitted(
                request=signal.request, workspace=decision.workspace, budget=decision.budget
            ),
        )
    if isinstance(signal, CloseAdmission):
        return Transition(state=state), (AdmissionControl(action="drain"),)
    if state.run.result is None:
        raise ContractError(("run", "result"), "drain has no registered stop proposal")
    if state.run.status == RunStatus.TERMINAL:
        return Transition(state=state), ()
    run = state.run.model_copy(update={"status": RunStatus.TERMINAL})
    return Transition(
        state=state.model_copy(update={"run": run}), events=(RunEnded(result=state.run.result),)
    ), ()


def propagate(
    state: CoreState, initial: tuple[Signal, ...], dispatch: Dispatch = _dispatch
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
    state, allocated = register_requests(state, tuple(requests))
    return Transition(state=state, requests=allocated, events=tuple(events))


def _reject(
    decision: Decision, code: RejectionCode, path: tuple[str | int, ...], detail: str
) -> Rejected:
    return Rejected(decision_id=decision.decision_id, code=code, path=path, detail=detail)


def _withdraw_signal(decision: Withdraw) -> tuple[Signal, ...]:
    target = decision.target
    if isinstance(decision.disposition, Interrupt) and isinstance(target, InvocationRef):
        return (InterruptRequested(invocation=target),)
    if not isinstance(target, AttemptRef):
        raise ContractError(("target",), "retirement requires attempt target")
    if isinstance(decision.disposition, Settle):
        proposal = decision.disposition
        value = Settlement(
            settlement_id=SettlementId(f"settlement:{decision.decision_id.root}"),
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


def _submitted(state: CoreState, event: DecisionSubmitted, dispatch: Dispatch) -> Transition:
    decision = event.decision
    payload_digest = digest(decision)
    previous = next(
        (
            receipt
            for receipt in state.run.receipts
            if receipt.decision.decision_id == decision.decision_id
        ),
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
    receipt = DecisionReceipt(decision=decision, payload_digest=payload_digest, feedback=feedback)
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
    try:
        result = propagate(updated, _decision_signal(decision), dispatch)
    except KernelNotImplementedError as error:
        rejection = _reject(decision, error.code, (error.area.value,), str(error))
        receipt = receipt.model_copy(update={"feedback": rejection})
        run = state.run.model_copy(update={"receipts": (*state.run.receipts, receipt)})
        return Transition(state=state.model_copy(update={"run": run}), events=(rejection,))
    if isinstance(decision, Operation):
        descriptor = next(entry for entry in state.registry if entry.kind == decision.request.kind)

        schema = OperationSchemaRef(
            kind=descriptor.kind,
            request_schema=descriptor.request_schema,
            outcome_schema=descriptor.outcome_schema,
            lifecycle=descriptor.lifecycle,
        )
        request = ExecuteRegisteredOperation(
            scope=decision.scope,
            deadline_at=decision.deadline_at,
            operation_id=OperationId(f"operation:{decision.decision_id.root}"),
            operation=OperationWire(
                schema_ref=schema, payload_json=decision.request.model_dump_json()
            ),
            retry_limit=state.run.limits.max_retries,
        )
        registered, requests = register_requests(result.state, (request,))
        result = Transition(state=registered, requests=requests, events=result.events)
    return result.model_copy(update={"events": (feedback, *result.events)})


def consume(state: CoreState, event: CoreEvent, dispatch: Dispatch) -> Transition:
    """Consume one top-level event, advancing the sole revision exactly once."""
    if isinstance(event, DecisionSubmitted):
        result = _submitted(state, event, dispatch)
    elif isinstance(event, RunControlEvent):
        action = (
            "drain"
            if event.control.action == "stop"
            else "pause"
            if event.control.action == "steer"
            else event.control.action
        )
        result = propagate(state, (AdmissionControl(action=action),), dispatch)
        result = result.model_copy(
            update={"events": (*result.events, ControlChanged(control=event.control))}
        )
    else:
        result = propagate(state, (event,), dispatch)
    return result.model_copy(
        update={"state": result.state.model_copy(update={"revision": state.revision + 1})}
    )


def step(state: CoreState, event: CoreEvent) -> Transition:
    """Consume one event through the declared area reducers, without I/O."""
    return consume(state, event, _dispatch)
