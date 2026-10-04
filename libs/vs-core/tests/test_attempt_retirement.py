"""Attempt retirement proofs survive duplicates, stale events and crash reloads."""

from typing import Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vs_core.api as core


def reload_state(
    state: core.CoreState,
    codec: core.OperationRegistry | None = None,
) -> core.CoreState:
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


def fake_inputs(
    state: core.SessionsState, context: core.SessionsContext, event: core.SessionsEvent
) -> core.AreaChange[core.SessionsState]:
    """Empty input owner has nothing to drain; never fabricate release proof."""
    if not isinstance(event, core.SessionDrainRequested) or state.inputs:
        raise core.ContractValidationError("event", "unsupported Fake Inputs event")
    del context
    return core.AreaChange(state=state)


def fake_evaluation(
    state: core.EvaluationState, context: core.EvaluationContext, event: core.EvaluationEvent
) -> core.AreaChange[core.EvaluationState]:
    """Drain transport retains all jobs until tests supply exact owner proofs."""
    if not isinstance(event, core.JobsDrainRequested):
        raise core.ContractValidationError("event", "unsupported Fake Evaluation event")
    if not any(
        row.closure is not None and row.closure.authority == event.authority
        for row in context.attempts.attempts
    ):
        raise core.ContractValidationError("authority", "missing committed closure")
    return core.AreaChange(state=state)


REDUCERS = core.CoreReducers(evaluation=fake_evaluation, session_inputs=fake_inputs)


def recorded_proof(state: core.CoreState, proof: core.Observation) -> core.CoreState:
    return state.model_copy(
        update={
            "intents": state.intents.model_copy(
                update={
                    "intents": tuple(
                        row.model_copy(update={"observation": proof})
                        if row.request_id == proof.request_id
                        else row
                        for row in state.intents.intents
                    ),
                }
            )
        }
    )


def checked_step(
    state: core.CoreState,
    event: core.CoreEvent,
    *,
    codec: core.OperationRegistry | None = None,
) -> core.Transition:
    before = state.model_dump_json()
    result = core.step(state, event, reducers=REDUCERS)
    assert core.step(reload_state(state, codec), event, reducers=REDUCERS) == result
    assert state.model_dump_json() == before
    assert reload_state(result.state, codec) == result.state
    assert core.project(result.state) == core.project(reload_state(result.state, codec))
    return result


def fixture() -> tuple[core.CoreState, core.AttemptRef]:
    state = core.initial_state()
    ref = core.AttemptRef(attempt_id=core.AttemptId(root="attempt"), generation=0)
    owner = core.AttemptView(
        attempt_id=ref.attempt_id,
        item_id=core.ItemId(root="item"),
        generation=ref.generation,
        phase=core.AttemptPhase.ACTIVE,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
        admission_id=core.DecisionId(root="admission"),
        charges=(
            core.ChargeReceipt(
                charge_id=core.ChargeId(root="admission-charge"),
                kind=core.ChargeKind.ADMISSION,
                charged=1,
            ),
        ),
    )
    admission = core.StartAttempt(
        decision_id=core.DecisionId(root="admission"),
        scope=core.Scope(owner=state.run.run_id, generation=ref.generation),
        attempt_id=ref.attempt_id,
        item_id=owner.item_id,
        workspace=owner.workspace,
        budget=owner.budget,
    )
    receipt = core.DecisionReceipt(
        decision_id=admission.decision_id,
        decision=admission,
        payload_digest="accepted-admission",
        feedback=core.Accepted(decision_id=admission.decision_id),
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"receipts": (receipt,)}),
            "attempts": core.AttemptsState(attempts=(owner,)),
            "scheduling": core.SchedulingState(
                slots=(core.Slot(attempt=ref, admission_id=owner.admission_id, admitted_at=0.0),)
            ),
        }
    )
    return state, ref


def retirement(
    ref: core.AttemptRef, disposition: Literal["park", "cancel", "settle"] = "cancel"
) -> core.RetireRequested:
    return core.RetireRequested(
        attempt=ref,
        disposition=disposition,
        authority=core.RequestId(root="withdraw"),
        admission_id=core.DecisionId(root="admission"),
        requested_at=1.0,
    )


def observation(
    ref: core.AttemptRef, request_id: core.RequestId, **facts: object
) -> core.Observation:
    return core.Observation.model_validate(
        {
            "event_id": core.EventId(root=f"observed:{request_id.root}"),
            "request_id": request_id,
            "scope": core.Scope(owner=ref.attempt_id, generation=ref.generation),
            "sequence": 1,
            "observed_at": 2.0,
            "status": core.ObservationStatus.SUCCEEDED,
            "accepted": True,
            "terminal": True,
            "released": True,
            "children_complete": True,
            "admission_id": core.DecisionId(root="admission"),
            **facts,
        }
    )


