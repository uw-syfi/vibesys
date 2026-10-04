"""Session-turn lifecycle properties through the published pure kernel API."""

import hashlib
import json
from typing import ClassVar, Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import BaseModel

import vs_core.api as core

from .proof_digest import value_digest


def assert_input_boundary(state: core.CoreState, event: core.CoreEvent, kind: str) -> None:
    """Pin the exact missing sibling, so another leaf's failure cannot satisfy a trace."""
    with pytest.raises(core.KernelNotImplementedError) as boundary:
        core.step(state, event)
    assert boundary.value.subarea == "_session_inputs"
    assert boundary.value.event_kind == kind


def turn(identity: str = "planner", charge: str = "free", max_turns: int = 1) -> core.TurnSpec:
    """Supplied identities keep provider bounds separate from logical currency."""
    state = core.initial_state()
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
            "workspace": core.Scope(owner=state.run.run_id, generation=0),
            "prompts": (),
            "output_schema": core.SchemaRef(name="plan", version=1),
            "deadline_at": 100.0,
            "charge_class": charge,
            "max_turns": max_turns,
        }
    )


def scope() -> core.Scope:
    return core.Scope(owner=core.initial_state().run.run_id, generation=0)


def invocation(spec: core.TurnSpec, generation: int = 0) -> core.InvocationRef:
    return core.InvocationRef(
        session_id=spec.session.session_id,
        invocation_id=spec.invocation_id,
        generation=generation,
    )


def reload_state(
    state: core.CoreState, codec: core.OperationRegistry | None = None
) -> core.CoreState:
    """Use the versioned public envelope codec, including its frozen identities."""
    envelope = core.RunEnvelope[core.StrategyState](
        schema_version=core.ENVELOPE_SCHEMA_VERSION,
        fence=core.HostFence(host_id=core.HostId(root="host"), epoch=1),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=core.StrategyState(schema_version=1),
        event_cursor=core.EventCursor(sequence=0),
    )
    codec = codec if codec is not None else core.OperationRegistry()
    return codec.decode_envelope(
        core.RunEnvelope[core.StrategyState], codec.encode_envelope(envelope)
    ).core


def reload_step(
    state: core.CoreState, event: core.CoreEvent, *, codec: core.OperationRegistry | None = None
) -> core.Transition:
    """Each boundary must survive canonical public-model serialization."""
    before = state.model_dump_json()
    result = core.step(state, event)
    assert result == core.step(reload_state(state, codec), event)
    assert state.model_dump_json() == before
    assert reload_state(result.state, codec) == result.state
    assert core.project(result.state) == core.project(reload_state(result.state, codec))
    return result


@given(st.integers(min_value=1, max_value=100))
def test_run_turn_admission_records_one_charge_and_durable_ensure(max_turns: int) -> None:
    state = core.initial_state()
    spec = turn(max_turns=max_turns)
    result = reload_step(state, core.TurnRequested(scope=scope(), turn=spec))
    assert len(result.state.sessions.invocations) == 1
    assert len(result.state.sessions.run_charges) == 1
    receipt = result.state.sessions.run_charges[0]
    assert receipt.kind == core.ChargeKind.TURN
    assert receipt.charged == 1
    assert receipt.refunded == 0
    assert receipt.invocation_id == spec.invocation_id
    assert len(result.requests) == 1
    assert isinstance(result.requests[0], core.EnsureSession)
    assert not any(isinstance(request, core.DispatchTurn) for request in result.requests)
    assert core.pending_requests(result.state.intents) == result.requests
    repeated = reload_step(result.state, core.TurnRequested(scope=scope(), turn=spec))
    assert repeated.requests == ()
    assert repeated.events == ()
    assert repeated.state.sessions == result.state.sessions


@given(st.lists(st.integers(min_value=1, max_value=20), max_size=25))
def test_unknown_and_stale_invocation_signals_never_create_execution(
    generations: list[int],
) -> None:
    spec = turn()
    known = waiting_turn_state(spec)
    assert known.sessions.invocations[0].invocation == invocation(spec)
    for seed_state in (core.initial_state(), known):
        state = seed_state
        for generation in generations:
            event = core.TurnInputsReserved(invocation=invocation(spec, generation), input_ids=())
            result = reload_step(state, event)
            assert result.requests == ()
            assert result.events == ()
            assert result.state.sessions == state.sessions
            state = result.state


@pytest.mark.parametrize("terminal", [False, True])
def test_orphan_session_observation_cannot_acquire_or_release_a_lease(*, terminal: bool) -> None:
    state = core.initial_state()
    observation = core.Observation(
        event_id=core.EventId(root="orphan"),
        request_id=core.RequestId(root="unrecorded"),
        scope=scope(),
        sequence=1,
        observed_at=1.0,
        status=core.ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=terminal,
        released=terminal,
        resource_id=core.ResourceId(root="lease"),
    )
    result = reload_step(
        state,
        core.SessionObserved(session_id=core.SessionId(root="session"), observation=observation),
    )
    assert result.state.sessions == state.sessions
    assert result.requests == ()
    assert result.events == ()


def waiting_turn_state(spec: core.TurnSpec, *, charged: bool = True) -> core.CoreState:
    """Seed immutable sibling proof as permitted by the independent slice contract."""
    state = core.initial_state()
    ref = invocation(spec)
    charge = core.ChargeReceipt(
        charge_id=core.ChargeId(root=f"fixture-turn:{spec.invocation_id.root}"),
        kind=core.ChargeKind.TURN,
        invocation_id=spec.invocation_id,
        charged=1,
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
                        invocation=ref,
                        scope=scope(),
                        turn=spec,
                        phase=core.SessionPhase.ACQUIRING,
                    ),
                ),
                run_charges=(charge,) if charged else (),
            )
        }
    )


def test_empty_manifest_dispatch_requires_charge_and_positive_session_correspondence() -> None:
    spec = turn()
    event = core.TurnInputsReserved(invocation=invocation(spec), input_ids=())
    with pytest.raises(core.ContractValidationError, match="charge"):
        core.step(waiting_turn_state(spec, charged=False), event)
    state = waiting_turn_state(spec)
    result = reload_step(state, event)
    assert len(result.requests) == 1
    request = result.requests[0]
    assert isinstance(request, core.DispatchTurn)
    assert request.turn == spec
    assert request.inputs == ()
    assert request.request_id is not None
    assert request in core.pending_requests(result.state.intents)
    repeated = reload_step(result.state, event)
    assert repeated.requests == ()
    assert repeated.state.sessions.run_charges == state.sessions.run_charges


@given(st.permutations((0, 1, 2)), st.lists(st.integers(min_value=0, max_value=2), max_size=10))
def test_dispatch_reordered_duplicate_and_stale_manifests_have_one_durable_intent(
    order: tuple[int, ...], duplicates: list[int]
) -> None:
    spec = turn()
    state = waiting_turn_state(spec)
    events = (
        core.TurnInputsReserved(invocation=invocation(spec), input_ids=()),
        core.TurnInputsReserved(invocation=invocation(spec, 1), input_ids=()),
        core.TurnInputsReserved(invocation=invocation(turn(identity="orphan")), input_ids=()),
    )
    dispatches = []
    for index in (*order, *duplicates):
        result = reload_step(state, events[index])
        dispatches.extend(result.requests)
        assert len(result.state.sessions.invocations) == 1
        assert sum(receipt.charged for receipt in result.state.sessions.run_charges) == 1
        assert all(receipt.refunded == 0 for receipt in result.state.sessions.run_charges)
        state = result.state
    assert len(dispatches) == 1
    assert isinstance(dispatches[0], core.DispatchTurn)
    assert len(state.intents.intents) == 1


@pytest.mark.parametrize("charge", ["correction", "resume"])
def test_correction_and_resume_without_predecessor_proof_cannot_admit(charge: str) -> None:
    state = core.initial_state()
    before = state.model_dump_json()
    with pytest.raises(core.ContractValidationError):
        core.step(state, core.TurnRequested(scope=scope(), turn=turn(charge=charge)))
    assert state.model_dump_json() == before


def test_leaf_preserves_input_and_interrupt_authorities() -> None:
    spec = turn()
    prior = invocation(turn(identity="previous")).model_copy(
        update={"session_id": core.SessionId(root="other-session")}
    )
    occurrence = core.InputRecord(
        input=core.SessionInput(
            input_id=core.InputId(root="note"),
            target=core.ScopeInputTarget(scope=scope()),
            artifact=core.ArtifactRef(
                artifact_id=core.ArtifactId(root="note-artifact"), digest="note-digest"
            ),
            received_at=0.0,
            sequence=0,
        )
    )
    claim = core.InterruptClaim(
        invocation=prior, authority=core.RequestId(root="interrupt"), refund=1
    )
    state = core.initial_state().model_copy(
        update={"sessions": core.SessionsState(inputs=(occurrence,), interrupts=(claim,))}
    )
    result = reload_step(state, core.TurnRequested(scope=scope(), turn=spec))
    assert result.state.sessions.inputs == state.sessions.inputs
    assert result.state.sessions.interrupts == state.sessions.interrupts


