"""Run-owned session authority and execution manifests through public contracts."""

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter, ValidationError

import vs_core.api as core


def _scope(generation: int = 0) -> core.Scope:
    return core.Scope(owner=core.RunId(root="run"), generation=generation)


def _invocation(generation: int = 0) -> core.InvocationRef:
    return core.InvocationRef(
        session_id=core.SessionId(root="session"),
        invocation_id=core.InvocationId(root="invocation"),
        generation=generation,
    )


def _observation(
    generation: int = 0,
    status: core.ObservationStatus = core.ObservationStatus.FAILED,
    *,
    accepted: bool = False,
    terminal: bool = True,
) -> core.Observation:
    return core.Observation(
        event_id=core.EventId(root="observed"),
        request_id=core.RequestId(root="checkpoint"),
        scope=_scope(generation),
        sequence=1,
        observed_at=1.0,
        status=status,
        accepted=accepted,
        terminal=terminal,
    )


def _checkpoint(generation: int = 0) -> core.RunInvocationCheckpoint:
    return core.RunInvocationCheckpoint(
        invocation=_invocation(generation),
        scope=_scope(generation),
        request_id=core.RequestId(root="checkpoint"),
        revision=core.RevisionRef(revision_id=core.RevisionId(root="retained"), digest="digest"),
        retention="wip",
    )


def _envelope(state: core.CoreState) -> core.RunEnvelope[core.StrategyState]:
    return core.RunEnvelope[core.StrategyState](
        schema_version=core.ENVELOPE_SCHEMA_VERSION,
        fence=core.HostFence(host_id=core.HostId(root="host"), epoch=1),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=core.StrategyState(schema_version=1),
        event_cursor=core.EventCursor(sequence=0),
    )


@given(st.integers(min_value=0, max_value=1000))
def test_run_checkpoint_receipt_survives_public_envelope_codec(generation: int) -> None:
    state = core.initial_state()
    state = state.model_copy(
        update={"sessions": core.SessionsState(run_checkpoints=(_checkpoint(generation),))}
    )
    registry = core.OperationRegistry()
    envelope = _envelope(state)
    restored = registry.decode_envelope(type(envelope), registry.encode_envelope(envelope))
    assert restored.core.sessions.run_checkpoints == state.sessions.run_checkpoints
    assert restored.core.run.result is None
    # An unrelated event cannot manufacture, remove or reinterpret checkpoint proof.
    event = core.RunControlEvent(
        control=core.ControlInput(control_id=core.ControlId(root="steer"), action="steer"),
        now_at=2.0,
    )
    assert core.step(restored.core, event).state.sessions == state.sessions


@given(st.integers(min_value=0, max_value=1000), st.integers(min_value=1, max_value=1000))
def test_run_checkpoint_contract_rejects_foreign_generation_and_attempt_owner(
    generation: int, difference: int
) -> None:
    receipt = _checkpoint(generation)
    for scope in (
        _scope(generation + difference),
        core.Scope(owner=core.AttemptId(root="attempt"), generation=generation),
    ):
        payload = receipt.model_dump()
        payload["scope"] = scope
        with pytest.raises(ValidationError, match=r"scope|generation"):
            core.RunInvocationCheckpoint.model_validate(payload)
    with pytest.raises(ValidationError, match="duplicate checkpoint authority"):
        core.SessionsState(run_checkpoints=(receipt, receipt))


@given(st.integers(min_value=0, max_value=1000))
def test_run_checkpoint_observation_preserves_absent_revision_without_proof(
    generation: int,
) -> None:
    event = core.RunInvocationCheckpointObserved(
        invocation=_invocation(generation),
        checkpoint_request=core.RequestId(root="checkpoint"),
        observation=_observation(generation),
    )
    adapter = TypeAdapter(core.CoreEvent)
    assert adapter.validate_json(adapter.dump_json(event)) == event
    assert event.revision is None
    payload = event.model_dump()
    payload["checkpoint_request"] = core.RequestId(root="foreign")
    with pytest.raises(ValidationError, match="checkpoint_request"):
        core.RunInvocationCheckpointObserved.model_validate(payload)


@given(
    failure=st.sampled_from(tuple(core.SetupFailureKind)),
    accepted=st.booleans(),
    terminal=st.booleans(),
    status=st.sampled_from(tuple(core.ObservationStatus)),
)
def test_setup_failure_classification_requires_terminal_failure(
    *,
    failure: core.SetupFailureKind,
    accepted: bool,
    terminal: bool,
    status: core.ObservationStatus,
) -> None:
    observation = _observation(status=status, accepted=accepted, terminal=terminal)
    classified = terminal and status in (
        core.ObservationStatus.FAILED,
        core.ObservationStatus.REJECTED,
        core.ObservationStatus.CANCELLED,
    )
    if failure != core.SetupFailureKind.UNKNOWN and not classified:
        with pytest.raises(ValidationError, match="failure"):
            core.SessionObserved(
                session_id=core.SessionId(root="session"), observation=observation, failure=failure
            )
    else:
        event = core.SessionObserved(
            session_id=core.SessionId(root="session"), observation=observation, failure=failure
        )
        adapter = TypeAdapter(core.CoreEvent)
        assert adapter.validate_json(adapter.dump_json(event)) == event
        assert event.failure == failure