@given(disposition=st.sampled_from(["park", "cancel"]))
def test_withdrawal_reaches_exact_missing_scheduling_boundary(
    disposition: Literal["park", "cancel"],
) -> None:
    state, ref = fixture()
    before = state.model_dump_json()
    with pytest.raises(core.KernelNotImplementedError) as boundary:
        core.step(state, retirement(ref, disposition), reducers=REDUCERS)
    assert boundary.value.subarea == "scheduling"
    assert boundary.value.event_kind == "slot_charge_ended"
    assert state.model_dump_json() == before


def closing_fixture() -> tuple[core.CoreState, core.AttemptRef]:
    """Committed intent/closure boundary supplied by the durable shell contract."""
    state, ref = fixture()
    event = retirement(ref)
    scope = core.Scope(owner=ref.attempt_id, generation=ref.generation)
    requests = (
        core.CloseAttemptScope(
            request_id=event.authority,
            scope=scope,
            attempt=ref,
            admission_id=event.admission_id,
            deadline_at=state.run.deadline_at,
        ),
        core.DiscardWorkspace(
            request_id=core.RequestId(root="withdraw:discard"),
            depends_on=(event.authority,),
            scope=scope,
            attempt=ref,
            admission_id=event.admission_id,
            deadline_at=state.run.deadline_at,
        ),
    )
    intents = tuple(
        core.Intent(
            request_id=request.request_id,
            request=request,
            payload_digest="durable",
            lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
            phase=core.IntentPhase.PREPARED,
            reconcile_deadline_at=state.run.deadline_at,
        )
        for request in requests
    )
    owner = state.attempts.attempts[0].model_copy(
        update={
            "phase": core.AttemptPhase.CLOSING,
            "closure": core.AttemptClosure(
                disposition="cancel",
                requested_at=1.0,
                authority=event.authority,
                admission_id=event.admission_id,
            ),
            "release_dependencies": (
                core.ReleaseDependency(kind="request", identity=event.authority),
                core.ReleaseDependency(kind="workspace", identity=requests[1].request_id),
            ),
        }
    )
    return state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "intents": state.intents.model_copy(update={"intents": intents}),
        }
    ), ref


@given(generations=st.lists(st.integers(min_value=1, max_value=10), max_size=12))
def test_stale_episode_retirement_cannot_change_owner_or_accounting(generations: list[int]) -> None:
    state, ref = fixture()
    for generation in generations:
        stale = retirement(ref.model_copy(update={"generation": generation}))
        result = checked_step(state, stale)
        assert result.state.attempts == state.attempts
        assert result.state.scheduling == state.scheduling
        assert result.requests == ()
        state = result.state


@given(
    dispositions=st.lists(st.sampled_from(["park", "cancel", "settle"]), min_size=1, max_size=12)
)
def test_f8_first_committed_withdrawal_keeps_its_disposition(
    dispositions: list[Literal["park", "cancel", "settle"]],
) -> None:
    state, ref = closing_fixture()
    closure = state.attempts.attempts[0].closure
    for disposition in dispositions:
        event = retirement(ref, disposition).model_copy(
            update={"authority": core.RequestId(root=f"late:{disposition}")}
        )
        result = checked_step(state, event)
        assert result.state.attempts.attempts[0].closure == closure
        assert result.state.attempts.attempts[0].charges == state.attempts.attempts[0].charges
        assert result.requests == ()
        state = result.state


@given(
    status=st.sampled_from(list(core.ObservationStatus)),
    terminal=st.booleans(),
    released=st.booleans(),
    complete=st.booleans(),
    episode=st.sampled_from([None, "admission", "stale"]),
)
def test_scope_close_guard_requires_positive_exact_complete_manifest(
    status: core.ObservationStatus,
    *,
    terminal: bool,
    released: bool,
    complete: bool,
    episode: str | None,
) -> None:
    state, ref = closing_fixture()
    dependency = core.ReleaseDependency(kind="request", identity=core.RequestId(root="withdraw"))
    proof = observation(
        ref,
        core.RequestId(root="withdraw"),
        status=status,
        terminal=terminal,
        released=released,
        children_complete=complete,
        admission_id=core.DecisionId(root=episode) if episode is not None else None,
    )
    event = core.ReleaseDependencyObserved(attempt=ref, dependency=dependency, observation=proof)
    state = recorded_proof(state, proof)
    if episode == "stale":
        with pytest.raises(core.ContractError, match=r"observation\.admission_id"):
            core.step(state, event, reducers=REDUCERS)
        return
    result = checked_step(state, event)
    positive = (
        status == core.ObservationStatus.SUCCEEDED
        and terminal
        and complete
        and episode == "admission"
    )
    if not positive:
        assert dependency in result.state.attempts.attempts[0].release_dependencies
        assert result.state.scheduling.slots == state.scheduling.slots
        assert result.state.settlement.settlements == ()
    else:
        assert dependency not in result.state.attempts.attempts[0].release_dependencies