@pytest.mark.parametrize("charge", ["paid", "correction", "resume", "free"])
def test_all_charge_classes_dispatch_preserves_recorded_currency(charge: str) -> None:
    if charge in ("paid", "resume"):
        state, spec = attempt_turn_dispatch_state(charge)
    else:
        spec = turn(identity=f"{charge}-turn", charge=charge, max_turns=50)
        state = waiting_turn_state(spec)
        if charge == "correction":
            previous_state, predecessor = malformed_planner_state()
            spec = spec.model_copy(update={"predecessor": predecessor})
            state = waiting_turn_state(spec)
            state = state.model_copy(
                update={
                    "sessions": state.sessions.model_copy(
                        update={
                            "invocations": (
                                *previous_state.sessions.invocations,
                                *state.sessions.invocations,
                            ),
                            "run_charges": (
                                *previous_state.sessions.run_charges,
                                *state.sessions.run_charges,
                            ),
                        }
                    )
                }
            )
        state = state.model_copy(
            update={
                "run": state.run.model_copy(
                    update={"limits": core.Limits(max_turns=10, max_retries=2)}
                )
            }
        )
    result = reload_step(state, core.TurnInputsReserved(invocation=invocation(spec), input_ids=()))
    assert len(result.requests) == 1
    if charge == "resume":
        assert isinstance(result.requests[0], core.ResumeSessionTurn)
    else:
        assert isinstance(result.requests[0], core.DispatchTurn)
    assert result.state.sessions.run_charges == state.sessions.run_charges
    assert result.state.attempts == state.attempts
    receipts = (
        state.attempts.attempts[0].charges
        if charge in ("paid", "resume")
        else state.sessions.run_charges
    )
    current = tuple(receipt for receipt in receipts if receipt.invocation_id == spec.invocation_id)
    assert sum(receipt.charged for receipt in current if receipt.kind == core.ChargeKind.TURN) == 1
    assert sum(
        receipt.charged for receipt in current if receipt.kind == core.ChargeKind.ATTEMPT
    ) == (1 if charge == "paid" else 0)


@given(st.permutations(("a", "b", "c")))
def test_dispatch_manifest_uses_reservation_occurrence_order_without_mutating_inputs(
    insertion_order: tuple[str, ...],
) -> None:
    spec = turn()
    state = waiting_turn_state(spec)
    ref = invocation(spec)
    records = tuple(
        core.InputRecord(
            input=core.SessionInput(
                input_id=core.InputId(root=identity),
                target=core.ScopeInputTarget(scope=scope()),
                artifact=core.ArtifactRef(
                    artifact_id=core.ArtifactId(root="same-content"), digest="same-digest"
                ),
                received_at=0.0,
                sequence=0 if identity != "a" else 1,
            ),
            reserved_to=ref,
        )
        for identity in insertion_order
    )
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"inputs": records})}
    )
    ids = tuple(core.InputId(root=identity) for identity in ("b", "c", "a"))
    result = reload_step(state, core.TurnInputsReserved(invocation=ref, input_ids=ids))
    request = result.requests[0]
    assert isinstance(request, core.DispatchTurn)
    assert tuple(item.input_id for item in request.inputs) == ids
    assert result.state.sessions.inputs == records
    assert result.state.sessions.invocations[0].input_ids == ids
    assert len(result.state.sessions.invocations[0].reserved_inputs) == 3
    with pytest.raises(core.ContractValidationError, match="manifest"):
        core.step(state, core.TurnInputsReserved(invocation=ref, input_ids=tuple(reversed(ids))))


def turn_observation(
    request: core.Request,
    *,
    sequence: int = 1,
    accepted: bool = False,
    terminal: bool = False,
    status: core.ObservationStatus = core.ObservationStatus.UNKNOWN,
) -> core.Observation:
    assert request.request_id is not None
    return core.Observation(
        event_id=core.EventId(root=f"observation-{sequence}"),
        request_id=request.request_id,
        scope=request.scope,
        sequence=sequence,
        observed_at=float(sequence),
        status=status,
        resource_id=core.ResourceId(root="conversation"),
        accepted=accepted,
        terminal=terminal,
    )


def test_unknown_acceptance_inspects_exact_turn_and_keeps_currency_and_payload() -> None:
    spec = turn()
    dispatched = reload_step(
        waiting_turn_state(spec), core.TurnInputsReserved(invocation=invocation(spec), input_ids=())
    )
    event = core.TurnObserved(
        invocation=invocation(spec), observation=turn_observation(dispatched.requests[0])
    )
    result = reload_step(dispatched.state, event)
    assert len(result.requests) == 1
    inspection = result.requests[0]
    assert isinstance(inspection, core.InspectTurn)
    assert inspection.invocation == invocation(spec)
    assert result.state.sessions.run_charges == dispatched.state.sessions.run_charges
    assert result.state.sessions.invocations[0].turn == spec
    repeated = reload_step(result.state, event)
    assert repeated.requests == ()
    assert repeated.events == ()
    assert len(repeated.state.sessions.invocations) == 1


def with_intent(state: core.CoreState, request: core.Request) -> core.CoreState:
    """Supply durable sibling-owned correspondence without executing that sibling."""
    assert request.request_id is not None
    record = core.Intent(
        request_id=request.request_id,
        request=request,
        payload_digest=hashlib.sha256(
            json.dumps(
                request.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest(),
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        phase=core.IntentPhase.DISPATCHED,
        reconcile_deadline_at=request.deadline_at,
    )
    return state.model_copy(
        update={"intents": state.intents.model_copy(update={"intents": (record,)})}
    )


def closing_session_state() -> tuple[core.CoreState, core.CloseSession]:
    spec = turn()
    request = core.CloseSession(
        request_id=core.RequestId(root="close-session"),
        scope=scope(),
        session_id=spec.session.session_id,
        deadline_at=100.0,
    )
    state = waiting_turn_state(spec)
    session = state.sessions.sessions[0].model_copy(
        update={"phase": core.SessionPhase.CLOSING, "pending_intents": (request.request_id,)}
    )
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"sessions": (session,)})}
    )
    return with_intent(state, request), request


@given(terminal=st.booleans(), released=st.booleans(), children_complete=st.booleans())
def test_session_release_requires_terminal_release_and_complete_child_manifest(
    *, terminal: bool, released: bool, children_complete: bool
) -> None:
    state, request = closing_session_state()
    observation = turn_observation(
        request, terminal=terminal, accepted=True, status=core.ObservationStatus.SUCCEEDED
    ).model_copy(update={"released": released, "children_complete": children_complete})
    event = core.SessionObserved(session_id=request.session_id, observation=observation)
    result = reload_step(state, event)
    proven = terminal and released and children_complete
    assert (result.state.sessions.sessions[0].phase == core.SessionPhase.TERMINAL) == proven
    assert result.state.sessions.run_charges == state.sessions.run_charges
    if proven:
        assert result.requests == ()
        duplicate = reload_step(result.state, event)
        assert duplicate.requests == ()
        assert duplicate.events == ()
    else:
        assert all(isinstance(item, core.InspectRequest) for item in result.requests)


def malformed_planner_state() -> tuple[core.CoreState, core.InvocationRef]:
    """Model the accepted malformed reply while retaining its conversation."""
    spec = turn()
    state = waiting_turn_state(spec)
    ref = invocation(spec)
    request = core.DispatchTurn(
        request_id=core.RequestId(root="original-dispatch"),
        scope=scope(),
        deadline_at=100.0,
        turn=spec,
    )
    observation = turn_observation(
        request, accepted=True, terminal=True, status=core.ObservationStatus.FAILED
    )
    original = state.sessions.invocations[0].model_copy(
        update={"phase": core.SessionPhase.TERMINAL, "observation": observation}
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={"limits": core.Limits(max_turns=10, max_retries=2)}
            ),
            "sessions": state.sessions.model_copy(update={"invocations": (original,)}),
        }
    )
    return state, ref


@pytest.mark.parametrize("max_retries", [1, 2])
def test_f1_planner_acquisition_and_correction_reach_declared_input_boundary(
    max_retries: int,
) -> None:
    spec = turn()
    prepared = reload_step(core.initial_state(), core.TurnRequested(scope=scope(), turn=spec))
    ensure = prepared.requests[0]
    event = core.SessionObserved(
        session_id=spec.session.session_id,
        observation=turn_observation(
            ensure, accepted=True, terminal=True, status=core.ObservationStatus.SUCCEEDED
        ),
    )
    # The independent Sessions B leaf remains frozen; prove A reaches its exact signal.
    assert_input_boundary(reload_state(prepared.state), event, "input_reservation_requested")
    state, predecessor = malformed_planner_state()
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={"limits": state.run.limits.model_copy(update={"max_retries": max_retries})}
            )
        }
    )
    correction = turn(identity="correction", charge="correction").model_copy(
        update={"predecessor": predecessor}
    )
    assert_input_boundary(
        reload_state(state),
        core.TurnRequested(scope=scope(), turn=correction),
        "input_reservation_requested",
    )
    # Legacy agentshim_driver assertions: correcting the first malformed reply keeps s-1.
    assert state.sessions.sessions[0].resource_id == core.ResourceId(root="conversation")
    assert correction.session == state.sessions.sessions[0].spec


@pytest.mark.parametrize("resource", [None, core.ResourceId(root="another-conversation")])
def test_close_receipt_requires_exact_known_physical_resource(
    resource: core.ResourceId | None,
) -> None:
    state, request = closing_session_state()
    observation = turn_observation(
        request, terminal=True, accepted=True, status=core.ObservationStatus.SUCCEEDED
    ).model_copy(update={"resource_id": resource, "released": True, "children_complete": True})
    result = reload_step(
        state, core.SessionObserved(session_id=request.session_id, observation=observation)
    )
    assert result.state.sessions.sessions[0].phase == core.SessionPhase.CLOSING


def test_cancellation_command_acknowledgement_does_not_prove_target_terminal() -> None:
    spec = turn()
    state = waiting_turn_state(spec)
    row = state.sessions.invocations[0].model_copy(update={"phase": core.SessionPhase.EXECUTING})
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"invocations": (row,)})}
    )
    request = core.CancelTurn(
        request_id=core.RequestId(root="cancel-command"),
        scope=scope(),
        deadline_at=100.0,
        invocation=invocation(spec),
    )
    state = with_intent(state, request)
    result = reload_step(
        state,
        core.TurnObserved(
            invocation=invocation(spec),
            observation=turn_observation(
                request, terminal=True, status=core.ObservationStatus.SUCCEEDED
            ),
        ),
    )
    assert result.state.sessions.invocations[0].phase == core.SessionPhase.EXECUTING
    assert result.state.sessions.invocations[0].observation is None
    assert result.requests == ()
    assert result.events == ()


