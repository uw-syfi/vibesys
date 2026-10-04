"""Production dispatcher exercised with declared wave-1 area output values."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    AttemptId,
    Cancel,
    ClockAdvanced,
    ContractError,
    ControlId,
    ControlInput,
    CoreState,
    DecisionId,
    DecisionSubmitted,
    InspectRequest,
    IntentPhase,
    IntentsChange,
    Interrupt,
    InterruptRequested,
    InvocationId,
    InvocationRef,
    OperationId,
    OperationRef,
    OperationRetireRequested,
    RecoveryStarted,
    ReducerTrace,
    RequestId,
    RunControlEvent,
    RunId,
    RunStatus,
    SchedulingChange,
    Scope,
    SessionId,
    SessionsChange,
    SignalCycleError,
    TraceFrame,
    Withdraw,
    initial_state,
    project,
    step,
    trace_step,
)


@given(st.floats(min_value=0, max_value=100, allow_nan=False, allow_infinity=False))
def test_request_registration_is_deterministic_immutable_and_serializable(now: float) -> None:
    state = initial_state()
    signal = ClockAdvanced(now_at=now)
    request = InspectRequest(
        scope=Scope(owner=state.run.run_id, generation=0),
        deadline_at=100.0,
        target=RequestId(root="previous"),
    )
    trace = ReducerTrace(
        frames=(
            TraceFrame(
                signal=signal, change=SchedulingChange(state=state.scheduling, requests=(request,))
            ),
        )
    )
    before = state.model_dump_json()
    result = trace_step(state, signal, trace)
    assert result == trace_step(state, signal, trace)
    assert state.model_dump_json() == before
    assert result.state.revision == state.revision + 1
    assert result.requests[0].request_id is not None
    intent = result.state.intents.intents[0]
    assert intent.phase == IntentPhase.PREPARED
    assert intent.request == result.requests[0]
    restarted = CoreState.model_validate_json(result.state.model_dump_json())
    assert restarted == result.state
    assert type(restarted.intents.intents[0].request.scope.owner) is RunId


def test_attempt_scope_keeps_its_distinct_identity_on_wire() -> None:

    scope = Scope(owner=AttemptId(root="same-text-as-run"), generation=4)
    assert Scope.model_validate_json(scope.model_dump_json()) == scope
    assert type(Scope.model_validate_json(scope.model_dump_json()).owner) is AttemptId


def test_fixed_order_and_one_revision_for_propagated_signals() -> None:
    state = initial_state()
    clock = ClockAdvanced(now_at=1.0)
    next_clock = ClockAdvanced(now_at=2.0)
    recovery = RecoveryStarted(now_at=1.0)
    trace = ReducerTrace(
        frames=(
            TraceFrame(
                signal=clock,
                change=SchedulingChange(state=state.scheduling, signals=(recovery, next_clock)),
            ),
            TraceFrame(signal=next_clock, change=SchedulingChange(state=state.scheduling)),
            TraceFrame(signal=recovery, change=IntentsChange(state=state.intents)),
        )
    )
    assert trace_step(state, clock, trace).state.revision == 1


def test_cyclic_signals_are_rejected_instead_of_spinning() -> None:
    state = initial_state()
    clock = ClockAdvanced(now_at=1.0)
    trace = ReducerTrace(
        frames=(
            TraceFrame(
                signal=clock, change=SchedulingChange(state=state.scheduling, signals=(clock,))
            ),
        )
    )
    with pytest.raises(SignalCycleError):
        trace_step(state, clock, trace)


def test_reusing_explicit_request_id_with_conflicting_payload_is_rejected() -> None:
    state = initial_state()
    clock = ClockAdvanced(now_at=1.0)
    request = InspectRequest(
        request_id=RequestId(root="stable"),
        scope=Scope(owner=state.run.run_id, generation=0),
        deadline_at=10.0,
        target=RequestId(root="one"),
    )
    conflict = request.model_copy(update={"target": RequestId(root="two")})
    trace = ReducerTrace(
        frames=(
            TraceFrame(
                signal=clock,
                change=SchedulingChange(state=state.scheduling, requests=(request, conflict)),
            ),
        )
    )
    with pytest.raises(ContractError, match="request identity conflict"):
        trace_step(state, clock, trace)


def test_kernel_projects_time_and_persisted_controls_without_turning_steer_into_pause() -> None:
    state = initial_state()
    clock = ClockAdvanced(now_at=5.0)
    result = trace_step(
        state,
        clock,
        ReducerTrace(
            frames=(TraceFrame(signal=clock, change=SchedulingChange(state=state.scheduling)),)
        ),
    )
    assert project(result.state).run.now_at == 5.0
    control = ControlInput(control_id=ControlId(root="steer"), action="steer")
    event = RunControlEvent(control=control, now_at=6.0)
    changed = step(result.state, event)
    assert changed.state.run.status == RunStatus.RUNNING
    assert project(changed.state).controls == (control,)
    assert len(changed.events) == 1
    replay = step(changed.state, event)
    assert replay.events == replay.requests == ()
    assert replay.state.run.controls == (control,)


@given(st.integers(min_value=0, max_value=100))
def test_interrupt_refund_is_preserved_through_kernel_routing(refund: int) -> None:
    state = initial_state()
    capabilities = state.run.capabilities.model_copy(update={"lifecycle": frozenset({"interrupt"})})
    state = state.model_copy(
        update={"run": state.run.model_copy(update={"capabilities": capabilities})}
    )
    invocation = InvocationRef(
        session_id=SessionId(root="session"),
        invocation_id=InvocationId(root="invocation"),
        generation=0,
    )
    decision = Withdraw(
        decision_id=DecisionId(root="interrupt"),
        scope=Scope(owner=state.run.run_id, generation=0),
        target=invocation,
        disposition=Interrupt(refund=refund),
    )
    result = trace_step(
        state,
        DecisionSubmitted(decision=decision, expected_revision=0),
        ReducerTrace(
            frames=(
                TraceFrame(
                    signal=InterruptRequested(invocation=invocation, refund=refund),
                    change=SessionsChange(state=state.sessions),
                ),
            )
        ),
    )
    assert result.state.scheduling == state.scheduling


def test_operation_retirement_routes_to_owning_intents_area() -> None:
    state = initial_state()
    scope = Scope(owner=state.run.run_id, generation=0)
    target = OperationRef(operation_id=OperationId(root="owned-operation"), generation=0)
    decision = Withdraw(
        decision_id=DecisionId(root="retire-operation"),
        scope=scope,
        target=target,
        disposition=Cancel(),
    )
    result = trace_step(
        state,
        DecisionSubmitted(decision=decision, expected_revision=0),
        ReducerTrace(
            frames=(
                TraceFrame(
                    signal=OperationRetireRequested(operation=target, scope=scope),
                    change=IntentsChange(state=state.intents),
                ),
            )
        ),
    )
    assert result.state.revision == 1
    assert result.events[0].kind == "accepted"