@given(
    status=st.sampled_from(list(core.ObservationStatus)),
    terminal=st.booleans(),
    released=st.booleans(),
    accepted=st.booleans(),
    corruption=st.sampled_from(["none", "scope", "request", "episode"]),
)
def test_f4_discard_requires_exact_positive_release_and_never_refills_early(
    status: core.ObservationStatus,
    *,
    terminal: bool,
    released: bool,
    accepted: bool,
    corruption: str,
) -> None:
    state, ref = closing_fixture()
    dependency = core.ReleaseDependency(
        kind="workspace", identity=core.RequestId(root="withdraw:discard")
    )
    proof = observation(
        ref,
        dependency.identity,
        status=status,
        terminal=terminal,
        released=released,
        accepted=accepted,
    )
    if corruption == "scope":
        proof = proof.model_copy(update={"scope": proof.scope.model_copy(update={"generation": 1})})
    elif corruption == "request":
        proof = proof.model_copy(update={"request_id": core.RequestId(root="other")})
    elif corruption == "episode":
        proof = proof.model_copy(update={"admission_id": core.DecisionId(root="other")})
    event = core.ReleaseDependencyObserved(attempt=ref, dependency=dependency, observation=proof)
    state = recorded_proof(state, proof)
    if corruption in ("scope", "episode"):
        path = "scope" if corruption == "scope" else "admission_id"
        with pytest.raises(core.ContractError, match=f"observation.{path}"):
            core.step(state, event, reducers=REDUCERS)
        return
    result = checked_step(state, event)
    positive = (
        corruption == "none"
        and terminal
        and released
        and status not in (core.ObservationStatus.UNKNOWN, core.ObservationStatus.PENDING)
    )
    if not positive:
        assert dependency in result.state.attempts.attempts[0].release_dependencies
    assert result.state.scheduling == state.scheduling
    assert result.state.settlement.settlements == ()
    assert all(isinstance(request, core.InspectRequest) for request in result.requests)


@given(
    statuses=st.lists(st.sampled_from(list(core.ObservationStatus)), min_size=1, max_size=15),
)
def test_reordered_duplicate_unknown_release_does_not_release_capacity(
    statuses: list[core.ObservationStatus],
) -> None:
    state, ref = closing_fixture()
    dependency = core.ReleaseDependency(
        kind="workspace", identity=core.RequestId(root="withdraw:discard")
    )
    charges = state.attempts.attempts[0].charges
    for index, status in enumerate(statuses):
        proof = observation(
            ref, dependency.identity, status=status, sequence=index % 3, released=False
        )
        state = recorded_proof(state, proof)
        result = checked_step(
            state,
            core.ReleaseDependencyObserved(attempt=ref, dependency=dependency, observation=proof),
        )
        assert all(isinstance(request, core.InspectRequest) for request in result.requests)
        assert result.state.attempts.attempts[0].charges == charges
        assert dependency in result.state.attempts.attempts[0].release_dependencies
        assert len(result.state.scheduling.slots) == 1
        assert result.state.settlement.settlements == ()
        state = result.state


@given(released=st.booleans(), matching_scope=st.booleans())
def test_late_children_join_release_graph_before_root_manifest_can_clear(
    *,
    released: bool,
    matching_scope: bool,
) -> None:
    state, ref = closing_fixture()
    scope = core.Scope(owner=ref.attempt_id, generation=0 if matching_scope else 1)
    resource = core.ResourceId(root="late-child")
    proof = observation(ref, core.RequestId(root="withdraw"), resource_id=resource)
    child = core.ChildLease(
        resource_id=resource,
        scope=scope,
        source_requests=(core.RequestId(root="withdraw"),),
        observation=proof if released and matching_scope else None,
    )
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"children": (child,)})}
    )
    root_edge = core.ReleaseDependency(kind="request", identity=core.RequestId(root="withdraw"))
    root_proof = observation(ref, core.RequestId(root="withdraw"))
    state = recorded_proof(state, root_proof)
    result = checked_step(
        state,
        core.ReleaseDependencyObserved(
            attempt=ref,
            dependency=root_edge,
            observation=root_proof,
        ),
    )
    child_edge = core.ReleaseDependency(kind="job", identity=resource)
    if matching_scope and not released:
        assert child_edge in result.state.attempts.attempts[0].release_dependencies
    assert result.state.scheduling == state.scheduling
    assert result.state.settlement.settlements == ()