@given(st.integers(min_value=2, max_value=100))
def test_close_sequence_is_independent_of_earlier_acquisition_sequence(prior_sequence: int) -> None:
    state, request = closing_session_state()
    session = state.sessions.sessions[0].model_copy(update={"acceptance_sequence": prior_sequence})
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"sessions": (session,)})}
    )
    observation = turn_observation(
        request, sequence=1, terminal=True, accepted=True, status=core.ObservationStatus.SUCCEEDED
    ).model_copy(update={"released": True, "children_complete": True})
    result = reload_step(
        state, core.SessionObserved(session_id=request.session_id, observation=observation)
    )
    assert result.state.sessions.sessions[0].phase == core.SessionPhase.TERMINAL
    assert result.requests == ()


def attempt_acquisition_state(
    admission: str = "episode-1",
) -> tuple[core.CoreState, core.AttemptRef, core.Scope, core.DecisionId]:
    state = core.initial_state()
    attempt_id = core.AttemptId(root="attempt")
    ref = core.AttemptRef(attempt_id=attempt_id, generation=0)
    owner_scope = core.Scope(owner=attempt_id, generation=0)
    admission_id = core.DecisionId(root=admission)
    owner = core.AttemptView(
        attempt_id=attempt_id,
        item_id=core.ItemId(root="item"),
        generation=0,
        phase=core.AttemptPhase.ACQUIRING,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
        admission_id=admission_id,
    )
    return (
        state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))}),
        ref,
        owner_scope,
        admission_id,
    )


def test_initial_group_prepares_all_sessions_once_and_waits_for_every_member() -> None:
    state, ref, owner_scope, admission = attempt_acquisition_state()
    first = turn().session
    second = first.model_copy(update={"session_id": core.SessionId(root="reviewer")})
    event = core.SessionsAcquireRequested(
        attempt=ref, admission_id=admission, scope=owner_scope, specs=(first, second)
    )
    prepared = reload_step(state, event)
    assert len(prepared.requests) == 2
    assert all(isinstance(item, core.EnsureSession) for item in prepared.requests)
    assert prepared.state.sessions.acquisition_groups[0].phase == "acquiring"
    assert prepared.state.sessions.run_charges == ()
    assert prepared.state.attempts == state.attempts
    repeated = reload_step(prepared.state, event)
    assert repeated.requests == ()
    assert repeated.state.sessions == prepared.state.sessions
    observation = turn_observation(
        prepared.requests[0], terminal=True, accepted=True, status=core.ObservationStatus.SUCCEEDED
    ).model_copy(update={"admission_id": admission})
    acquired = reload_step(
        prepared.state, core.SessionObserved(session_id=first.session_id, observation=observation)
    )
    assert acquired.state.sessions.sessions[0].phase == core.SessionPhase.IDLE
    assert acquired.state.sessions.sessions[1].phase == core.SessionPhase.ACQUIRING
    assert acquired.state.sessions.acquisition_groups[0].phase == "acquiring"
    assert acquired.requests == ()
    assert acquired.events == ()
    last = turn_observation(
        prepared.requests[1], terminal=True, accepted=True, status=core.ObservationStatus.SUCCEEDED
    ).model_copy(
        update={"admission_id": admission, "resource_id": core.ResourceId(root="reviewer-lease")}
    )
    ready = core.step(
        acquired.state, core.SessionObserved(session_id=second.session_id, observation=last)
    )
    assert ready.requests == ()
    assert ready.events == ()
    assert ready.state.sessions.acquisition_groups[0].phase == "ready"
    assert ready.state.attempts.attempts[0].phase == core.AttemptPhase.ACQUIRING


def test_reacquisition_has_episode_identity_and_requires_exact_retained_conversation() -> None:
    state, ref, owner_scope, admission = attempt_acquisition_state()
    spec = turn().session.model_copy(update={"policy": "reuse", "lifetime": "owner"})
    event = core.SessionsAcquireRequested(
        attempt=ref, admission_id=admission, scope=owner_scope, specs=(spec,)
    )
    original = reload_step(state, event)
    resource = core.ResourceId(root="retained-conversation")
    parked = original.state.sessions.sessions[0].model_copy(
        update={"phase": core.SessionPhase.CHECKPOINTED, "resource_id": resource}
    )
    newer_admission = core.DecisionId(root="episode-2")
    owner = state.attempts.attempts[0].model_copy(update={"admission_id": newer_admission})
    state = original.state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "sessions": original.state.sessions.model_copy(update={"sessions": (parked,)}),
        }
    )
    newer = event.model_copy(update={"admission_id": newer_admission})
    reattached = reload_step(state, newer)
    request = reattached.requests[0]
    assert isinstance(request, core.EnsureSession)
    assert request.required_resource == resource
    assert request.request_id != original.requests[0].request_id
    stale = turn_observation(
        original.requests[0], terminal=True, accepted=True, status=core.ObservationStatus.SUCCEEDED
    ).model_copy(update={"admission_id": admission, "resource_id": resource})
    ignored = reload_step(
        reattached.state, core.SessionObserved(session_id=spec.session_id, observation=stale)
    )
    assert ignored.state.sessions == reattached.state.sessions
    assert ignored.requests == ()
    wrong = turn_observation(
        request, terminal=True, accepted=True, status=core.ObservationStatus.SUCCEEDED
    ).model_copy(update={"admission_id": newer_admission})
    with pytest.raises(core.ContractValidationError, match="conversation"):
        core.step(
            reattached.state, core.SessionObserved(session_id=spec.session_id, observation=wrong)
        )
    missing = state.model_copy(
        update={
            "sessions": state.sessions.model_copy(
                update={"sessions": (parked.model_copy(update={"resource_id": None}),)}
            )
        }
    )
    with pytest.raises(core.ContractValidationError, match="correspondence"):
        core.step(missing, newer)


@given(st.lists(st.integers(min_value=0, max_value=1), min_size=1, max_size=12))
def test_failed_initial_group_late_acceptance_only_adds_cleanup(indices: list[int]) -> None:
    state, ref, owner_scope, admission = attempt_acquisition_state()
    first = turn().session
    second = first.model_copy(update={"session_id": core.SessionId(root="reviewer")})
    prepared = reload_step(
        state,
        core.SessionsAcquireRequested(
            attempt=ref, admission_id=admission, scope=owner_scope, specs=(first, second)
        ),
    )
    failed = prepared.state.sessions.acquisition_groups[0].model_copy(
        update={"phase": "failed", "failure_request": core.RequestId(root="failed-member")}
    )
    state = prepared.state.model_copy(
        update={
            "sessions": prepared.state.sessions.model_copy(update={"acquisition_groups": (failed,)})
        }
    )
    closes: list[core.Request] = []
    for index in indices:
        request = prepared.requests[index]
        assert isinstance(request, core.EnsureSession)
        observation = turn_observation(
            request, terminal=True, accepted=True, status=core.ObservationStatus.SUCCEEDED
        ).model_copy(
            update={
                "admission_id": admission,
                "resource_id": core.ResourceId(root=f"lease-{index}"),
            }
        )
        result = reload_step(
            state, core.SessionObserved(session_id=request.spec.session_id, observation=observation)
        )
        assert result.state.sessions.acquisition_groups[0].phase == "failed"
        assert result.events == ()
        assert all(isinstance(item, core.CloseSession) for item in result.requests)
        closes.extend(result.requests)
        state = result.state
    assert len(closes) == len(set(indices))
    assert all(
        state.sessions.sessions[index].phase == core.SessionPhase.CLOSING for index in set(indices)
    )
    assert state.sessions.run_charges == ()


def test_initial_group_replay_cannot_change_session_role_or_access() -> None:
    state, ref, owner_scope, admission = attempt_acquisition_state()
    spec = turn().session
    event = core.SessionsAcquireRequested(
        attempt=ref, admission_id=admission, scope=owner_scope, specs=(spec,)
    )
    prepared = reload_step(state, event)
    changed = spec.model_copy(update={"access": core.Access.WRITE_CANDIDATE})
    with pytest.raises(core.ContractValidationError, match="conflict"):
        core.step(prepared.state, event.model_copy(update={"specs": (changed,)}))


@given(st.integers(min_value=0, max_value=20))
def test_global_turn_budget_counts_only_turn_currency(other_currency: int) -> None:
    state, _, _, _ = attempt_acquisition_state()
    owner = state.attempts.attempts[0].model_copy(
        update={
            "charges": (
                core.ChargeReceipt(
                    charge_id=core.ChargeId(root="admission"),
                    kind=core.ChargeKind.ADMISSION,
                    charged=other_currency,
                ),
                core.ChargeReceipt(
                    charge_id=core.ChargeId(root="attempt"),
                    kind=core.ChargeKind.ATTEMPT,
                    charged=other_currency,
                ),
            )
        }
    )
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    result = reload_step(state, core.TurnRequested(scope=scope(), turn=turn()))
    assert len(result.state.sessions.run_charges) == 1
    assert result.state.attempts == state.attempts
    spent = owner.model_copy(
        update={
            "charges": (
                *owner.charges,
                core.ChargeReceipt(
                    charge_id=core.ChargeId(root="turn"), kind=core.ChargeKind.TURN, charged=1
                ),
            )
        }
    )
    exhausted = state.model_copy(update={"attempts": core.AttemptsState(attempts=(spent,))})
    with pytest.raises(core.ContractValidationError, match="budget"):
        core.step(exhausted, core.TurnRequested(scope=scope(), turn=turn()))


@pytest.mark.parametrize("charge", ["free", "correction"])
def test_decision_budget_and_retry_exhaustion_returns_typed_feedback(charge: str) -> None:
    if charge == "correction":
        state, predecessor = malformed_planner_state()
        spec = turn(identity="correction", charge=charge).model_copy(
            update={"predecessor": predecessor}
        )
        state = state.model_copy(
            update={
                "run": state.run.model_copy(
                    update={"limits": core.Limits(max_turns=10, max_retries=0)}
                )
            }
        )
    else:
        state = core.initial_state().model_copy(
            update={
                "run": core.initial_state().run.model_copy(
                    update={"limits": core.Limits(max_turns=0)}
                )
            }
        )
        spec = turn()
    decision = core.RequestTurn(
        decision_id=core.DecisionId(root="out-of-budget"), scope=scope(), turn=spec
    )
    result = reload_step(
        state, core.DecisionSubmitted(decision=decision, expected_revision=state.revision)
    )
    assert len(result.events) == 1
    rejection = result.events[0]
    assert isinstance(rejection, core.Rejected)
    assert rejection.decision_id == decision.decision_id
    assert rejection.code == core.RejectionCode.BUDGET
    assert result.requests == ()
    assert result.state.sessions == state.sessions


