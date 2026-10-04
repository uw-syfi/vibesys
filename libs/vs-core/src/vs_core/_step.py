"""Single revision authority, deterministic propagation and durable registration."""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from typing import assert_never

from pydantic import TypeAdapter

from . import attempts, evaluation, intents, scheduling, sessions, settlement
from ._ownership import cleanup_pending
from ._registry import ContractError
from ._routing import SIGNAL_ORDER, event_area
from ._validation import validate_decision
from ._values import canonical_json
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
    CompletionStatus,
    DecisionId,
    DependencyRef,
    DependencyStatus,
    InvocationRef,
    KernelNotImplementedError,
    LifecycleClass,
    OperationId,
    OperationRef,
    RejectionCode,
    RequestId,
    RevisionAuthority,
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
    DecisionDependencyResolved,
    DispatchAuthorized,
    ExecuteRegisteredOperation,
    InspectRequest,
    Intent,
    IntentPhase,
    IntentsEvent,
    IntentsState,
    OperationResult,
    OperationRetireRequested,
    Request,
    RequestObserved,
    RequestPrepared,
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

MAX_SIGNALS = 1024

type Dispatch = Callable[[CoreState, Signal], AreaChange]


def digest(value: Value) -> str:
    """Deterministic value fingerprint, with no clock or random identity source."""
    return sha256(canonical_json(value).encode()).hexdigest()


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
            "intents": IntentsState(intents=tuple(records)),
            "run": state.run.model_copy(update={"receipts": receipts}),
        }
    ), tuple(allocated)


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
    if cleanup_pending(state):
        return Transition(state=state), ()
    pending_stop = next(
        (
            receipt.decision
            for receipt in reversed(state.run.receipts)
            if isinstance(receipt.decision, Stop)
        ),
        None,
    )
    if isinstance(pending_stop, Stop):
        completions = {receipt.decision_id: receipt.completion for receipt in state.run.receipts}
        if any(
            completions.get(identity) != CompletionStatus.SUCCEEDED
            for identity in pending_stop.depends_on
        ):
            return Transition(state=state), ()
    if state.run.status == RunStatus.TERMINAL:
        return Transition(state=state), ()
    run = state.run.model_copy(update={"status": RunStatus.TERMINAL})
    return Transition(
        state=state.model_copy(update={"run": run}), events=(RunEnded(result=state.run.result),)
    ), ()


class _LeafRejectionError(Exception):
    """Unaccepted area changes must roll back through the submission boundary."""

    def __init__(self, feedback: Rejected) -> None:
        self.feedback = feedback
        super().__init__(feedback.detail)


def _validate_area_outputs(
    state: CoreState, area: Area, change: AreaChange, cause: DecisionId | None
) -> None:
    for event in change.events:
        if isinstance(event, Accepted):
            raise ContractError(("feedback",), "only the kernel issues acceptance")
        if isinstance(event, Rejected) and event.decision_id == cause:
            raise _LeafRejectionError(event)
        if isinstance(event, OperationResult) and not event.outcome_is_registered:
            raise ContractError(("outcome",), "registered callback outcome proof required")
    for request in change.requests:
        if (
            isinstance(request, ExecuteRegisteredOperation)
            and operation_owner(state, request) != area
        ):
            raise ContractError(
                ("operation", "owner"), "registered execution bypassed owning lifecycle"
            )


def propagate(
    state: CoreState,
    initial: tuple[Signal, ...],
    dispatch: Dispatch = _dispatch,
    dependencies: tuple[RequestId, ...] = (),
    initial_cause: tuple[DecisionId | None, tuple[DecisionId, ...]] = (None, ()),
) -> Transition:
    """Apply typed signals to quiescence in fixed area order, rejecting cycles."""
    pending = [(signal, *initial_cause) for signal in initial]
    seen: set[tuple[Area, str]] = set()
    requests: list[Request] = []
    events: list[StrategyEvent] = []
    while pending:
        pending.sort(key=lambda entry: SIGNAL_ORDER.index(event_area(entry[0])))
        signal, cause, requires = pending.pop(0)
        if isinstance(signal, DecisionCompleted):
            completed, notifications = _complete_decision(state, signal)
            state = completed.state
            events.extend(completed.events)
            pending.extend((child, None, ()) for child in notifications)
            continue
        if isinstance(signal, AdmitAttempt):
            cause = signal.request.decision_id
            owner = next(receipt for receipt in state.run.receipts if receipt.decision_id == cause)
            requires = owner.decision.depends_on if owner.decision is not None else ()
        key = (event_area(signal), digest(signal))
        if key in seen:
            raise SignalCycleError(signal.kind)
        seen.add(key)
        if len(seen) > MAX_SIGNALS:
            raise SignalCycleError("propagation-bound")
        if isinstance(signal, AdmitAttempt | CloseAdmission | RunDrained):
            result, signals = _kernel_signal(state, signal)
            state = result.state
            pending.extend((child, cause, requires) for child in signals)
            events.extend(result.events)
            continue
        change = dispatch(state, signal)
        area = event_area(signal)
        expected = type(getattr(state, area.value))
        if type(change.state) is not expected:
            raise ContractError((area.value, "state"), "reducer returned another area state")
        state = state.model_copy(update={area.value: change.state})
        pending.extend((child, cause, requires) for child in change.signals)
        _validate_area_outputs(state, area, change, cause)
        requests.extend(
            request.model_copy(
                update={
                    "decision_id": request.decision_id or cause,
                    "decision_dependencies": tuple(
                        dict.fromkeys((*requires, *request.decision_dependencies))
                    ),
                }
            )
            for request in change.requests
        )
        events.extend(change.events)
    proposed = tuple(
        request.model_copy(
            update={"depends_on": tuple(dict.fromkeys((*dependencies, *request.depends_on)))}
        )
        for request in requests
    )
    state, allocated = register_requests(state, proposed)
    return Transition(state=state, requests=allocated, events=tuple(events))