@given(st.lists(st.integers(min_value=1, max_value=100), min_size=0, max_size=10))
def test_pool_capacities_are_positive_unique_and_preserved(capacities: list[int]) -> None:
    pools = tuple(
        core.PoolCapacity(pool_id=core.PoolId(root=f"pool-{index}"), capacity=capacity)
        for index, capacity in enumerate(capacities)
    )
    limits = core.Limits(pool_capacities=pools)
    assert core.Limits.model_validate_json(limits.model_dump_json()) == limits
    if pools:
        with pytest.raises(ValidationError, match="duplicate pool_id"):
            core.Limits(pool_capacities=(*pools, pools[0]))
    with pytest.raises(ValidationError, match="capacity"):
        core.PoolCapacity(pool_id=core.PoolId(root="invalid"), capacity=0)


@given(st.integers(min_value=1, max_value=10))
def test_registered_input_manifest_preserves_distinct_equal_artifact_occurrences(
    count: int,
) -> None:
    artifact = core.ArtifactRef(artifact_id=core.ArtifactId(root="input"), digest="same")
    inputs = tuple(
        core.ReservedInputOccurrence(
            input_id=core.InputId(root=f"input-{index}"), artifact=artifact
        )
        for index in range(count)
    )
    request = core.ExecuteRegisteredOperation(
        scope=_scope(),
        deadline_at=10.0,
        operation_id=core.OperationId(root="operation"),
        operation=core.OperationWire(
            schema_ref=core.OperationSchemaRef(
                kind="custom.turn",
                request_schema=core.SchemaRef(name="request", version=1),
                outcome_schema=core.SchemaRef(name="outcome", version=1),
                lifecycle=core.LifecycleClass.SESSION_TURN,
            ),
            payload_json="{}",
        ),
        retry_limit=0,
        inputs=inputs,
    )
    restored = TypeAdapter(core.Request).validate_json(request.model_dump_json())
    assert restored == request
    assert restored.operation == request.operation
    for lifecycle in core.LifecycleClass:
        if lifecycle == core.LifecycleClass.SESSION_TURN:
            continue
        payload = request.model_dump()
        payload["operation"]["schema_ref"]["lifecycle"] = lifecycle
        with pytest.raises(ValidationError, match="SESSION_TURN"):
            core.ExecuteRegisteredOperation.model_validate(payload)
    with pytest.raises(ValidationError, match="duplicate input_id"):
        core.ExecuteRegisteredOperation.model_validate(
            {**request.model_dump(), "inputs": (*inputs, inputs[0])}
        )


@given(st.sampled_from(["pause", "resume", "steer"]))
def test_operator_stop_result_is_required_only_for_stop(action: str) -> None:
    result = core.RunResultProposal(outcome="cancelled", reason="operator requested stop")
    payload = {
        "control": core.ControlInput(control_id=core.ControlId(root="stop"), action="stop"),
        "now_at": 1.0,
    }
    with pytest.raises(ValidationError, match="result"):
        core.RunControlEvent.model_validate(payload)
    event = core.RunControlEvent.model_validate({**payload, "result": result})
    assert TypeAdapter(core.CoreEvent).validate_json(event.model_dump_json()) == event
    payload["control"] = core.ControlInput.model_validate(
        {"control_id": core.ControlId(root="other"), "action": action}
    )
    with pytest.raises(ValidationError, match="result"):
        core.RunControlEvent.model_validate({**payload, "result": result})


@given(st.integers(min_value=1, max_value=100))
def test_run_paid_turn_is_rejected_without_spending_any_currency(max_turns: int) -> None:
    state = core.initial_state()
    turn = core.TurnSpec(
        session=core.SessionSpec(
            session_id=core.SessionId(root="planner"),
            role_id=core.RoleId(root="planner"),
            policy="fresh",
            lifetime="ephemeral",
            access=core.Access.READ_ONLY,
        ),
        invocation_id=core.InvocationId(root="planner"),
        workspace=_scope(),
        prompts=(),
        output_schema=core.SchemaRef(name="output", version=1),
        deadline_at=10.0,
        charge_class="paid",
        max_turns=max_turns,
    )
    event = core.DecisionSubmitted(
        decision=core.RequestTurn(
            decision_id=core.DecisionId(root="paid"), scope=_scope(), turn=turn
        ),
        expected_revision=0,
    )
    envelope = _envelope(state)
    codec = core.OperationRegistry()
    restored = codec.decode_envelope(type(envelope), codec.encode_envelope(envelope))
    result = core.step(state, event)
    assert core.step(restored.core, event) == result
    assert result.requests == ()
    assert result.state.sessions.run_charges == state.sessions.run_charges
    assert result.state.attempts == state.attempts
    assert isinstance(result.events[0], core.Rejected)
    assert result.events[0].code == core.RejectionCode.OWNERSHIP
    assert result.events[0].path == ("turn", "charge_class")