@pytest.mark.parametrize("status", list(core.ObservationStatus))
def test_conclusive_close_facts_release_independent_of_command_outcome(
    status: core.ObservationStatus,
) -> None:
    state, request = closing_session_state()
    observation = turn_observation(request, terminal=True, status=status).model_copy(
        update={"released": True, "children_complete": True}
    )
    result = reload_step(
        state, core.SessionObserved(session_id=request.session_id, observation=observation)
    )
    if status in (core.ObservationStatus.UNKNOWN, core.ObservationStatus.PENDING):
        assert result.state.sessions.sessions[0].phase == core.SessionPhase.CLOSING
        assert len(result.requests) == 1
        assert isinstance(result.requests[0], core.InspectRequest)
        assert result.requests[0].resource_id is None
    else:
        assert result.state.sessions.sessions[0].phase == core.SessionPhase.TERMINAL
        assert result.requests == ()


def test_repeated_unknown_with_new_supplied_time_reuses_exact_inspection_payload() -> None:
    spec = turn()
    dispatched = reload_step(
        waiting_turn_state(spec), core.TurnInputsReserved(invocation=invocation(spec), input_ids=())
    )
    state = dispatched.state.model_copy(
        update={"run": dispatched.state.run.model_copy(update={"now_at": 1.0})}
    )
    first = reload_step(
        state,
        core.TurnObserved(
            invocation=invocation(spec), observation=turn_observation(dispatched.requests[0])
        ),
    )
    state = first.state.model_copy(
        update={"run": first.state.run.model_copy(update={"now_at": 2.0})}
    )
    second = reload_step(
        state,
        core.TurnObserved(
            invocation=invocation(spec),
            observation=turn_observation(dispatched.requests[0], sequence=2),
        ),
    )
    assert len(first.requests) == 1
    assert second.requests == ()
    assert core.pending_requests(second.state.intents) == core.pending_requests(first.state.intents)
    assert second.state.sessions.run_charges == first.state.sessions.run_charges
    assert len(second.state.sessions.invocations) == 1


def interrupted_attempt_state(
    phase: str,
) -> tuple[core.CoreState, core.InvocationRef, core.TurnSpec, core.Scope]:
    state, _, owner_scope, admission = attempt_acquisition_state()
    spec = turn().model_copy(update={"workspace": owner_scope})
    previous_ref = invocation(spec)
    source = core.RequestId(root="old-dispatch")
    authority = core.RequestId(root="interrupt-authority")
    checkpoint_authority = core.RequestId(root="wip-checkpoint")
    paid_charge = core.ChargeId(root="paid-charge")
    paid = core.ChargeReceipt(
        charge_id=paid_charge,
        kind=core.ChargeKind.ATTEMPT,
        invocation_id=spec.invocation_id,
        source_request=source,
        charged=1,
        refunded=1 if phase == "completed" else 0,
        refund_sources=(authority,) if phase == "completed" else (),
    )
    logical = core.ChargeReceipt(
        charge_id=core.ChargeId(root="old-turn"),
        kind=core.ChargeKind.TURN,
        invocation_id=spec.invocation_id,
        source_request=source,
        charged=1,
    )
    request = core.DispatchTurn(
        request_id=source, scope=owner_scope, admission_id=admission, deadline_at=100.0, turn=spec
    )
    terminal = turn_observation(
        request, terminal=True, accepted=True, status=core.ObservationStatus.CANCELLED
    ).model_copy(update={"admission_id": admission})
    predecessor = core.Invocation(
        invocation=previous_ref,
        scope=owner_scope,
        turn=spec,
        phase=core.SessionPhase.TERMINAL,
        observation=terminal,
    )
    checkpoint = core.AttemptCheckpoint(
        invocation=previous_ref,
        request_id=checkpoint_authority,
        revision=state.run.facts.baseline,
        retention="wip",
    )
    owner = state.attempts.attempts[0].model_copy(
        update={
            "phase": core.AttemptPhase.ACTIVE,
            "charges": (paid, logical),
            "checkpoints": (checkpoint,) if phase in ("checkpointed", "completed") else (),
        }
    )
    claim = core.InterruptClaim.model_validate(
        {
            "invocation": previous_ref,
            "authority": authority,
            "refund": 1,
            "phase": phase,
            "checkpoint_authority": checkpoint_authority
            if phase in ("checkpointed", "completed")
            else None,
            "refunded_charge": paid_charge if phase == "completed" else None,
        }
    )
    session = core.SessionView(
        spec=spec.session,
        scope=owner_scope,
        generation=0,
        phase=core.SessionPhase.IDLE,
        invocation=spec.invocation_id,
        resource_id=core.ResourceId(root="conversation"),
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={"limits": core.Limits(max_turns=10, max_refunds=1)}
            ),
            "attempts": core.AttemptsState(attempts=(owner,)),
            "sessions": core.SessionsState(
                sessions=(session,), invocations=(predecessor,), interrupts=(claim,)
            ),
        }
    )
    successor = spec.model_copy(
        update={
            "invocation_id": core.InvocationId(root="replacement"),
            "charge_class": "paid",
            "predecessor": previous_ref,
        }
    )
    return state, previous_ref, successor, owner_scope


@pytest.mark.parametrize("phase", ["pending", "draining", "checkpointed", "blocked", "completed"])
def test_interrupted_replacement_requires_terminal_checkpoint_and_completed_refund(
    phase: str,
) -> None:
    state, previous, successor, owner_scope = interrupted_attempt_state(phase)
    event = core.TurnRequested(scope=owner_scope, turn=successor)
    if phase == "completed":
        reached = core.step(reload_state(state), event)
        assert reached.requests == ()
        assert reached.events == ()
        # The interrupted predecessor's paid charge already covers the replacement.
        assert reached.state.attempts.attempts[0].charges == state.attempts.attempts[0].charges
        assert reached.state.sessions.invocations[-1].phase == core.SessionPhase.ACQUIRING
    else:
        with pytest.raises(core.ContractValidationError, match="completed"):
            core.step(reload_state(state), event)
    assert successor.invocation_id != previous.invocation_id
    assert state.sessions.interrupts[0].phase == phase
    paid, logical = state.attempts.attempts[0].charges
    assert logical.kind == core.ChargeKind.TURN
    assert logical.refunded == 0
    assert paid.refunded <= paid.charged


def test_interrupted_replacement_cannot_reuse_predecessor_identity() -> None:
    state, previous, successor, owner_scope = interrupted_attempt_state("completed")
    reused = successor.model_copy(update={"invocation_id": previous.invocation_id})
    with pytest.raises(core.ContractValidationError, match="conflict"):
        core.step(state, core.TurnRequested(scope=owner_scope, turn=reused))


@pytest.mark.parametrize("charge", ["correction", "paid"])
@pytest.mark.parametrize("status", [core.ObservationStatus.UNKNOWN, core.ObservationStatus.PENDING])
def test_inconclusive_terminal_flag_never_authorizes_a_successor(
    charge: str, status: core.ObservationStatus
) -> None:
    if charge == "paid":
        state, _, successor, owner_scope = interrupted_attempt_state("completed")
    else:
        state, predecessor = malformed_planner_state()
        owner_scope = scope()
        successor = turn(identity="correction", charge=charge).model_copy(
            update={"predecessor": predecessor}
        )
    previous = state.sessions.invocations[0]
    assert previous.observation is not None
    ambiguous = previous.model_copy(
        update={
            "phase": core.SessionPhase.UNKNOWN,
            "observation": previous.observation.model_copy(update={"status": status}),
        }
    )
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"invocations": (ambiguous,)})}
    )
    with pytest.raises(core.ContractValidationError):
        core.step(reload_state(state), core.TurnRequested(scope=owner_scope, turn=successor))


def test_missing_session_resource_cannot_dispatch_on_policy_alone() -> None:
    spec = turn().model_copy(
        update={"session": turn().session.model_copy(update={"policy": "reuse"})}
    )
    state = waiting_turn_state(spec)
    session = state.sessions.sessions[0].model_copy(update={"resource_id": None})
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"sessions": (session,)})}
    )
    result = reload_step(state, core.TurnInputsReserved(invocation=invocation(spec), input_ids=()))
    assert result.requests == ()
    assert result.state.sessions == state.sessions


@given(
    st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=5),
            st.sampled_from(list(core.ObservationStatus)),
            st.integers(min_value=0, max_value=1),
        ),
        max_size=20,
    )
)
def test_terminal_history_ignores_duplicate_reordered_and_stale_results(
    observations: list[tuple[int, core.ObservationStatus, int]],
) -> None:
    state, ref = malformed_planner_state()
    turn_spec = state.sessions.invocations[0].turn
    request = core.DispatchTurn(
        request_id=core.RequestId(root="original-dispatch"),
        scope=scope(),
        deadline_at=100.0,
        turn=turn_spec,
    )
    state = with_intent(state, request)
    expected = state.sessions
    for sequence, status, generation in observations:
        event = core.TurnObserved(
            invocation=ref.model_copy(update={"generation": generation}),
            observation=turn_observation(
                request, sequence=sequence, terminal=True, accepted=True, status=status
            ),
            output_schema=core.SchemaRef(name="late-unrelated-schema", version=2),
            output_json="{late reply}",
        )
        result = reload_step(state, event)
        row = result.state.sessions.invocations[0]
        original = expected.invocations[0]
        assert row.phase == core.SessionPhase.TERMINAL
        assert row.turn == original.turn
        assert row.output_schema == original.output_schema
        assert row.output_json == original.output_json
        assert row.observation is not None
        assert original.observation is not None
        assert row.observation.status == original.observation.status
        assert result.state.sessions.run_charges == expected.run_charges
        assert result.state.sessions.inputs == expected.inputs
        assert result.requests == ()
        assert result.events == ()
        assert len(result.state.sessions.invocations) == 1
        state = result.state