@given(admission=st.sampled_from([None, "admission", "stale"]))
def test_withdrawal_admission_guard_cannot_close_an_unrelated_episode(
    admission: str | None,
) -> None:
    state, ref = fixture()
    owner = state.attempts.attempts[0].model_copy(
        update={
            "admission_id": core.DecisionId(root=admission) if admission is not None else None,
        }
    )
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    if admission == "admission":
        with pytest.raises(core.KernelNotImplementedError) as boundary:
            core.step(state, retirement(ref), reducers=REDUCERS)
        assert boundary.value.event_kind == "slot_charge_ended"
    else:
        result = checked_step(state, retirement(ref))
        assert result.state.attempts == state.attempts
        assert result.requests == ()


@given(disposition=st.sampled_from(["park", "cancel"]))
def test_f8_settlement_first_rejects_withdrawal(disposition: Literal["park", "cancel"]) -> None:
    state, ref = fixture()
    settlement = core.Settlement(
        settlement_id=core.SettlementId(root="settled"),
        attempt=ref,
        candidate=None,
        assessments=(),
        eligible=False,
        retention="discard",
        outcome="failed",
    )
    state = state.model_copy(update={"settlement": core.SettlementState(settlements=(settlement,))})
    result = checked_step(state, retirement(ref, disposition))
    assert result.state.attempts == state.attempts
    assert result.state.settlement.settlements == (settlement,)
    assert result.requests == ()


def pending_settlement(state: core.CoreState, ref: core.AttemptRef) -> core.Settlement:
    return core.Settlement(
        settlement_id=core.SettlementId(root="pending"),
        attempt=ref,
        candidate=state.run.facts.baseline,
        assessments=(),
        eligible=False,
        retention="wip",
        outcome="failed",
    )


@given(disposition=st.sampled_from(["park", "cancel"]))
def test_f8_pending_settlement_already_fences_withdrawal(
    disposition: Literal["park", "cancel"],
) -> None:
    state, ref = fixture()
    pending = pending_settlement(state, ref)
    state = state.model_copy(update={"settlement": core.SettlementState(pending=(pending,))})
    result = checked_step(state, retirement(ref, disposition))
    assert result.state.attempts == state.attempts
    assert result.state.settlement.pending == (pending,)
    assert result.requests == ()


@given(retention=st.sampled_from(["discard", "wip", "candidate"]), revision=st.booleans())
def test_retention_signal_without_authoritative_settlement_never_creates_disposal(
    retention: Literal["discard", "wip", "candidate"],
    *,
    revision: bool,
) -> None:
    state, ref = fixture()
    result = checked_step(
        state,
        core.RetentionRequired(
            attempt=ref,
            retention=retention,
            revision=state.run.facts.baseline if revision else None,
        ),
    )
    assert result.requests == ()
    assert result.state.attempts == state.attempts


@given(retention=st.sampled_from(["discard", "wip", "candidate"]), revision=st.booleans())
def test_settlement_retention_handshake_requires_exact_pending_disposition(
    retention: Literal["discard", "wip", "candidate"],
    *,
    revision: bool,
) -> None:
    state, ref = fixture()
    pending = pending_settlement(state, ref)
    state = state.model_copy(update={"settlement": core.SettlementState(pending=(pending,))})
    event = core.RetentionRequired(
        attempt=ref,
        retention=retention,
        revision=state.run.facts.baseline if revision else None,
    )
    if retention == pending.retention and revision:
        with pytest.raises(core.KernelNotImplementedError) as boundary:
            core.step(state, event, reducers=REDUCERS)
        assert boundary.value.subarea == "scheduling"
        assert boundary.value.event_kind == "slot_charge_ended"
    else:
        result = checked_step(state, event)
        assert result.requests == ()
        assert result.state.attempts == state.attempts


