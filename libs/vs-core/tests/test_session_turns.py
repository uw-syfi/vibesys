"""Session-turn lifecycle properties through the published pure kernel API."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core


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


def reload_state(state: core.CoreState) -> core.CoreState:
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
    codec = core.OperationRegistry()
    return codec.decode_envelope(
        core.RunEnvelope[core.StrategyState], codec.encode_envelope(envelope)
    ).core


def reload_step(state: core.CoreState, event: core.CoreEvent) -> core.Transition:
    """Each boundary must survive canonical public-model serialization."""
    before = state.model_dump_json()
    result = core.step(state, event)
    assert result == core.step(reload_state(state), event)
    assert state.model_dump_json() == before
    assert reload_state(result.state) == result.state
    assert core.project(result.state) == core.project(reload_state(result.state))
    return result


@given(
    st.sampled_from(("paid", "free")),
    st.integers(min_value=1, max_value=100),
)
def test_run_turn_admission_records_one_charge_and_durable_ensure(
    charge: str, max_turns: int
) -> None:
    state = core.initial_state()
    spec = turn(charge=charge, max_turns=max_turns)
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
    state = core.initial_state()
    spec = turn()
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
        charge_id=core.ChargeId(root="turn-charge"),
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
    prior = invocation(turn(identity="previous"))
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
def test_all_charge_classes_dispatch_one_logical_turn_without_multiplying_provider_bound(
    charge: str,
) -> None:
    spec = turn(charge=charge, max_turns=50)
    state = waiting_turn_state(spec)
    if charge == "resume":
        prior_turn = turn(identity="yielded")
        prior_ref = invocation(prior_turn)
        continuation_id = core.ContinuationId(root="wait")
        spec = spec.model_copy(update={"continuation_id": continuation_id})
        state = waiting_turn_state(spec)
        prior = core.Invocation(
            invocation=prior_ref,
            scope=scope(),
            turn=prior_turn,
            phase=core.SessionPhase.SUSPENDED,
        )
        state = state.model_copy(
            update={
                "sessions": state.sessions.model_copy(
                    update={"invocations": (prior, *state.sessions.invocations)}
                ),
                "evaluation": core.EvaluationState(
                    continuations=(
                        core.Continuation(
                            continuation_id=continuation_id,
                            invocation=prior_ref,
                            next_invocation=invocation(spec),
                            jobs=(),
                            deadline_at=80.0,
                            phase=core.ContinuationPhase.AUTHORIZED,
                        ),
                    )
                ),
            }
        )
    result = reload_step(state, core.TurnInputsReserved(invocation=invocation(spec), input_ids=()))
    assert len(result.requests) == 1
    if charge == "resume":
        assert isinstance(result.requests[0], core.ResumeSessionTurn)
    else:
        assert isinstance(result.requests[0], core.DispatchTurn)
    assert result.state.sessions.run_charges == state.sessions.run_charges
    assert sum(receipt.charged for receipt in result.state.sessions.run_charges) == 1
    assert all(
        receipt.kind == core.ChargeKind.TURN for receipt in result.state.sessions.run_charges
    )


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
        payload_digest="fixture-correspondence",
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


def test_release_for_another_physical_session_cannot_close_owned_conversation() -> None:
    state, request = closing_session_state()
    observation = turn_observation(
        request, terminal=True, accepted=True, status=core.ObservationStatus.SUCCEEDED
    ).model_copy(
        update={
            "resource_id": core.ResourceId(root="other-conversation"),
            "released": True,
            "children_complete": True,
        }
    )
    result = reload_step(
        state, core.SessionObserved(session_id=request.session_id, observation=observation)
    )
    assert result.state.sessions == state.sessions
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


def test_f1_planner_acquisition_and_correction_reach_declared_input_boundary() -> None:
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
    with pytest.raises(core.KernelNotImplementedError) as acquisition:
        core.step(reload_state(prepared.state), event)
    assert acquisition.value.subarea == "_session_inputs"
    assert acquisition.value.event_kind == "input_reservation_requested"
    state, predecessor = malformed_planner_state()
    correction = turn(identity="correction", charge="correction").model_copy(
        update={"predecessor": predecessor}
    )
    with pytest.raises(core.KernelNotImplementedError) as corrected:
        core.step(reload_state(state), core.TurnRequested(scope=scope(), turn=correction))
    assert corrected.value.subarea == "_session_inputs"
    assert corrected.value.event_kind == "input_reservation_requested"
    # Legacy agentshim_driver assertions: correcting the first malformed reply keeps s-1.
    assert state.sessions.sessions[0].resource_id == core.ResourceId(root="conversation")
    assert correction.session == state.sessions.sessions[0].spec


@given(st.integers(min_value=0, max_value=1))
def test_correction_requires_remaining_retry_authority(max_retries: int) -> None:
    state, predecessor = malformed_planner_state()
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={"limits": core.Limits(max_turns=10, max_retries=max_retries)}
            )
        }
    )
    correction = turn(identity="correction", charge="correction").model_copy(
        update={"predecessor": predecessor}
    )
    if max_retries == 0:
        with pytest.raises(core.ContractValidationError, match="retry"):
            core.step(state, core.TurnRequested(scope=scope(), turn=correction))
    else:
        with pytest.raises(core.KernelNotImplementedError) as reached:
            core.step(state, core.TurnRequested(scope=scope(), turn=correction))
        assert reached.value.subarea == "_session_inputs"
        assert reached.value.event_kind == "input_reservation_requested"


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