def dependency_status(state: CoreState, request: Request) -> DependencyStatus:
    """Prepared requests remain fenced until all semantic prerequisites succeed."""
    receipts = {receipt.decision_id: receipt for receipt in state.run.receipts}
    owner = receipts.get(request.decision_id)
    if owner is not None and isinstance(owner.feedback, Rejected):
        return DependencyStatus.FAILED
    intents_by_id = {intent.request_id: intent for intent in state.intents.intents}
    for identity in request.decision_dependencies:
        receipt = receipts.get(identity)
        if (
            receipt is None
            or isinstance(receipt.feedback, Rejected)
            or receipt.completion
            in (
                CompletionStatus.FAILED,
                CompletionStatus.CANCELLED,
            )
        ):
            return DependencyStatus.FAILED
    if any(receipts[identity].completion is None for identity in request.decision_dependencies):
        return DependencyStatus.PENDING
    for identity in request.depends_on:
        intent = intents_by_id.get(identity)
        if intent is None or intent.phase != IntentPhase.COMPLETED:
            return DependencyStatus.PENDING
        if intent.observation is None or intent.observation.status.value != "succeeded":
            return DependencyStatus.FAILED
    return DependencyStatus.SUCCEEDED


def _complete_decision(
    state: CoreState, event: DecisionCompleted
) -> tuple[Transition, tuple[Signal, ...]]:
    receipt = next(
        (item for item in state.run.receipts if item.decision_id == event.decision_id), None
    )
    if receipt is None or not isinstance(receipt.feedback, Accepted):
        raise ContractError(("decision_id",), "completion requires accepted decision")
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
            and isinstance(item.feedback, Accepted)
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
    return Transition(state=updated, events=tuple(events)), notifications


def _event_cause(
    state: CoreState, event: CoreEvent
) -> tuple[DecisionId | None, tuple[DecisionId, ...]]:
    observation = getattr(event, "observation", None)
    if observation is not None:
        intent = next(
            (item for item in state.intents.intents if item.request_id == observation.request_id),
            None,
        )
        if intent is not None:
            return intent.request.decision_id, intent.request.decision_dependencies
    return None, ()


def _reject(
    decision: Decision, code: RejectionCode, path: tuple[str | int, ...], detail: str
) -> Rejected:
    return Rejected(decision_id=decision.decision_id, code=code, path=path, detail=detail)


def _withdraw_signal(decision: Withdraw) -> tuple[Signal, ...]:
    target = decision.target
    if isinstance(decision.disposition, Interrupt) and isinstance(target, InvocationRef):
        return (InterruptRequested(invocation=target, refund=decision.disposition.refund),)
    if isinstance(decision.disposition, Cancel) and isinstance(target, OperationRef):
        return (OperationRetireRequested(operation=target, scope=decision.scope),)
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
            signals = _withdraw_signal(decision)
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


def _operation_prepared(state: CoreState, decision: Operation) -> RequestPrepared:
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
    return RequestPrepared(
        request=request,
        lifecycle=decision.request.lifecycle,
        normalized_turn=decision.normalized_turn,
    )


def operation_owner(state: CoreState, request: ExecuteRegisteredOperation) -> Area:
    """One declared authority governs each registered execution request."""
    descriptor = next(
        (item for item in state.registry if item.kind == request.operation.schema_ref.kind), None
    )
    if descriptor is None:
        raise ContractError(("operation",), "unregistered dispatch")
    if descriptor.revision_authority != RevisionAuthority.NONE:
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


def consume(state: CoreState, event: CoreEvent, dispatch: Dispatch) -> Transition:
    """Consume one top-level event, advancing the sole revision exactly once."""
    if (
        isinstance(event, RequestObserved)
        and (event.outcome is not None or event.operation_schema is not None)
        and not event.outcome_is_registered
    ):
        raise ContractError(("outcome",), "registered observation outcome proof required")
    if isinstance(event, DispatchAuthorized):
        intent = next(
            (item for item in state.intents.intents if item.request_id == event.request_id), None
        )
        if intent is None or dependency_status(state, intent.request) != DependencyStatus.SUCCEEDED:
            raise ContractError(
                ("dependency",), "dispatch requires successful dependency completion"
            )
    if isinstance(event, DecisionCompleted):
        raise ContractError(("event",), "completion is an internal lifecycle signal")
    if isinstance(event, ProposalSubmitted):
        result = _proposal(state, event, dispatch)
    elif isinstance(event, DecisionSubmitted):
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
        cause, requires = _event_cause(state, event)
        result = propagate(state, (event,), dispatch, initial_cause=(cause, requires))
    return result.model_copy(
        update={"state": result.state.model_copy(update={"revision": state.revision + 1})}
    )


def step(state: CoreState, event: CoreEvent) -> Transition:
    """Consume one event through the declared area reducers, without I/O."""
    return consume(state, event, _dispatch)