@given(disposition=st.sampled_from(["park", "cancel", "settle"]))
def test_writer_edge_prevents_retention_and_discard_until_positive_drain(
    disposition: Literal["park", "cancel", "settle"],
) -> None:
    state, ref = closing_fixture()
    owner = state.attempts.attempts[0]
    assert owner.closure is not None
    writer = core.ReleaseDependency(kind="session", identity=core.SessionId(root="writer"))
    owner = owner.model_copy(
        update={
            "closure": owner.closure.model_copy(update={"disposition": disposition}),
            "release_dependencies": (owner.release_dependencies[0], writer),
        }
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "intents": state.intents.model_copy(update={"intents": state.intents.intents[:1]}),
            "settlement": core.SettlementState(pending=(pending_settlement(state, ref),))
            if disposition == "settle"
            else state.settlement,
        }
    )
    result = checked_step(
        state,
        core.RetentionRequired(
            attempt=ref,
            retention="wip",
            revision=state.run.facts.baseline,
        ),
    )
    assert result.requests == ()
    assert writer in result.state.attempts.attempts[0].release_dependencies
    assert result.state.scheduling == state.scheduling


def writer_invocation(
    ref: core.AttemptRef,
    charge: Literal["paid", "correction", "resume", "free"] = "paid",
    *,
    predecessor: bool = False,
) -> core.Invocation:
    scope = core.Scope(owner=ref.attempt_id, generation=ref.generation)
    session = core.SessionSpec(
        session_id=core.SessionId(root="writer"),
        role_id=core.RoleId(root="implementer"),
        policy="fresh",
        lifetime="ephemeral",
        access=core.Access.WRITE_CANDIDATE,
    )
    invocation = core.InvocationRef(
        session_id=session.session_id,
        invocation_id=core.InvocationId(root="writer-turn"),
        generation=0,
    )
    turn = core.TurnSpec(
        session=session,
        invocation_id=invocation.invocation_id,
        continuation_id=core.ContinuationId(root="writer-continuation")
        if charge == "resume"
        else None,
        workspace=scope,
        prompts=(),
        output_schema=core.SchemaRef(name="implementation", version=1),
        deadline_at=100.0,
        charge_class=charge,
        predecessor=invocation.model_copy(
            update={"invocation_id": core.InvocationId(root="predecessor")}
        )
        if predecessor
        else None,
    )
    return core.Invocation(
        invocation=invocation,
        scope=scope,
        turn=turn,
        phase=core.SessionPhase.TERMINAL,
    )


@pytest.mark.parametrize("charge", ["paid", "correction", "resume", "free"])
@pytest.mark.parametrize("predecessor", [False, True])
@given(
    proof=st.tuples(
        st.sampled_from(list(core.ObservationStatus)),
        st.booleans(),
        st.booleans(),
        st.booleans(),
        st.sampled_from([None, "admission", "stale"]),
    )
)
@example(proof=(core.ObservationStatus.SUCCEEDED, True, True, True, "admission"))
def test_every_writer_variant_requires_exact_complete_lease_release_before_disposal(
    charge: Literal["paid", "correction", "resume", "free"],
    proof: tuple[core.ObservationStatus, bool, bool, bool, str | None],
    *,
    predecessor: bool,
) -> None:
    status, terminal, released, complete, episode = proof
    state, ref = closing_fixture()
    owner = state.attempts.attempts[0].model_copy(
        update={"release_dependencies": state.attempts.attempts[0].release_dependencies[:1]}
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "intents": state.intents.model_copy(update={"intents": state.intents.intents[:1]}),
        }
    )
    state = recorded_proof(state, observation(ref, core.RequestId(root="withdraw")))
    invocation = writer_invocation(ref, charge, predecessor=predecessor)
    writer_proof = observation(
        ref,
        core.RequestId(root="writer-dispatch"),
        resource_id=core.ResourceId(root="writer-turn-resource"),
        status=status,
        terminal=terminal,
        released=released,
        children_complete=complete,
        admission_id=core.DecisionId(root=episode) if episode is not None else None,
    )
    invocation = invocation.model_copy(update={"observation": writer_proof})
    dispatch_fields = {
        "request_id": writer_proof.request_id,
        "scope": invocation.scope,
        "admission_id": core.DecisionId(root="admission"),
        "deadline_at": invocation.turn.deadline_at,
        "turn": invocation.turn,
    }
    request = (
        core.ResumeSessionTurn.model_validate(
            {
                **dispatch_fields,
                "continuation_id": core.ContinuationId(root="writer-continuation"),
            }
        )
        if charge == "resume"
        else core.DispatchTurn.model_validate(dispatch_fields)
    )
    intent = core.Intent(
        request_id=writer_proof.request_id,
        request=request,
        payload_digest="writer-dispatch",
        lifecycle=core.LifecycleClass.SESSION_TURN,
        phase=core.IntentPhase.COMPLETED,
        observation=writer_proof,
        reconcile_deadline_at=state.run.deadline_at,
    )
    lease_id = core.RequestId(root="writer-lease")
    lease = core.Intent(
        request_id=lease_id,
        request=core.CloseSession(
            request_id=lease_id,
            scope=invocation.scope,
            session_id=invocation.invocation.session_id,
            admission_id=core.DecisionId(root="admission"),
            deadline_at=state.run.deadline_at,
        ),
        payload_digest="writer-lease-release",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        phase=core.IntentPhase.COMPLETED,
        observation=observation(
            ref, lease_id, resource_id=core.ResourceId(root="writer-conversation")
        ),
        reconcile_deadline_at=state.run.deadline_at,
    )
    state = state.model_copy(
        update={
            "sessions": core.SessionsState(
                invocations=(invocation,),
                sessions=(
                    core.SessionView(
                        spec=invocation.turn.session,
                        scope=invocation.scope,
                        generation=invocation.invocation.generation,
                        phase=core.SessionPhase.TERMINAL,
                        accepted=True,
                        resource_id=core.ResourceId(root="writer-conversation"),
                    ),
                ),
            ),
            "intents": state.intents.model_copy(
                update={"intents": (*state.intents.intents, intent, lease)}
            ),
        }
    )
    result = checked_step(
        state,
        core.ReleaseDependencyObserved(
            attempt=ref,
            dependency=owner.release_dependencies[0],
            observation=observation(ref, core.RequestId(root="withdraw")),
        ),
    )
    positive = (
        status not in (core.ObservationStatus.UNKNOWN, core.ObservationStatus.PENDING)
        and terminal
        and released
        and complete
        and episode == "admission"
    )
    if positive:
        assert len(result.requests) == 1
        assert isinstance(result.requests[0], core.DiscardWorkspace)
    else:
        assert result.requests == ()
    assert result.state.attempts.attempts[0].charges == owner.charges
    assert result.state.scheduling == state.scheduling