@pytest.mark.parametrize("phase", list(core.ContinuationPhase))
def test_resume_requires_exact_authorized_continuation(phase: core.ContinuationPhase) -> None:
    state, resumed = attempt_turn_dispatch_state("resume")
    continuation = state.evaluation.continuations[0].model_copy(update={"phase": phase})
    state = state.model_copy(
        update={
            "sessions": state.sessions.model_copy(
                update={"invocations": state.sessions.invocations[:1]}
            ),
            "evaluation": core.EvaluationState(continuations=(continuation,)),
        }
    )
    event = core.TurnRequested(scope=state.sessions.invocations[0].scope, turn=resumed)
    if phase == core.ContinuationPhase.AUTHORIZED:
        # The attempt leaf now authorizes the resume charge; the turn then
        # stops at the still-unimplemented session-input reservation.
        assert_input_boundary(reload_state(state), event, "input_reservation_requested")
    else:
        with pytest.raises(core.ContractValidationError, match="authorization"):
            core.step(reload_state(state), event)
    assert state.evaluation.continuations == (continuation,)


@pytest.mark.parametrize("status", [core.ObservationStatus.UNKNOWN, core.ObservationStatus.PENDING])
def test_inconclusive_terminal_observation_keeps_the_turn_reconcilable(
    status: core.ObservationStatus,
) -> None:
    spec = turn()
    dispatched = reload_step(
        waiting_turn_state(spec), core.TurnInputsReserved(invocation=invocation(spec), input_ids=())
    )
    first = reload_step(
        dispatched.state,
        core.TurnObserved(
            invocation=invocation(spec),
            observation=turn_observation(dispatched.requests[0], terminal=True, status=status),
        ),
    )
    assert first.events == ()
    assert first.state.sessions.invocations[0].phase != core.SessionPhase.TERMINAL
    assert first.state.sessions.run_charges == dispatched.state.sessions.run_charges
    confirmed = core.TurnObserved(
        invocation=invocation(spec),
        observation=turn_observation(
            dispatched.requests[0],
            sequence=2,
            terminal=True,
            accepted=True,
            status=core.ObservationStatus.SUCCEEDED,
        ),
    )
    assert_input_boundary(reload_state(first.state), confirmed, "input_acceptance_observed")


@pytest.mark.parametrize(
    "proof",
    [
        "valid",
        "absent",
        "subset",
        "wrong-invocation",
        "historical",
        "extra-currency",
        "wrong-amount",
    ],
)
def test_attempt_charge_authorization_requires_the_exact_live_currency_set(proof: str) -> None:
    state, _, owner_scope, _ = attempt_acquisition_state()
    spec = turn(charge="paid").model_copy(update={"workspace": owner_scope})
    ref = invocation(spec)
    turn_charge = core.ChargeReceipt(
        charge_id=core.ChargeId(root="turn-authority"),
        kind=core.ChargeKind.TURN,
        invocation_id=spec.invocation_id,
        charged=1,
    )
    paid_charge = core.ChargeReceipt(
        charge_id=core.ChargeId(root="paid-authority"),
        kind=core.ChargeKind.ATTEMPT,
        invocation_id=spec.invocation_id,
        charged=1,
    )
    ids = (turn_charge.charge_id, paid_charge.charge_id)
    charges = (turn_charge, paid_charge)
    if proof == "absent":
        ids = (core.ChargeId(root="missing-charge"),)
    elif proof == "subset":
        ids = (turn_charge.charge_id,)
    elif proof == "wrong-invocation":
        charges = (
            turn_charge,
            paid_charge.model_copy(update={"invocation_id": core.InvocationId(root="other")}),
        )
    elif proof == "historical":
        artifact = core.ArtifactRef(
            artifact_id=core.ArtifactId(root="historical-proof"), digest="historical"
        )
        charges = (turn_charge, paid_charge.model_copy(update={"historical_proof": artifact}))
    elif proof == "extra-currency":
        extra = core.ChargeReceipt(
            charge_id=core.ChargeId(root="admission-authority"),
            kind=core.ChargeKind.ADMISSION,
            invocation_id=spec.invocation_id,
            charged=1,
        )
        charges = (*charges, extra)
        ids = (*ids, extra.charge_id)
    elif proof == "wrong-amount":
        charges = (turn_charge, paid_charge.model_copy(update={"charged": 2}))
    owner = state.attempts.attempts[0].model_copy(
        update={"phase": core.AttemptPhase.ACTIVE, "charges": charges}
    )
    session = core.SessionView(
        spec=spec.session,
        scope=owner_scope,
        generation=0,
        phase=core.SessionPhase.IDLE,
        invocation=spec.invocation_id,
        resource_id=core.ResourceId(root="conversation"),
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "sessions": core.SessionsState(
                sessions=(session,),
                invocations=(
                    core.Invocation(
                        invocation=ref,
                        scope=owner_scope,
                        turn=spec,
                        phase=core.SessionPhase.ACQUIRING,
                    ),
                ),
            ),
        }
    )
    event = core.InvocationChargesAuthorized(invocation=ref, charge_ids=ids)
    if proof == "valid":
        assert_input_boundary(reload_state(state), event, "input_reservation_requested")
    else:
        with pytest.raises(core.ContractValidationError, match="charge"):
            core.step(reload_state(state), event)
    assert state.attempts.attempts[0].charges == charges
    assert all(receipt.source_request is None for receipt in charges)


def test_success_without_acceptance_never_releases_reserved_input_and_can_reconcile_acceptance() -> (
    None
):
    spec = turn()
    state = waiting_turn_state(spec)
    ref = invocation(spec)
    record = core.InputRecord(
        input=core.SessionInput(
            input_id=core.InputId(root="pending-note"),
            target=core.ScopeInputTarget(scope=scope()),
            artifact=core.ArtifactRef(
                artifact_id=core.ArtifactId(root="note-artifact"), digest="note-digest"
            ),
            received_at=0.0,
            sequence=0,
        ),
        reserved_to=ref,
    )
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"inputs": (record,)})}
    )
    dispatched = reload_step(
        state, core.TurnInputsReserved(invocation=ref, input_ids=(record.input.input_id,))
    )
    terminal = core.TurnObserved(
        invocation=ref,
        observation=turn_observation(
            dispatched.requests[0], terminal=True, status=core.ObservationStatus.SUCCEEDED
        ),
        output_schema=spec.output_schema,
        output_json='{"plan": "completed"}',
    )
    result = reload_step(dispatched.state, terminal)
    assert result.state.sessions.inputs == (record,)
    assert len(result.events) == 1
    assert isinstance(result.events[0], core.TurnResult)
    assert result.events[0].output_json == terminal.output_json
    repeated = reload_step(result.state, terminal)
    assert repeated.events == ()
    assert repeated.requests == ()
    accepted = terminal.model_copy(
        update={
            "observation": turn_observation(
                dispatched.requests[0],
                sequence=2,
                terminal=True,
                accepted=True,
                status=core.ObservationStatus.SUCCEEDED,
            )
        }
    )
    assert_input_boundary(reload_state(result.state), accepted, "input_acceptance_observed")


class SessionTurnOutcome(core.Value):
    completed: bool


class SessionTurnOperation(core.OperationRequest):
    kind: Literal["test.session-turn"] = "test.session-turn"
    lifecycle: Literal[core.LifecycleClass.SESSION_TURN] = core.LifecycleClass.SESSION_TURN
    outcome_model: ClassVar[type[BaseModel]] = SessionTurnOutcome
    turn: core.TurnSpec


def normalize_registered_turn(request: core.OperationRequest) -> core.TurnSpec:
    assert isinstance(request, SessionTurnOperation)
    return request.turn


def registered_turn_state(
    *,
    spec: core.TurnSpec | None = None,
    owner_scope: core.Scope | None = None,
    initial: core.CoreState | None = None,
) -> tuple[core.CoreState, core.ExecuteRegisteredOperation, core.TurnSpec, core.OperationRegistry]:
    spec = spec or turn()
    owner_scope = owner_scope or scope()
    descriptor = core.OperationDescriptor(
        kind="test.session-turn",
        lifecycle=core.LifecycleClass.SESSION_TURN,
        request_schema=core.SchemaRef(name="custom-turn", version=1),
        outcome_schema=core.SchemaRef(name="custom-turn-result", version=1),
        inspect=True,
        cancel=True,
        watch=True,
    )
    registration = core.OperationRegistration(
        descriptor=descriptor,
        request_model=SessionTurnOperation,
        outcome_model=SessionTurnOutcome,
        normalize_turn=normalize_registered_turn,
    )
    codec = core.OperationRegistry((registration,))
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="custom-decision"),
            scope=owner_scope,
            request=SessionTurnOperation(turn=spec),
            deadline_at=spec.deadline_at,
        )
    )
    assert isinstance(decision, core.Operation)
    request = core.ExecuteRegisteredOperation(
        request_id=core.RequestId(root="custom-operation-request"),
        scope=owner_scope,
        admission_id=next(
            (
                owner.admission_id
                for owner in initial.attempts.attempts
                if owner.attempt_id == owner_scope.owner
            ),
            None,
        )
        if initial is not None
        else None,
        decision_id=decision.decision_id,
        operation_id=core.OperationId(root="custom-operation"),
        operation=codec.encode(decision.request),
        retry_limit=0,
        deadline_at=spec.deadline_at,
    )
    assert request.request_id is not None
    state = with_intent(initial or core.initial_state(), request)
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest="accepted-custom-proposal",
        feedback=core.Accepted(decision_id=decision.decision_id, request_ids=(request.request_id,)),
        request_ids=(request.request_id,),
    )
    canonical = state.intents.intents[0].model_copy(
        update={"phase": core.IntentPhase.PREPARED, "lifecycle": core.LifecycleClass.SESSION_TURN}
    )
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={
                    "capabilities": core.Capabilities(operations=codec.descriptors),
                    "receipts": (receipt,),
                }
            ),
            "intents": state.intents.model_copy(update={"intents": (canonical,)}),
        }
    )
    return state, request, spec, codec