@given(st.integers(min_value=0, max_value=1000))
def test_run_checkpoint_pending_request_preserves_intent_in_public_codec(generation: int) -> None:
    state = core.initial_state()
    request = core.SnapshotAndRetainRun(
        request_id=core.RequestId(root="checkpoint"),
        scope=_scope(generation),
        invocation=_invocation(generation),
        retention="wip",
        deadline_at=30.0,
    )
    intent = core.Intent(
        request_id=core.RequestId(root="checkpoint"),
        request=request,
        payload_digest="checkpoint-payload",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        phase=core.IntentPhase.PREPARED,
        reconcile_deadline_at=30.0,
    )
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"intents": (intent,)})}
    )
    codec = core.OperationRegistry()
    envelope = _envelope(state)
    restored = codec.decode_envelope(type(envelope), codec.encode_envelope(envelope))
    assert restored.core.intents.intents == (intent,)
    assert restored.core.sessions.run_checkpoints == ()
    event = core.RunInvocationCheckpointRequested(
        scope=_scope(generation),
        invocation=_invocation(generation),
        retention="wip",
        authority=core.RequestId(root="turn"),
    )
    handler = core.EVENT_ROUTES[core.Area.SESSIONS][type(event)]
    assert handler.__module__ == "vs_core._session_turns"
    steer = core.RunControlEvent(
        control=core.ControlInput(control_id=core.ControlId(root="steer"), action="steer"),
        now_at=2.0,
    )
    unchanged = core.step(restored.core, steer)
    assert unchanged.state.intents.intents == (intent,)
    assert unchanged.state.sessions.run_checkpoints == ()


@given(st.integers(min_value=0, max_value=1000))
def test_run_drain_requires_run_ownership_and_typed_stop_authority(generation: int) -> None:
    event = core.RunSessionsDrainRequested(
        scope=_scope(generation), authority=core.DecisionId(root="stop")
    )
    adapter = TypeAdapter(core.CoreEvent)
    assert adapter.validate_json(adapter.dump_json(event)) == event
    with pytest.raises(ValidationError, match=r"scope.owner"):
        core.RunSessionsDrainRequested(
            scope=core.Scope(owner=core.AttemptId(root="attempt"), generation=generation),
            authority=core.DecisionId(root="stop"),
        )
    with pytest.raises(ValidationError, match="authority"):
        core.RunSessionsDrainRequested.model_validate(
            {"scope": _scope(generation), "authority": core.RequestId(root="stop")}
        )


@given(
    failure=st.sampled_from(tuple(core.SetupFailureKind)),
    accepted=st.booleans(),
)
def test_setup_classification_survives_root_target_and_canonical_intent_codecs(
    *, failure: core.SetupFailureKind, accepted: bool
) -> None:
    observation = _observation(accepted=accepted)
    root = core.RequestObserved(observation=observation, setup_failure=failure)
    target = core.TargetObservation(observation=observation, setup_failure=failure)
    query = core.RequestObserved(
        observation=core.Observation(
            event_id=core.EventId(root="inspection"),
            request_id=core.RequestId(root="query"),
            scope=_scope(),
            sequence=3,
            observed_at=2.0,
            status=core.ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
        ),
        target=target,
    )
    adapter = TypeAdapter(core.CoreEvent)
    assert adapter.validate_json(adapter.dump_json(root)) == root
    restored_query = adapter.validate_json(adapter.dump_json(query))
    assert isinstance(restored_query, core.RequestObserved)
    assert restored_query.setup_failure == core.SetupFailureKind.UNKNOWN
    assert restored_query.target == target
    request = core.EnsureSession(
        request_id=observation.request_id,
        scope=observation.scope,
        deadline_at=10.0,
        spec=core.SessionSpec(
            session_id=core.SessionId(root="session"),
            role_id=core.RoleId(root="planner"),
            policy="fresh",
            lifetime="ephemeral",
            access=core.Access.READ_ONLY,
        ),
    )
    intent = core.Intent(
        request_id=observation.request_id,
        request=request,
        payload_digest="ensure-payload",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        phase=core.IntentPhase.COMPLETED,
        observation=observation,
        setup_failure=failure,
        sequence=observation.sequence,
        reconcile_deadline_at=10.0,
    )
    state = core.initial_state()
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"intents": (intent,)})}
    )
    codec = core.OperationRegistry()
    envelope = _envelope(state)
    restored = codec.decode_envelope(type(envelope), codec.encode_envelope(envelope))
    assert restored.core.intents.intents[0].setup_failure == failure
    restored_observation = restored.core.intents.intents[0].observation
    assert restored_observation is not None
    assert restored_observation.accepted == accepted
    steer = core.RunControlEvent(
        control=core.ControlInput(control_id=core.ControlId(root="steer"), action="steer"),
        now_at=3.0,
    )
    assert core.step(restored.core, steer).state.intents == state.intents
    if accepted:
        # Even FAILED does not turn accepted external ownership into release.
        assert not restored_observation.released