@given(
    status=st.sampled_from(list(core.ObservationStatus)),
    terminal=st.booleans(),
    complete=st.booleans(),
    canonical=st.booleans(),
)
@example(status=core.ObservationStatus.SUCCEEDED, terminal=True, complete=True, canonical=True)
def test_empty_writer_set_never_authorizes_disposal_before_positive_scope_fence(
    status: core.ObservationStatus,
    *,
    terminal: bool,
    complete: bool,
    canonical: bool,
) -> None:
    state, ref = closing_fixture()
    owner = state.attempts.attempts[0].model_copy(
        update={"release_dependencies": state.attempts.attempts[0].release_dependencies[:1]}
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "intents": state.intents.model_copy(update={"intents": state.intents.intents[:1]}),
        }
    )
    proof = observation(
        ref,
        core.RequestId(root="withdraw"),
        status=status,
        terminal=terminal,
        children_complete=complete,
    )
    if canonical:
        state = recorded_proof(state, proof)
    result = checked_step(
        state,
        core.ReleaseDependencyObserved(
            attempt=ref,
            dependency=owner.release_dependencies[0],
            observation=proof,
        ),
    )
    if canonical and status == core.ObservationStatus.SUCCEEDED and terminal and complete:
        assert len(result.requests) == 1
        assert isinstance(result.requests[0], core.DiscardWorkspace)
        assert core.pending_requests(result.state.intents)[-1] == result.requests[0]
    else:
        assert all(isinstance(request, core.InspectRequest) for request in result.requests)
    assert result.state.scheduling == state.scheduling


def normalize_reopen(request: core.OperationRequest) -> core.ScopeReopenNormalization:
    assert isinstance(request, core.ScopedAdmissionReopen)
    return core.ScopeReopenNormalization(
        attempt=request.attempt,
        continuation_id=request.continuation_id,
        park_authority=request.park_authority,
        resolved_cancelled_jobs=request.resolved_cancelled_jobs,
    )