def test_registered_turn_keeps_canonical_operation_and_acquires_before_dispatch() -> None:
    state, request, spec, codec = registered_turn_state()
    event = core.RegisteredTurnRequested(request=request, turn=spec)
    result = reload_step(state, event, codec=codec)
    assert len(result.requests) == 1
    assert isinstance(result.requests[0], core.EnsureSession)
    assert result.state.sessions.invocations[0].registered_operation == request.operation_id
    assert result.state.intents.intents[0] == state.intents.intents[0]
    assert len(result.state.sessions.run_charges) == 1
    replay = reload_step(result.state, event, codec=codec)
    assert replay.requests == ()
    assert replay.events == ()
    assert replay.state.sessions == result.state.sessions


def test_registered_turn_manifest_uses_original_canonical_request() -> None:
    state, request, spec, codec = registered_turn_state()
    seeded = waiting_turn_state(spec)
    row = seeded.sessions.invocations[0].model_copy(
        update={"registered_operation": request.operation_id}
    )
    state = state.model_copy(
        update={"sessions": seeded.sessions.model_copy(update={"invocations": (row,)})}
    )
    result = reload_step(
        state, core.TurnInputsReserved(invocation=invocation(spec), input_ids=()), codec=codec
    )
    assert result.requests == ()
    assert result.state.intents.intents == state.intents.intents
    assert result.state.sessions.invocations[0].phase == core.SessionPhase.EXECUTING
    assert core.pending_requests(result.state.intents) == (request,)
    note = core.SessionInput(
        input_id=core.InputId(root="registered-note"),
        target=core.ScopeInputTarget(scope=scope()),
        artifact=core.ArtifactRef(artifact_id=core.ArtifactId(root="note"), digest="note"),
        received_at=0.0,
        sequence=0,
    )
    with_notes = state.model_copy(
        update={
            "sessions": state.sessions.model_copy(
                update={
                    "inputs": (core.InputRecord(input=note, reserved_to=invocation(spec)),),
                }
            )
        }
    )
    with pytest.raises(core.ContractValidationError, match="reserved-input transport"):
        core.step(
            with_notes,
            core.TurnInputsReserved(invocation=invocation(spec), input_ids=(note.input_id,)),
        )


@pytest.mark.parametrize(
    "forgery",
    [
        "missing",
        "request-id",
        "payload",
        "scope",
        "operation-id",
        "deadline",
        "normalization",
        "lifecycle",
    ],
)
def test_registered_turn_cannot_admit_without_exact_canonical_operation(forgery: str) -> None:
    state, request, spec, codec = registered_turn_state()
    if forgery == "missing":
        state = state.model_copy(
            update={"intents": state.intents.model_copy(update={"intents": ()})}
        )
    elif forgery == "request-id":
        request = request.model_copy(
            update={"request_id": core.RequestId(root="unrecorded-request")}
        )
    elif forgery == "payload":
        request = request.model_copy(
            update={"operation": codec.encode(SessionTurnOperation(turn=turn(identity="other")))}
        )
    elif forgery == "scope":
        request = request.model_copy(update={"scope": scope().model_copy(update={"generation": 1})})
    elif forgery == "operation-id":
        request = request.model_copy(
            update={"operation_id": core.OperationId(root="unrecorded-operation")}
        )
    elif forgery == "deadline":
        request = request.model_copy(update={"deadline_at": 101.0})
    elif forgery == "normalization":
        spec = spec.model_copy(update={"max_turns": 2})
    else:
        schema = request.operation.schema_ref.model_copy(
            update={"lifecycle": core.LifecycleClass.IDEMPOTENT_WRITE}
        )
        request = request.model_copy(
            update={"operation": request.operation.model_copy(update={"schema_ref": schema})}
        )
    with pytest.raises(core.ContractValidationError):
        core.step(state, core.RegisteredTurnRequested(request=request, turn=spec))


def test_declared_cancellation_is_durable_idempotent_and_preserves_refund_authority() -> None:
    spec = turn()
    ref = invocation(spec)
    dispatched = reload_step(
        waiting_turn_state(spec), core.TurnInputsReserved(invocation=ref, input_ids=())
    )
    authority = core.RequestId(root="declared-interrupt")
    claim = core.InterruptClaim(invocation=ref, authority=authority, refund=1)
    state = dispatched.state.model_copy(
        update={"sessions": dispatched.state.sessions.model_copy(update={"interrupts": (claim,)})}
    )
    event = core.InvocationCancellationRequested(invocation=ref, authority=authority)
    result = reload_step(state, event)
    assert len(result.requests) == 1
    request = result.requests[0]
    assert isinstance(request, core.CancelTurn)
    assert request.invocation == ref
    assert request.request_id is not None
    assert request in core.pending_requests(result.state.intents)
    assert result.state.sessions.interrupts == (claim,)
    assert result.state.sessions.run_charges == state.sessions.run_charges
    assert result.state.sessions.invocations[0].phase == core.SessionPhase.EXECUTING
    replay = reload_step(result.state, event)
    assert replay.requests == ()
    assert replay.events == ()
    assert replay.state.sessions == result.state.sessions


def test_arbitrary_recorded_intent_does_not_authorize_turn_cancellation() -> None:
    spec = turn()
    dispatched = reload_step(
        waiting_turn_state(spec), core.TurnInputsReserved(invocation=invocation(spec), input_ids=())
    )
    unrelated = core.CloseSession(
        request_id=core.RequestId(root="unrelated-authority"),
        scope=scope(),
        session_id=core.SessionId(root="another-session"),
        deadline_at=100.0,
    )
    state = with_intent(dispatched.state, unrelated)
    assert unrelated.request_id is not None
    with pytest.raises(core.ContractValidationError, match="authority"):
        core.step(
            state,
            core.InvocationCancellationRequested(
                invocation=invocation(spec), authority=unrelated.request_id
            ),
        )


def test_registered_manifest_cannot_substitute_normalized_turn_payload() -> None:
    state, request, spec, _ = registered_turn_state()
    changed = spec.model_copy(update={"max_turns": 2})
    seeded = waiting_turn_state(changed)
    row = seeded.sessions.invocations[0].model_copy(
        update={"registered_operation": request.operation_id}
    )
    state = state.model_copy(
        update={"sessions": seeded.sessions.model_copy(update={"invocations": (row,)})}
    )
    with pytest.raises(core.ContractValidationError):
        core.step(state, core.TurnInputsReserved(invocation=invocation(changed), input_ids=()))


def test_late_child_discovery_invalidates_an_older_complete_empty_manifest() -> None:
    spec = turn()
    ref = invocation(spec)
    dispatched = reload_step(
        waiting_turn_state(spec), core.TurnInputsReserved(invocation=ref, input_ids=())
    )
    first = core.TurnObserved(
        invocation=ref,
        observation=turn_observation(
            dispatched.requests[0], terminal=True, status=core.ObservationStatus.SUCCEEDED
        ).model_copy(update={"children_complete": True}),
    )
    finished = reload_step(dispatched.state, first)
    child = core.ResourceId(root="late-child")
    late = core.TurnObserved(
        invocation=ref,
        observation=turn_observation(
            dispatched.requests[0],
            sequence=2,
            terminal=True,
            status=core.ObservationStatus.SUCCEEDED,
        ).model_copy(update={"children": (child,), "children_complete": False, "released": True}),
    )
    result = reload_step(finished.state, late)
    proof = result.state.sessions.invocations[0].observation
    assert proof is not None
    assert child in proof.children
    assert not proof.children_complete
    assert result.events == ()
    assert result.state.sessions.run_charges == finished.state.sessions.run_charges


def test_attempt_dispatch_borrows_run_owned_reusable_lease_without_replacing_it() -> None:
    state, _, owner_scope, admission = attempt_acquisition_state()
    spec = turn(charge="paid").model_copy(
        update={
            "session": turn().session.model_copy(update={"policy": "reuse", "lifetime": "owner"}),
            "workspace": owner_scope,
        }
    )
    ref = invocation(spec)
    owner = state.attempts.attempts[0].model_copy(
        update={
            "phase": core.AttemptPhase.ACTIVE,
            "charges": (
                core.ChargeReceipt(
                    charge_id=core.ChargeId(root="borrowed-turn"),
                    kind=core.ChargeKind.TURN,
                    invocation_id=spec.invocation_id,
                    charged=1,
                ),
                core.ChargeReceipt(
                    charge_id=core.ChargeId(root="borrowed-paid"),
                    kind=core.ChargeKind.ATTEMPT,
                    invocation_id=spec.invocation_id,
                    charged=1,
                ),
            ),
        }
    )
    resource = core.ResourceId(root="run-owned-conversation")
    session = core.SessionView(
        spec=spec.session,
        scope=scope(),
        generation=0,
        phase=core.SessionPhase.IDLE,
        invocation=spec.invocation_id,
        resource_id=resource,
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "sessions": core.SessionsState(
                sessions=(session,),
                invocations=(
                    core.Invocation(
                        invocation=ref,
                        scope=owner_scope,
                        turn=spec,
                        phase=core.SessionPhase.ACQUIRING,
                    ),
                ),
            ),
        }
    )
    result = reload_step(state, core.TurnInputsReserved(invocation=ref, input_ids=()))
    assert len(result.requests) == 1
    request = result.requests[0]
    assert isinstance(request, core.DispatchTurn)
    assert request.scope == owner_scope
    assert request.admission_id == admission
    assert result.state.sessions.sessions[0].scope == scope()
    assert result.state.sessions.sessions[0].resource_id == resource
    assert result.state.sessions.run_charges == ()
    assert result.state.attempts == state.attempts
    assert not any(
        isinstance(item, core.EnsureSession | core.CloseSession) for item in result.requests
    )


