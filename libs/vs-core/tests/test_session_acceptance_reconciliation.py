"""Final output does not discharge unknown input acceptance or conversation reuse."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_session_sibling_fakes import fake_session_inputs


def scope() -> core.Scope:
    return core.Scope(owner=core.initial_state().run.run_id, generation=0)


def turn(identity: str = "planner", charge: str = "free") -> core.TurnSpec:
    return core.TurnSpec.model_validate(
        {
            "session": core.SessionSpec(
                session_id=core.SessionId(root="session"),
                role_id=core.RoleId(root="planner"),
                policy="fresh",
                lifetime="ephemeral",
                access=core.Access.READ_ONLY,
            ),
            "invocation_id": core.InvocationId(root=identity),
            "workspace": scope(),
            "prompts": (),
            "output_schema": core.SchemaRef(name="plan", version=1),
            "deadline_at": 100.0,
            "charge_class": charge,
        }
    )


def invocation(spec: core.TurnSpec) -> core.InvocationRef:
    return core.InvocationRef(
        session_id=spec.session.session_id, invocation_id=spec.invocation_id, generation=0
    )


def waiting_turn_state(spec: core.TurnSpec) -> core.CoreState:
    state = core.initial_state()
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={"limits": state.run.limits.model_copy(update={"max_retries": 2})}
            )
        }
    )
    return state.model_copy(
        update={
            "sessions": core.SessionsState(
                sessions=(
                    core.SessionView(
                        spec=spec.session,
                        scope=scope(),
                        generation=0,
                        phase=core.SessionPhase.IDLE,
                        invocation=spec.invocation_id,
                        resource_id=core.ResourceId(root="conversation"),
                    ),
                ),
                invocations=(
                    core.Invocation(
                        invocation=invocation(spec),
                        scope=scope(),
                        turn=spec,
                        phase=core.SessionPhase.ACQUIRING,
                    ),
                ),
                run_charges=(
                    core.ChargeReceipt(
                        charge_id=core.ChargeId(root="turn-charge"),
                        kind=core.ChargeKind.TURN,
                        invocation_id=spec.invocation_id,
                        charged=1,
                    ),
                ),
            )
        }
    )


def turn_observation(
    request: core.Request, *, terminal: bool, status: core.ObservationStatus
) -> core.Observation:
    assert request.request_id is not None
    return core.Observation(
        event_id=core.EventId(root="proof"),
        request_id=request.request_id,
        scope=request.scope,
        sequence=1,
        observed_at=1.0,
        status=status,
        resource_id=core.ResourceId(root="conversation"),
        terminal=terminal,
    )


def reload_step(state: core.CoreState, event: core.CoreEvent) -> core.Transition:
    before = state.model_dump_json()
    result = core.step(state, event, reducers=core.CoreReducers(session_inputs=fake_session_inputs))
    assert state.model_dump_json() == before
    assert (
        core.step(
            core.CoreState.model_validate_json(before),
            event,
            reducers=core.CoreReducers(session_inputs=fake_session_inputs),
        )
        == result
    )
    return result


def unacknowledged_success() -> tuple[core.Transition, core.TurnObserved]:
    spec = turn()
    ref = invocation(spec)
    record = core.InputRecord(
        input=core.SessionInput(
            input_id=core.InputId(root="reserved"),
            target=core.ScopeInputTarget(scope=scope()),
            artifact=core.ArtifactRef(
                artifact_id=core.ArtifactId(root="payload"), digest="payload-digest"
            ),
            received_at=0.0,
            sequence=0,
        ),
        reserved_to=ref,
    )
    unrelated = record.model_copy(
        update={
            "input": record.input.model_copy(update={"input_id": core.InputId(root="unrelated")}),
            "reserved_to": None,
        }
    )
    state = waiting_turn_state(spec)
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"inputs": (record, unrelated)})}
    )
    dispatched = reload_step(
        state, core.TurnInputsReserved(invocation=ref, input_ids=(record.input.input_id,))
    )
    event = core.TurnObserved(
        invocation=ref,
        observation=turn_observation(
            dispatched.requests[0], terminal=True, status=core.ObservationStatus.SUCCEEDED
        ),
        output_schema=spec.output_schema,
        output_json='{"plan":"completed"}',
    )
    return dispatched, event


def test_unacknowledged_success_requests_exact_inspection_and_fences_session_reuse() -> None:
    dispatched, event = unacknowledged_success()
    result = reload_step(dispatched.state, event)
    assert len(result.requests) == 1
    inspection = result.requests[0]
    assert isinstance(inspection, core.InspectTurn)
    assert inspection.invocation == event.invocation
    assert inspection.scope == event.observation.scope
    assert result.state.sessions.sessions[0].phase == core.SessionPhase.UNKNOWN
    assert result.state.sessions.invocations[0].phase == core.SessionPhase.TERMINAL
    assert result.state.sessions.inputs == dispatched.state.sessions.inputs
    assert result.state.sessions.run_charges == dispatched.state.sessions.run_charges
    assert isinstance(result.events[0], core.TurnResult)
    assert result.events[0].output_json == event.output_json
    with pytest.raises(core.ContractValidationError, match="session"):
        core.step(result.state, core.TurnRequested(scope=scope(), turn=turn(identity="successor")))
    replay = reload_step(result.state, event)
    assert replay.requests == ()
    assert replay.events == ()
    assert inspection in core.pending_requests(replay.state.intents)


@given(
    sequences=st.lists(st.integers(min_value=1, max_value=30), min_size=1, max_size=12),
    charge=st.sampled_from(("free", "correction")),
)
def test_unacknowledged_terminal_proof_keeps_one_inspection_and_reserved_payload(
    sequences: list[int], charge: str
) -> None:
    dispatched, event = unacknowledged_success()
    state = dispatched.state
    inspections = []
    outputs = []
    for sequence in (1, *sequences):
        observation = event.observation.model_copy(
            update={"sequence": sequence, "event_id": core.EventId(root=f"proof-{sequence}")}
        )
        result = reload_step(state, event.model_copy(update={"observation": observation}))
        inspections.extend(row for row in result.requests if isinstance(row, core.InspectTurn))
        outputs.extend(row for row in result.events if isinstance(row, core.TurnResult))
        assert result.state.sessions.inputs == dispatched.state.sessions.inputs
        assert result.state.sessions.sessions[0].phase == core.SessionPhase.UNKNOWN
        successor = turn(identity="successor", charge=charge).model_copy(
            update={"predecessor": event.invocation} if charge == "correction" else {}
        )
        with pytest.raises(core.ContractValidationError, match="session"):
            core.step(result.state, core.TurnRequested(scope=scope(), turn=successor))
        state = result.state
    assert len(inspections) == 1
    assert len(outputs) == 1
    pending = core.pending_requests(state.intents)
    assert len([row for row in pending if isinstance(row, core.InspectTurn)]) == 1


@given(st.lists(st.integers(min_value=1, max_value=30), max_size=15))
def test_correlated_acceptance_reopens_only_the_current_unknown_session(
    sequences: list[int],
) -> None:
    dispatched, event = unacknowledged_success()
    result = reload_step(dispatched.state, event)
    confirmation = event.model_copy(
        update={
            "observation": event.observation.model_copy(
                update={
                    "accepted": True,
                    "sequence": 1,
                    "request_id": result.requests[0].request_id,
                }
            )
        }
    )
    reconciled = reload_step(result.state, confirmation)
    assert reconciled.state.sessions.sessions[0].phase == core.SessionPhase.IDLE
    assert reconciled.state.sessions.sessions[0].accepted
    receipt = reconciled.state.sessions.inputs[0].receipt
    assert isinstance(receipt, core.InputDelivered)
    assert receipt.invocation == event.invocation
    assert receipt.observation.accepted
    assert reconciled.state.sessions.inputs[1] == dispatched.state.sessions.inputs[1]
    assert reconciled.events == ()
    assert reconciled.requests == ()
    state = reconciled.state
    for sequence in sequences:
        older = confirmation.model_copy(
            update={
                "observation": confirmation.observation.model_copy(
                    update={"accepted": False, "sequence": sequence}
                )
            }
        )
        result = reload_step(state, older)
        assert result.state.sessions.sessions[0].phase == core.SessionPhase.IDLE
        assert result.state.sessions.sessions[0].accepted
        assert result.state.sessions.invocations[0].observation is not None
        assert result.state.sessions.invocations[0].observation.accepted
        assert result.events == ()
        assert result.requests == ()
        state = result.state


def test_exact_inspection_acceptance_reaches_input_delivery_authority() -> None:
    dispatched, event = unacknowledged_success()
    result = reload_step(dispatched.state, event)
    confirmation = event.model_copy(
        update={
            "observation": event.observation.model_copy(
                update={"accepted": True, "request_id": result.requests[0].request_id}
            )
        }
    )
    reconciled = reload_step(result.state, confirmation)
    record = reconciled.state.sessions.inputs[0]
    assert isinstance(record.receipt, core.InputDelivered)
    assert record.receipt.input_id == record.input.input_id
    assert record.receipt.invocation == event.invocation
    assert record.receipt.observation.request_id == event.observation.request_id
    assert reconciled.state.sessions.sessions[0].phase == core.SessionPhase.IDLE
    assert reconciled.events == ()
    replay = reload_step(reconciled.state, confirmation)
    assert replay.state.sessions.inputs == reconciled.state.sessions.inputs
    assert replay.events == ()