def reopen_fixture() -> tuple[core.CoreState, core.ScopeReopenRequested, core.OperationRegistry]:
    state, ref = closing_fixture()
    descriptor = core.OperationDescriptor(
        kind="evaluation.scope.reopen",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        request_schema=core.SchemaRef(name="scope-reopen", version=1),
        outcome_schema=core.SchemaRef(name="scope-reopened", version=1),
        inspect=True,
        normalization=core.OperationNormalizationKind.SCOPE_REOPEN,
    )
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor,
                request_model=core.ScopedAdmissionReopen,
                outcome_model=core.ScopedAdmissionReopenOutcome,
                normalize_scope_reopen=normalize_reopen,
            ),
        )
    )
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="reopen"),
            scope=core.Scope(owner=state.run.run_id, generation=0),
            deadline_at=100.0,
            request=core.ScopedAdmissionReopen(
                attempt=ref,
                continuation_id=core.ContinuationId(root="continuation"),
                park_authority=core.RequestId(root="withdraw"),
                resolved_cancelled_jobs=(),
            ),
        )
    )
    norm = decision.normalized_scope_reopen
    assert norm is not None
    request = core.ExecuteRegisteredOperation(
        request_id=core.RequestId(root="operation:reopen"),
        decision_id=decision.decision_id,
        scope=decision.scope,
        deadline_at=decision.deadline_at,
        operation_id=core.OperationId(root="operation:reopen"),
        operation=codec.encode(decision.request),
        retry_limit=state.run.limits.max_retries,
    )
    invocation = core.InvocationRef(
        session_id=core.SessionId(root="session"),
        invocation_id=core.InvocationId(root="yield"),
        generation=0,
    )
    continuation = core.Continuation(
        continuation_id=norm.continuation_id,
        invocation=invocation,
        next_invocation=invocation.model_copy(
            update={"invocation_id": core.InvocationId(root="resume")}
        ),
        jobs=(),
        deadline_at=100.0,
        phase=core.ContinuationPhase.REOPENING,
        park_authority=norm.park_authority,
        reopen_authority=request.request_id,
    )
    owner = state.attempts.attempts[0]
    assert owner.closure is not None
    owner = owner.model_copy(
        update={
            "phase": core.AttemptPhase.PARKED,
            "release_dependencies": (),
            "closure": owner.closure.model_copy(update={"disposition": "park"}),
            "checkpoints": (
                core.AttemptCheckpoint(
                    invocation=None,
                    request_id=core.RequestId(root="withdraw:retention"),
                    revision=state.run.facts.baseline,
                    retention="wip",
                ),
            ),
        }
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest="canonical",
        feedback=core.Accepted(decision_id=decision.decision_id),
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
            "attempts": core.AttemptsState(attempts=(owner,)),
            "evaluation": core.EvaluationState(continuations=(continuation,)),
            "sessions": core.SessionsState(
                invocations=(
                    writer_invocation(ref).model_copy(
                        update={
                            "invocation": invocation,
                        }
                    ),
                )
            ),
            "intents": state.intents.model_copy(update={"intents": ()}),
        }
    )
    return state, core.ScopeReopenRequested(request=request, normalization=norm), codec


@pytest.mark.parametrize("causal_decision", [False, True])
@given(
    guard=st.tuples(
        st.sampled_from(list(core.ContinuationPhase)),
        st.sampled_from(["accepted", "rejected", "wrong-id"]),
        st.booleans(),
        st.booleans(),
        st.booleans(),
        st.sampled_from(["exact", "extra", "missing"]),
    )
)
@example(guard=(core.ContinuationPhase.REOPENING, "accepted", True, True, True, "exact"))
def test_reopen_admission_guard_requires_exact_positive_authority(
    guard: tuple[core.ContinuationPhase, str, bool, bool, bool, str],
    *,
    causal_decision: bool,
) -> None:
    phase, feedback, parked, checkpoint, matching_park, resolutions = guard
    state, event, codec = reopen_fixture()
    if not causal_decision:
        event = event.model_copy(
            update={"request": event.request.model_copy(update={"decision_id": None})}
        )
    owner = state.attempts.attempts[0].model_copy(
        update={
            "phase": core.AttemptPhase.PARKED if parked else core.AttemptPhase.TERMINAL,
            "checkpoints": state.attempts.attempts[0].checkpoints if checkpoint else (),
        }
    )
    continuation = state.evaluation.continuations[0].model_copy(
        update={
            "phase": phase,
            "park_authority": event.normalization.park_authority if matching_park else None,
            "cancelled_resolutions": (core.ResourceId(root="unresolved"),)
            if resolutions == "missing"
            else (),
        }
    )
    if resolutions == "extra":
        event = event.model_copy(
            update={
                "normalization": event.normalization.model_copy(
                    update={
                        "resolved_cancelled_jobs": (core.ResourceId(root="extra"),),
                    }
                )
            }
        )
    receipt = state.run.receipts[0]
    if feedback == "rejected":
        receipt = receipt.model_copy(
            update={
                "feedback": core.Rejected(
                    decision_id=receipt.decision_id,
                    code=core.RejectionCode.OWNERSHIP,
                    path=("scope",),
                    detail="rejected",
                )
            }
        )
    elif feedback == "wrong-id":
        receipt = receipt.model_copy(
            update={"feedback": core.Accepted(decision_id=core.DecisionId(root="wrong"))}
        )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "evaluation": core.EvaluationState(continuations=(continuation,)),
            "run": state.run.model_copy(update={"receipts": (receipt,)}),
        }
    )
    positive = (
        parked
        and checkpoint
        and matching_park
        and phase == core.ContinuationPhase.REOPENING
        and feedback == "accepted"
        and resolutions == "exact"
    )
    if positive:
        with pytest.raises(core.KernelNotImplementedError) as boundary:
            core.step(state, event, reducers=REDUCERS)
        assert boundary.value.subarea == "scheduling"
        assert boundary.value.event_kind == "attempt_reopen_requested"
    else:
        result = checked_step(state, event, codec=codec)
        assert result.requests == ()
        assert result.state.attempts == state.attempts
        assert result.state.attempts.attempts[0].charges == owner.charges