def test_provisional_acquisition_drain_reaches_declared_input_drain_boundary() -> None:
    state, ref, owner_scope, admission = attempt_acquisition_state()
    spec = turn().session
    prepared = reload_step(
        state,
        core.SessionsAcquireRequested(
            attempt=ref, admission_id=admission, scope=owner_scope, specs=(spec,)
        ),
    )
    authority = core.RequestId(root="retirement")
    closure = core.AttemptClosure(
        disposition="cancel", requested_at=1.0, authority=authority, admission_id=admission
    )
    owner = state.attempts.attempts[0].model_copy(
        update={"phase": core.AttemptPhase.CLOSING, "closure": closure}
    )
    state = prepared.state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    assert_input_boundary(
        reload_state(state),
        core.SessionDrainRequested(attempt=ref, authority=authority, disposition="cancel"),
        "session_drain_requested",
    )


@given(
    st.lists(
        st.tuples(
            st.text(alphabet="abc:012", min_size=1, max_size=10),
            st.text(alphabet="abc:012", min_size=1, max_size=10),
        ),
        min_size=2,
        max_size=2,
        unique_by=lambda pair: pair[0],
    ).filter(lambda pairs: pairs[0][1] != pairs[1][1])
)
@example([("a:0:b", "c"), ("a", "b:0:c")])
def test_generated_authority_ids_encode_components_without_delimiter_collisions(
    pairs: list[tuple[str, str]],
) -> None:
    state = core.initial_state()
    state = state.model_copy(
        update={"run": state.run.model_copy(update={"limits": core.Limits(max_turns=2)})}
    )
    specs = tuple(
        turn(identity=invocation_id).model_copy(
            update={
                "session": turn().session.model_copy(
                    update={"session_id": core.SessionId(root=session_id)}
                )
            }
        )
        for session_id, invocation_id in pairs
    )
    for spec in specs:
        result = reload_step(state, core.TurnRequested(scope=scope(), turn=spec))
        assert len(result.requests) == 1
        assert isinstance(result.requests[0], core.EnsureSession)
        state = result.state
    assert len({receipt.charge_id for receipt in state.sessions.run_charges}) == 2
    assert len({intent.request_id for intent in state.intents.intents}) == 2
    sessions = tuple(
        session.model_copy(
            update={
                "phase": core.SessionPhase.IDLE,
                "resource_id": core.ResourceId(root=f"lease-{index}"),
            }
        )
        for index, session in enumerate(state.sessions.sessions)
    )
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"sessions": sessions})}
    )
    requests = []
    for spec in specs:
        event = core.TurnInputsReserved(invocation=invocation(spec), input_ids=())
        result = reload_step(state, event)
        requests.extend(result.requests)
        state = result.state
        repeated = reload_step(state, event)
        assert repeated.requests == ()
        assert repeated.state.sessions == state.sessions
    assert len(requests) == 2
    assert len({request.request_id for request in requests}) == 2
    assert sum(receipt.charged for receipt in state.sessions.run_charges) == 2


@pytest.mark.parametrize("phase", [core.SessionPhase.IDLE, core.SessionPhase.CHECKPOINTED])
def test_missing_reusable_correspondence_blocks_admission_before_charging(
    phase: core.SessionPhase,
) -> None:
    spec = turn().model_copy(
        update={
            "session": turn().session.model_copy(update={"policy": "reuse", "lifetime": "owner"})
        }
    )
    session = core.SessionView(spec=spec.session, scope=scope(), generation=0, phase=phase)
    state = core.initial_state().model_copy(
        update={"sessions": core.SessionsState(sessions=(session,))}
    )
    before = state.model_dump_json()
    with pytest.raises(core.ContractValidationError, match="correspondence"):
        core.step(state, core.TurnRequested(scope=scope(), turn=spec))
    assert state.model_dump_json() == before
    assert state.sessions.invocations == ()
    assert state.sessions.run_charges == ()


def attempt_turn_dispatch_state(charge: str) -> tuple[core.CoreState, core.TurnSpec]:
    state, _, owner_scope, admission = attempt_acquisition_state()
    spec = turn(identity=f"{charge}-turn", charge=charge, max_turns=50).model_copy(
        update={"workspace": owner_scope}
    )
    ref = invocation(spec)
    logical = core.ChargeReceipt(
        charge_id=core.ChargeId(root="current-turn"),
        kind=core.ChargeKind.TURN,
        invocation_id=spec.invocation_id,
        charged=1,
    )
    charges = (logical,)
    invocations = (
        core.Invocation(
            invocation=ref, scope=owner_scope, turn=spec, phase=core.SessionPhase.ACQUIRING
        ),
    )
    checkpoints: tuple[core.AttemptCheckpoint, ...] = ()
    evaluation = core.EvaluationState()
    session_phase = core.SessionPhase.IDLE
    if charge == "paid":
        paid = core.ChargeReceipt(
            charge_id=core.ChargeId(root="current-paid"),
            kind=core.ChargeKind.ATTEMPT,
            invocation_id=spec.invocation_id,
            charged=1,
        )
        charges = (*charges, paid)
    else:
        prior_turn = turn(identity="yielded", charge="paid").model_copy(
            update={"workspace": owner_scope}
        )
        prior_ref = invocation(prior_turn)
        continuation_id = core.ContinuationId(root="wait")
        spec = spec.model_copy(update={"continuation_id": continuation_id})
        prior_request = core.DispatchTurn(
            request_id=core.RequestId(root="yielded-request"),
            scope=owner_scope,
            admission_id=admission,
            deadline_at=100.0,
            turn=prior_turn,
        )
        observation = turn_observation(
            prior_request, accepted=True, terminal=True, status=core.ObservationStatus.SUCCEEDED
        ).model_copy(update={"admission_id": admission})
        prior = core.Invocation(
            invocation=prior_ref,
            scope=owner_scope,
            turn=prior_turn,
            phase=core.SessionPhase.SUSPENDED,
            observation=observation,
        )
        invocations = (prior, invocations[0].model_copy(update={"turn": spec}))
        charges = (
            *charges,
            core.ChargeReceipt(
                charge_id=core.ChargeId(root="yielded-turn"),
                kind=core.ChargeKind.TURN,
                invocation_id=prior_turn.invocation_id,
                charged=1,
            ),
            core.ChargeReceipt(
                charge_id=core.ChargeId(root="yielded-paid"),
                kind=core.ChargeKind.ATTEMPT,
                invocation_id=prior_turn.invocation_id,
                charged=1,
            ),
        )
        checkpoints = (
            core.AttemptCheckpoint(
                invocation=prior_ref,
                request_id=core.RequestId(root="yielded-checkpoint"),
                revision=state.run.facts.baseline,
                retention="wip",
            ),
        )
        evaluation = core.EvaluationState(
            continuations=(
                core.Continuation(
                    continuation_id=continuation_id,
                    invocation=prior_ref,
                    next_invocation=ref,
                    jobs=(),
                    deadline_at=80.0,
                    phase=core.ContinuationPhase.AUTHORIZED,
                ),
            )
        )
        session_phase = core.SessionPhase.SUSPENDED
    owner = state.attempts.attempts[0].model_copy(
        update={"phase": core.AttemptPhase.ACTIVE, "charges": charges, "checkpoints": checkpoints}
    )
    session = core.SessionView(
        spec=spec.session,
        scope=owner_scope,
        generation=0,
        phase=session_phase,
        invocation=spec.invocation_id,
        resource_id=core.ResourceId(root="conversation"),
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={"limits": core.Limits(max_turns=10, max_retries=2)}
            ),
            "attempts": core.AttemptsState(attempts=(owner,)),
            "sessions": core.SessionsState(sessions=(session,), invocations=invocations),
            "evaluation": evaluation,
        }
    )
    return state, spec


def test_run_paid_turn_has_no_attempt_currency_authority() -> None:
    state = core.initial_state()
    spec = turn(charge="paid")
    before = state.model_dump_json()
    with pytest.raises(core.ContractValidationError, match="ATTEMPT"):
        core.step(state, core.TurnRequested(scope=scope(), turn=spec))
    assert state.model_dump_json() == before
    decision = core.RequestTurn(
        decision_id=core.DecisionId(root="run-paid"), scope=scope(), turn=spec
    )
    result = reload_step(state, core.DecisionSubmitted(decision=decision, expected_revision=0))
    assert result.requests == ()
    assert result.state.sessions == state.sessions
    assert len(result.events) == 1
    assert isinstance(result.events[0], core.Rejected)
    assert result.events[0].code == core.RejectionCode.OWNERSHIP


def test_ready_lease_cannot_overwrite_an_invocation_waiting_for_dispatch() -> None:
    original = turn()
    state = waiting_turn_state(original)
    state = state.model_copy(
        update={"run": state.run.model_copy(update={"limits": core.Limits(max_turns=3)})}
    )
    next_turn = original.model_copy(update={"invocation_id": core.InvocationId(root="next")})
    before = state.model_dump_json()
    with pytest.raises(core.ContractValidationError, match="invocation"):
        core.step(state, core.TurnRequested(scope=scope(), turn=next_turn))
    assert state.model_dump_json() == before
    assert state.sessions.sessions[0].invocation == original.invocation_id
    assert len(state.sessions.invocations) == 1
    assert len(state.sessions.run_charges) == 1


@pytest.mark.parametrize("status", list(core.ObservationStatus))
@pytest.mark.parametrize("accepted", [False, True])
def test_acquisition_requires_conclusive_readiness_or_nonacceptance(
    status: core.ObservationStatus, *, accepted: bool
) -> None:
    spec = turn()
    prepared = reload_step(core.initial_state(), core.TurnRequested(scope=scope(), turn=spec))
    observation = turn_observation(
        prepared.requests[0], terminal=True, accepted=accepted, status=status
    )
    event = core.SessionObserved(session_id=spec.session.session_id, observation=observation)
    abandonment = not accepted and status in (
        core.ObservationStatus.FAILED,
        core.ObservationStatus.REJECTED,
        core.ObservationStatus.CANCELLED,
    )
    if abandonment or (accepted and status == core.ObservationStatus.SUCCEEDED):
        with pytest.raises(core.KernelNotImplementedError) as boundary:
            core.step(reload_state(prepared.state), event)
        assert boundary.value.subarea == "_session_inputs"
        assert boundary.value.event_kind == (
            "input_reservation_released" if abandonment else "input_reservation_requested"
        )
    else:
        result = reload_step(prepared.state, event)
        assert result.state.sessions.sessions[0].phase == core.SessionPhase.UNKNOWN
        assert prepared.requests[0].request_id in result.state.sessions.sessions[0].pending_intents
        assert result.state.sessions.invocations[0].phase == core.SessionPhase.ACQUIRING
        assert len(result.requests) == 1
        assert isinstance(result.requests[0], core.InspectRequest)
        assert result.requests[0].resource_id is None
        assert result.events == ()
        assert result.state.sessions.run_charges == prepared.state.sessions.run_charges
        if status == core.ObservationStatus.SUCCEEDED:
            confirmed = event.model_copy(
                update={
                    "observation": observation.model_copy(update={"sequence": 2, "accepted": True})
                }
            )
            assert_input_boundary(
                reload_state(result.state), confirmed, "input_reservation_requested"
            )