@given(
    checkpoint=st.sampled_from(
        ["exact", "absent", "wrong-request", "wrong-revision", "wrong-retention"]
    ),
    status=st.sampled_from(list(core.ObservationStatus)),
    terminal=st.booleans(),
)
@example(checkpoint="exact", status=core.ObservationStatus.SUCCEEDED, terminal=True)
def test_retention_crash_ack_requires_exact_checkpoint_history(
    checkpoint: str,
    status: core.ObservationStatus,
    *,
    terminal: bool,
) -> None:
    state, ref = closing_fixture()
    owner = state.attempts.attempts[0]
    assert owner.closure is not None
    request = core.RetainRevision(
        request_id=core.RequestId(root="withdraw:retention"),
        depends_on=(owner.closure.authority,),
        scope=core.Scope(owner=ref.attempt_id, generation=ref.generation),
        admission_id=owner.admission_id,
        deadline_at=state.run.deadline_at,
        attempt=ref,
        revision=state.run.facts.baseline,
        retention="wip",
    )
    retained = core.AttemptCheckpoint(
        invocation=None,
        request_id=request.request_id
        if checkpoint != "wrong-request"
        else core.RequestId(root="other"),
        revision=request.revision
        if checkpoint != "wrong-revision"
        else request.revision.model_copy(update={"digest": "other"}),
        retention="candidate" if checkpoint == "wrong-retention" else "wip",
    )
    edge = core.ReleaseDependency(kind="workspace", identity=request.request_id)
    owner = owner.model_copy(
        update={
            "closure": owner.closure.model_copy(update={"disposition": "settle"}),
            "release_dependencies": (owner.release_dependencies[0], edge),
            "checkpoints": () if checkpoint == "absent" else (retained,),
        }
    )
    intent = state.intents.intents[1].model_copy(
        update={
            "request_id": request.request_id,
            "request": request,
        }
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "settlement": core.SettlementState(pending=(pending_settlement(state, ref),)),
            "intents": state.intents.model_copy(
                update={"intents": (state.intents.intents[0], intent)}
            ),
        }
    )
    proof = observation(ref, request.request_id, status=status, terminal=terminal)
    state = recorded_proof(state, proof)
    result = checked_step(
        state,
        core.ReleaseDependencyObserved(
            attempt=ref,
            dependency=edge,
            observation=proof,
        ),
    )
    if checkpoint == "exact" and status == core.ObservationStatus.SUCCEEDED and terminal:
        assert edge not in result.state.attempts.attempts[0].release_dependencies
    else:
        assert edge in result.state.attempts.attempts[0].release_dependencies
    assert result.state.attempts.attempts[0].checkpoints == owner.checkpoints
    assert result.state.scheduling == state.scheduling
    assert result.state.settlement.settlements == ()


def test_final_disposal_requires_scope_manifest_before_settlement_boundary() -> None:
    state, ref = closing_fixture()
    owner = state.attempts.attempts[0].model_copy(
        update={
            "release_dependencies": state.attempts.attempts[0].release_dependencies[1:],
        }
    )
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    scope_proof = observation(ref, core.RequestId(root="withdraw"))
    state = recorded_proof(state, scope_proof)
    discard_proof = observation(ref, core.RequestId(root="withdraw:discard"))
    state = recorded_proof(state, discard_proof)
    event = core.ReleaseDependencyObserved(
        attempt=ref,
        dependency=owner.release_dependencies[0],
        observation=discard_proof,
    )
    with pytest.raises(core.KernelNotImplementedError) as boundary:
        core.step(state, event, reducers=REDUCERS)
    assert boundary.value.subarea == "scheduling"
    assert boundary.value.event_kind == "slot_released"