@pytest.mark.parametrize("registered", [False, True])
def test_late_acquisition_closes_lease_and_cancels_waiting_decision(*, registered: bool) -> None:
    state, spec = attempt_turn_dispatch_state("paid")
    owner = state.attempts.attempts[0]
    owner_scope = state.sessions.invocations[0].scope
    assert owner.admission_id is not None
    decision = core.RequestTurn(
        decision_id=core.DecisionId(root="pending-turn"), scope=owner_scope, turn=spec
    )
    codec = None
    registered_intents = ()
    if registered:
        state, operation, _, codec = registered_turn_state(
            spec=spec, owner_scope=owner_scope, initial=state
        )
        registered_intents = state.intents.intents
        decision = state.run.receipts[0].decision
        current = state.sessions.invocations[0].model_copy(
            update={"registered_operation": operation.operation_id}
        )
        state = state.model_copy(
            update={"sessions": state.sessions.model_copy(update={"invocations": (current,)})}
        )
    assert isinstance(decision, (core.RequestTurn, core.Operation))
    ensure = core.EnsureSession(
        request_id=core.RequestId(root="pending-ensure"),
        scope=owner_scope,
        admission_id=owner.admission_id,
        decision_id=decision.decision_id,
        spec=spec.session,
        deadline_at=100.0,
    )
    assert ensure.request_id is not None
    state = with_intent(state, ensure)
    state = state.model_copy(
        update={
            "intents": state.intents.model_copy(
                update={"intents": registered_intents + state.intents.intents}
            )
        }
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=value_digest(decision),
        feedback=core.Accepted(decision_id=decision.decision_id),
        request_ids=(ensure.request_id,),
    )
    closure = core.AttemptClosure(
        disposition="cancel",
        requested_at=1.0,
        authority=core.RequestId(root="close-attempt"),
        admission_id=owner.admission_id,
    )
    owner = owner.model_copy(update={"phase": core.AttemptPhase.CLOSING, "closure": closure})
    session = state.sessions.sessions[0].model_copy(
        update={
            "phase": core.SessionPhase.ACQUIRING,
            "resource_id": None,
            "pending_intents": (ensure.request_id,),
        }
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"receipts": (receipt,)}),
            "attempts": core.AttemptsState(attempts=(owner,)),
            "sessions": state.sessions.model_copy(update={"sessions": (session,)}),
        }
    )
    observation = turn_observation(
        ensure, terminal=True, accepted=True, status=core.ObservationStatus.SUCCEEDED
    ).model_copy(update={"admission_id": owner.admission_id})
    result = reload_step(
        state,
        core.SessionObserved(session_id=spec.session.session_id, observation=observation),
        codec=codec,
    )
    assert result.state.sessions.invocations[0].phase == core.SessionPhase.TERMINAL
    assert result.state.run.receipts[0].completion == core.CompletionStatus.CANCELLED
    assert any(isinstance(request, core.CloseSession) for request in result.requests)


def test_distinct_decision_cannot_create_a_second_origin_for_one_invocation() -> None:
    decision = core.RequestTurn(
        decision_id=core.DecisionId(root="first-origin"), scope=scope(), turn=turn()
    )
    admitted = reload_step(
        core.initial_state(), core.DecisionSubmitted(decision=decision, expected_revision=0)
    )
    duplicate = decision.model_copy(update={"decision_id": core.DecisionId(root="second-origin")})
    rejected = reload_step(
        admitted.state,
        core.DecisionSubmitted(decision=duplicate, expected_revision=admitted.state.revision),
    )
    assert rejected.requests == ()
    assert rejected.state.sessions == admitted.state.sessions
    assert isinstance(rejected.events[0], core.Rejected)
    assert rejected.events[0].code == core.RejectionCode.IDENTITY_CONFLICT
    replay = reload_step(
        rejected.state,
        core.DecisionSubmitted(decision=decision, expected_revision=rejected.state.revision),
    )
    assert replay.requests == ()
    assert replay.state.sessions == admitted.state.sessions


@pytest.mark.parametrize("phase", [core.SessionPhase.TERMINAL, core.SessionPhase.CLOSING])
def test_turn_completion_cannot_revive_a_closed_or_closing_physical_session(
    phase: core.SessionPhase,
) -> None:
    spec = turn()
    ref = invocation(spec)
    dispatched = reload_step(
        waiting_turn_state(spec), core.TurnInputsReserved(invocation=ref, input_ids=())
    )
    _, close = closing_session_state()
    close_state = with_intent(dispatched.state, close)
    session = dispatched.state.sessions.sessions[0].model_copy(
        update={"phase": core.SessionPhase.CLOSING, "pending_intents": (close.request_id,)}
    )
    state = dispatched.state.model_copy(
        update={
            "intents": dispatched.state.intents.model_copy(
                update={
                    "intents": dispatched.state.intents.intents + close_state.intents.intents,
                }
            ),
            "sessions": dispatched.state.sessions.model_copy(update={"sessions": (session,)}),
        }
    )
    if phase == core.SessionPhase.TERMINAL:
        release = turn_observation(close, terminal=True, status=core.ObservationStatus.SUCCEEDED)
        state = reload_step(
            state,
            core.SessionObserved(
                session_id=spec.session.session_id,
                observation=release.model_copy(
                    update={"released": True, "children_complete": True}
                ),
            ),
        ).state
    completed = reload_step(
        state,
        core.TurnObserved(
            invocation=ref,
            observation=turn_observation(
                dispatched.requests[0], terminal=True, status=core.ObservationStatus.SUCCEEDED
            ),
        ),
    )
    assert completed.state.sessions.sessions[0].phase == phase
    assert completed.state.sessions.invocations[0].phase == core.SessionPhase.TERMINAL


@pytest.mark.parametrize("case", ["ready", "failed", "missing-resource", "outstanding-turn"])
def test_required_group_reattaches_run_owned_lease_without_transferring_or_closing_it(
    case: str,
) -> None:
    state, ref, owner_scope, admission = attempt_acquisition_state()
    spec = turn().model_copy(
        update={
            "session": turn().session.model_copy(update={"policy": "reuse", "lifetime": "owner"})
        }
    )
    resource = core.ResourceId(root="persisted-run-conversation")
    session = core.SessionView(
        spec=spec.session,
        scope=scope(),
        generation=0,
        phase=core.SessionPhase.IDLE,
        resource_id=None if case == "missing-resource" else resource,
        accepted=True,
        acceptance_sequence=1,
        invocation=spec.invocation_id if case == "outstanding-turn" else None,
    )
    outstanding = (
        (
            core.Invocation(
                invocation=invocation(spec),
                scope=scope(),
                turn=spec,
                phase=core.SessionPhase.ACQUIRING,
            ),
        )
        if case == "outstanding-turn"
        else ()
    )
    state = state.model_copy(
        update={
            "sessions": core.SessionsState(
                sessions=(session,),
                invocations=outstanding,
            )
        }
    )
    event = core.SessionsAcquireRequested(
        attempt=ref,
        admission_id=admission,
        scope=owner_scope,
        specs=(spec.session,),
    )
    if case in ("missing-resource", "outstanding-turn"):
        before = state.model_dump_json()
        with pytest.raises(
            core.ContractValidationError,
            match=("correspondence" if case == "missing-resource" else "outstanding invocation"),
        ):
            core.step(reload_state(state), event)
        assert state.model_dump_json() == before
        return
    acquired = reload_step(state, event)
    assert len(acquired.requests) == 1
    ensure = acquired.requests[0]
    assert isinstance(ensure, core.EnsureSession)
    assert ensure.scope == scope()
    assert ensure.required_resource == resource
    assert ensure.admission_id == admission
    assert acquired.state.sessions.sessions[0].scope == scope()
    assert acquired.state.sessions.sessions[0].resource_id == resource
    observation = turn_observation(
        ensure,
        sequence=2,
        terminal=True,
        accepted=True,
        status=core.ObservationStatus.SUCCEEDED,
    ).model_copy(update={"admission_id": admission, "resource_id": resource})
    confirmed = core.SessionObserved(session_id=spec.session.session_id, observation=observation)
    if case == "ready":
        boundary = core.step(reload_state(acquired.state), confirmed)
        assert boundary.requests == ()
        assert boundary.events == ()
        assert boundary.state.sessions.acquisition_groups[0].phase == "ready"
        assert boundary.state.attempts.attempts[0].phase == core.AttemptPhase.ACQUIRING
    else:
        failed = acquired.state.sessions.acquisition_groups[0].model_copy(
            update={
                "phase": "failed",
                "failure_request": core.RequestId(root="failed-required-member"),
            }
        )
        abandoned = acquired.state.model_copy(
            update={
                "sessions": acquired.state.sessions.model_copy(
                    update={"acquisition_groups": (failed,)}
                )
            }
        )
        result = reload_step(abandoned, confirmed)
        assert result.requests == result.events == ()
        assert result.state.sessions.acquisition_groups[0].phase == "failed"
        assert result.state.sessions.sessions[0].scope == scope()
        assert result.state.sessions.sessions[0].resource_id == resource
        assert result.state.sessions.sessions[0].phase == core.SessionPhase.IDLE
        assert result.state.sessions.run_charges == ()
        assert result.state.attempts == state.attempts
