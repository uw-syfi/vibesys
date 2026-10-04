"""Attempt retirement proofs survive duplicates, stale events and crash reloads."""

from typing import ClassVar, Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

import vs_core.api as core


def changed[T: BaseModel](value: T, **fields: object) -> T:
    return value.model_copy(update=fields)


def reload_state(
    state: core.CoreState, codec: core.OperationRegistry | None = None
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
    if isinstance(event, core.ContinuationScopeReopened):
        assert any(
            row.phase == core.AttemptPhase.ACTIVE and row.closure is None
            for row in context.attempts.attempts
        )
        return core.AreaChange(state=state)
    if not isinstance(event, core.JobsDrainRequested):
        raise core.ContractValidationError("event", "unsupported Fake Evaluation event")
    if not any(
        row.closure is not None and row.closure.authority == event.authority
        for row in context.attempts.attempts
    ):
        raise core.ContractValidationError("authority", "missing committed closure")
    return core.AreaChange(state=state)


REDUCERS = core.CoreReducers(evaluation=fake_evaluation, session_inputs=fake_inputs)


def request_identity(request: core.Request) -> core.RequestId:
    assert request.request_id is not None
    return request.request_id


def proof_intent(
    request: core.Request,
    deadline: float,
    proof: core.Observation | None = None,
    *,
    lifecycle: core.LifecycleClass = core.LifecycleClass.IDEMPOTENT_WRITE,
) -> core.Intent:
    return core.Intent(
        request_id=request_identity(request),
        request=request,
        payload_digest="durable",
        lifecycle=lifecycle,
        phase=core.IntentPhase.PREPARED if proof is None else core.IntentPhase.COMPLETED,
        observation=proof,
        reconcile_deadline_at=deadline,
    )


def with_owner(state: core.CoreState, owner: core.AttemptView, **areas: object) -> core.CoreState:
    return state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,)), **areas})


def recorded_proof(state: core.CoreState, proof: core.Observation) -> core.CoreState:
    return changed(
        state,
        intents=changed(
            state.intents,
            intents=tuple(
                changed(row, observation=proof) if row.request_id == proof.request_id else row
                for row in state.intents.intents
            ),
        ),
    )


def checked_step(
    state: core.CoreState, event: core.CoreEvent, *, codec: core.OperationRegistry | None = None
) -> core.Transition:
    before = state.model_dump_json()
    result = core.step(state, event, reducers=REDUCERS)
    assert core.step(reload_state(state, codec), event, reducers=REDUCERS) == result
    assert state.model_dump_json() == before
    assert result.state.scheduling == state.scheduling
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
    assert owner.admission_id is not None
    state = with_owner(
        state,
        owner,
        run=changed(state.run, receipts=(receipt,)),
        scheduling=core.SchedulingState(
            slots=(core.Slot(attempt=ref, admission_id=owner.admission_id, admitted_at=0.0),)
        ),
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
    fields: dict[str, object] = {
        "scope": scope,
        "attempt": ref,
        "admission_id": event.admission_id,
        "deadline_at": state.run.deadline_at,
    }
    requests = (
        core.CloseAttemptScope.model_validate({"request_id": event.authority, **fields}),
        core.DiscardWorkspace.model_validate(
            {
                "request_id": core.RequestId(root="withdraw:discard"),
                "depends_on": (event.authority,),
                **fields,
            }
        ),
    )
    intents = tuple(proof_intent(request, state.run.deadline_at) for request in requests)
    owner = changed(
        state.attempts.attempts[0],
        phase=core.AttemptPhase.CLOSING,
        closure=core.AttemptClosure(
            disposition="cancel",
            requested_at=1.0,
            authority=event.authority,
            admission_id=event.admission_id,
        ),
        release_dependencies=(
            core.ReleaseDependency(kind="request", identity=event.authority),
            core.ReleaseDependency(kind="workspace", identity=request_identity(requests[1])),
        ),
    )
    return with_owner(state, owner, intents=changed(state.intents, intents=intents)), ref


@given(generations=st.lists(st.integers(min_value=1, max_value=10), max_size=12))
def test_stale_episode_retirement_cannot_change_owner_or_accounting(generations: list[int]) -> None:
    state, ref = fixture()
    for generation in generations:
        stale = retirement(changed(ref, generation=generation))
        result = checked_step(state, stale)
        assert result.state.attempts == state.attempts
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
        event = changed(
            retirement(ref, disposition), authority=core.RequestId(root=f"late:{disposition}")
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
    assert isinstance(dependency.identity, core.RequestId)
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


@pytest.mark.parametrize("queued", [False, True])
@given(
    admission=st.sampled_from([None, "admission", "stale"]),
    authority=st.sampled_from(["accepted", "absent", "rejected", "feedback-id", "decision-id"]),
)
@example(admission="admission", authority="accepted")
@example(admission=None, authority="accepted")
def test_withdrawal_requires_exact_accepted_receipt_for_every_admission_variant(
    admission: str | None, authority: str, *, queued: bool
) -> None:
    state, ref = fixture()
    owner = changed(
        state.attempts.attempts[0],
        phase=core.AttemptPhase.QUEUED if queued else core.AttemptPhase.ACTIVE,
        admission_id=None if admission is None else core.DecisionId(root=admission),
    )
    receipt = state.run.receipts[0]
    if authority == "rejected":
        receipt = changed(
            receipt,
            feedback=core.Rejected(
                decision_id=receipt.decision_id,
                code=core.RejectionCode.OWNERSHIP,
                path=("admission",),
                detail="declined",
            ),
        )
    elif authority == "feedback-id":
        receipt = changed(
            receipt, feedback=core.Accepted(decision_id=core.DecisionId(root="other"))
        )
    elif authority == "decision-id":
        assert receipt.decision is not None
        receipt = changed(
            receipt, decision=changed(receipt.decision, decision_id=core.DecisionId(root="other"))
        )
    state = with_owner(
        state,
        owner,
        run=changed(state.run, receipts=() if authority == "absent" else (receipt,)),
        scheduling=changed(state.scheduling, slots=()) if queued else state.scheduling,
    )
    positive = authority == "accepted" and (
        admission == "admission" or (queued and admission is None)
    )
    if positive:
        with pytest.raises(core.KernelNotImplementedError) as boundary:
            core.step(state, retirement(ref), reducers=REDUCERS)
        assert boundary.value.event_kind == (
            "queue_entry_retired" if queued else "slot_charge_ended"
        )
    else:
        result = checked_step(state, retirement(ref))
        assert result.state.attempts == state.attempts
        assert result.requests == ()


@pytest.mark.parametrize("pending", [False, True])
@given(disposition=st.sampled_from(["park", "cancel"]))
def test_f8_authoritative_settlement_fences_later_withdrawal(
    disposition: Literal["park", "cancel"], *, pending: bool
) -> None:
    state, ref = fixture()
    settlement = pending_settlement(state, ref)
    state = changed(
        state,
        settlement=core.SettlementState.model_validate(
            {"pending" if pending else "settlements": (settlement,)}
        ),
    )
    result = checked_step(state, retirement(ref, disposition))
    assert result.state.attempts == state.attempts
    assert result.state.settlement == state.settlement
    assert result.requests == ()


@given(retention=st.sampled_from(["discard", "wip", "candidate"]), revision=st.booleans())
def test_retention_signal_without_authoritative_settlement_never_creates_disposal(
    retention: Literal["discard", "wip", "candidate"], *, revision: bool
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
    retention: Literal["discard", "wip", "candidate"], *, revision: bool
) -> None:
    state, ref = fixture()
    pending = pending_settlement(state, ref)
    state = changed(state, settlement=core.SettlementState(pending=(pending,)))
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


class RegisteredWriter(core.OperationRequest):
    kind: Literal["tests.writer.turn"] = "tests.writer.turn"
    lifecycle: Literal[core.LifecycleClass.SESSION_TURN] = core.LifecycleClass.SESSION_TURN
    outcome_model: ClassVar[type[core.Observation]] = core.Observation
    turn: core.TurnSpec


def normalize_writer(request: core.OperationRequest) -> core.TurnSpec:
    assert isinstance(request, RegisteredWriter)
    return request.turn


def registered_writer(
    state: core.CoreState, invocation: core.Invocation, guard: str
) -> tuple[
    core.CoreState, core.Invocation, core.ExecuteRegisteredOperation, core.OperationRegistry
]:
    descriptor = core.OperationDescriptor(
        kind="tests.writer.turn",
        lifecycle=core.LifecycleClass.SESSION_TURN,
        request_schema=core.SchemaRef(name="writer", version=1),
        outcome_schema=core.SchemaRef(name="writer-result", version=1),
        inspect=True,
    )
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor,
                request_model=RegisteredWriter,
                outcome_model=core.Observation,
                normalize_turn=normalize_writer,
            ),
        )
    )
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="writer"),
            scope=invocation.scope,
            deadline_at=invocation.turn.deadline_at,
            request=RegisteredWriter(turn=invocation.turn),
        )
    )
    request = core.ExecuteRegisteredOperation(
        request_id=core.RequestId(root="operation:writer"),
        decision_id=decision.decision_id,
        operation_id=core.OperationId(root="operation:writer"),
        scope=invocation.scope,
        admission_id=core.DecisionId(root="admission"),
        deadline_at=invocation.turn.deadline_at,
        operation=codec.encode(decision.request),
        retry_limit=state.run.limits.max_retries,
    )
    feedback: core.Accepted | core.Rejected = core.Accepted(
        decision_id=decision.decision_id if guard != "wrong-id" else core.DecisionId(root="other")
    )
    if guard == "rejected":
        feedback = core.Rejected(
            decision_id=decision.decision_id,
            code=core.RejectionCode.OWNERSHIP,
            path=("writer",),
            detail="rejected",
        )
    if guard == "mutated":
        decision = changed(
            decision,
            normalized_turn=changed(invocation.turn, deadline_at=invocation.turn.deadline_at - 1),
        )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest="writer",
        feedback=feedback,
    )
    state = changed(
        state,
        registry=codec.descriptors,
        run=changed(
            state.run,
            capabilities=core.Capabilities(operations=codec.descriptors),
            receipts=(*state.run.receipts, receipt),
        ),
    )
    return (
        state,
        changed(invocation, registered_operation=request.operation_id),
        request,
        codec,
    )


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
        predecessor=changed(invocation, invocation_id=core.InvocationId(root="predecessor"))
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
@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("registered", [False, True])
@given(
    proof=st.tuples(
        st.sampled_from(list(core.ObservationStatus)),
        st.booleans(),
        st.booleans(),
        st.booleans(),
        st.sampled_from([None, "admission", "stale"]),
        st.sampled_from(["accepted", "rejected", "wrong-id", "mutated"]),
    )
)
@example(proof=(core.ObservationStatus.SUCCEEDED, True, True, True, "admission", "accepted"))
def test_every_writer_variant_requires_exact_complete_lease_release_before_disposal(
    charge: Literal["paid", "correction", "resume", "free"],
    proof: tuple[core.ObservationStatus, bool, bool, bool, str | None, str],
    *,
    predecessor: bool,
    reuse: bool,
    registered: bool,
) -> None:
    status, terminal, released, complete, episode, guard = proof
    state, ref = closing_fixture()
    owner = changed(
        state.attempts.attempts[0],
        release_dependencies=state.attempts.attempts[0].release_dependencies[:1],
    )
    state = with_owner(
        state,
        owner,
        intents=changed(state.intents, intents=state.intents.intents[:1]),
    )
    state = recorded_proof(state, observation(ref, core.RequestId(root="withdraw")))
    invocation = writer_invocation(ref, charge, predecessor=predecessor)
    if reuse:
        invocation = changed(
            invocation,
            turn=changed(
                invocation.turn,
                session=changed(invocation.turn.session, policy="reuse", lifetime="owner"),
            ),
        )
        owner = changed(owner, sessions=(invocation.invocation.session_id,))
        state = with_owner(state, owner)
    writer_proof = observation(
        ref,
        core.RequestId(root="operation:writer" if registered else "writer-dispatch"),
        scope=invocation.scope,
        resource_id=core.ResourceId(root="writer-turn-resource"),
        status=status,
        terminal=terminal,
        released=released,
        children_complete=complete,
        admission_id=core.DecisionId(root=episode) if episode is not None else None,
    )
    invocation = changed(invocation, observation=writer_proof)
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
    codec = None
    if registered:
        state, invocation, request, codec = registered_writer(state, invocation, guard)
    intent = proof_intent(
        request, state.run.deadline_at, writer_proof, lifecycle=core.LifecycleClass.SESSION_TURN
    )
    lease_id = core.RequestId(root="writer-lease")
    lease = proof_intent(
        core.CloseSession(
            request_id=lease_id,
            scope=invocation.scope,
            session_id=invocation.invocation.session_id,
            admission_id=core.DecisionId(root="admission"),
            deadline_at=state.run.deadline_at,
        ),
        state.run.deadline_at,
        observation(ref, lease_id, resource_id=core.ResourceId(root="writer-conversation")),
    )
    state = changed(
        state,
        sessions=core.SessionsState(
            invocations=(invocation,),
            sessions=(
                core.SessionView(
                    spec=invocation.turn.session,
                    scope=core.Scope(owner=state.run.run_id, generation=state.run.generation)
                    if reuse
                    else invocation.scope,
                    generation=invocation.invocation.generation,
                    phase=core.SessionPhase.TERMINAL,
                    accepted=True,
                    resource_id=core.ResourceId(root="writer-conversation"),
                ),
            ),
        ),
        intents=changed(
            state.intents,
            intents=(*state.intents.intents, intent)
            if reuse
            else (*state.intents.intents, intent, lease),
        ),
    )
    signal = core.ReleaseDependencyObserved(
        attempt=ref,
        dependency=owner.release_dependencies[0],
        observation=observation(ref, core.RequestId(root="withdraw")),
    )
    result = (
        core.step(state, signal, reducers=REDUCERS)
        if registered and guard == "mutated"
        else checked_step(state, signal, codec=codec)
    )
    if registered and guard == "mutated":
        with pytest.raises(ValidationError, match="durable normalization differs"):
            reload_state(state, codec)
    positive = (
        status not in (core.ObservationStatus.UNKNOWN, core.ObservationStatus.PENDING)
        and terminal
        and released
        and complete
        and episode == "admission"
        and (not registered or guard == "accepted")
    )
    if positive:
        assert len(result.requests) == 1
        assert isinstance(result.requests[0], core.DiscardWorkspace)
    else:
        assert result.requests == ()
    assert result.state.attempts.attempts[0].charges == owner.charges


@given(
    status=st.sampled_from(list(core.ObservationStatus)),
    terminal=st.booleans(),
    complete=st.booleans(),
    canonical=st.booleans(),
)
@example(status=core.ObservationStatus.SUCCEEDED, terminal=True, complete=True, canonical=True)
def test_empty_writer_set_never_authorizes_disposal_before_positive_scope_fence(
    status: core.ObservationStatus, *, terminal: bool, complete: bool, canonical: bool
) -> None:
    state, ref = closing_fixture()
    owner = changed(
        state.attempts.attempts[0],
        release_dependencies=state.attempts.attempts[0].release_dependencies[:1],
    )
    state = with_owner(
        state,
        owner,
        intents=changed(state.intents, intents=state.intents.intents[:1]),
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
        next_invocation=changed(invocation, invocation_id=core.InvocationId(root="resume")),
        jobs=(),
        deadline_at=100.0,
        phase=core.ContinuationPhase.REOPENING,
        park_authority=norm.park_authority,
        reopen_authority=request.request_id,
    )
    owner = state.attempts.attempts[0]
    assert owner.closure is not None
    owner = changed(
        owner,
        phase=core.AttemptPhase.PARKED,
        release_dependencies=(),
        closure=changed(owner.closure, disposition="park"),
        checkpoints=(
            core.AttemptCheckpoint(
                invocation=None,
                request_id=core.RequestId(root="withdraw:retention"),
                revision=state.run.facts.baseline,
                retention="wip",
            ),
        ),
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest="canonical",
        feedback=core.Accepted(decision_id=decision.decision_id),
    )
    state = with_owner(
        state,
        owner,
        registry=codec.descriptors,
        run=changed(
            state.run,
            capabilities=core.Capabilities(operations=codec.descriptors),
            receipts=(receipt,),
        ),
        evaluation=core.EvaluationState(continuations=(continuation,)),
        sessions=core.SessionsState(
            invocations=(changed(writer_invocation(ref), invocation=invocation),)
        ),
        intents=changed(state.intents, intents=()),
    )
    return state, core.ScopeReopenRequested(request=request, normalization=norm), codec


@pytest.mark.parametrize("causal_decision", [False, True])
@given(
    guard=st.tuples(
        st.sampled_from(list(core.ContinuationPhase)),
        st.sampled_from(
            [
                "accepted",
                "rejected",
                "wrong-id",
                "mutated-normalization",
                "failed-completion",
                "cancelled-completion",
            ]
        ),
        st.booleans(),
        st.booleans(),
        st.booleans(),
        st.sampled_from(["exact", "extra", "missing"]),
    )
)
@example(guard=(core.ContinuationPhase.REOPENING, "accepted", True, True, True, "exact"))
def test_reopen_admission_guard_requires_exact_positive_authority(
    guard: tuple[core.ContinuationPhase, str, bool, bool, bool, str], *, causal_decision: bool
) -> None:
    phase, feedback, parked, checkpoint, matching_park, resolutions = guard
    state, event, codec = reopen_fixture()
    if not causal_decision:
        event = changed(event, request=changed(event.request, decision_id=None))
    owner = changed(
        state.attempts.attempts[0],
        phase=core.AttemptPhase.PARKED if parked else core.AttemptPhase.TERMINAL,
        checkpoints=state.attempts.attempts[0].checkpoints if checkpoint else (),
    )
    continuation = changed(
        state.evaluation.continuations[0],
        phase=phase,
        park_authority=event.normalization.park_authority if matching_park else None,
        cancelled_resolutions=(core.ResourceId(root="unresolved"),)
        if resolutions == "missing"
        else (),
    )
    if resolutions == "extra":
        event = changed(
            event,
            normalization=changed(
                event.normalization, resolved_cancelled_jobs=(core.ResourceId(root="extra"),)
            ),
        )
    receipt = state.run.receipts[0]
    if feedback in ("failed-completion", "cancelled-completion"):
        receipt = changed(
            receipt,
            completion=core.CompletionStatus.FAILED
            if feedback == "failed-completion"
            else core.CompletionStatus.CANCELLED,
        )
    if feedback == "mutated-normalization":
        decision = receipt.decision
        assert isinstance(decision, core.Operation)
        assert decision.normalized_scope_reopen is not None
        receipt = changed(
            receipt,
            decision=changed(
                decision,
                normalized_scope_reopen=changed(
                    decision.normalized_scope_reopen, park_authority=core.RequestId(root="foreign")
                ),
            ),
        )
    if feedback == "rejected":
        receipt = changed(
            receipt,
            feedback=core.Rejected(
                decision_id=receipt.decision_id,
                code=core.RejectionCode.OWNERSHIP,
                path=("scope",),
                detail="rejected",
            ),
        )
    elif feedback == "wrong-id":
        receipt = changed(
            receipt, feedback=core.Accepted(decision_id=core.DecisionId(root="wrong"))
        )
    state = with_owner(
        state,
        owner,
        evaluation=core.EvaluationState(continuations=(continuation,)),
        run=changed(state.run, receipts=(receipt,)),
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
    elif feedback == "mutated-normalization":
        result = core.step(state, event, reducers=REDUCERS)
        assert result.requests == ()
        assert result.state.attempts == state.attempts
        with pytest.raises(ValidationError, match="durable normalization differs"):
            reload_state(state, codec)
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
    checkpoint: str, status: core.ObservationStatus, *, terminal: bool
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
        request_id=request_identity(request)
        if checkpoint != "wrong-request"
        else core.RequestId(root="other"),
        revision=request.revision
        if checkpoint != "wrong-revision"
        else changed(request.revision, digest="other"),
        retention="candidate" if checkpoint == "wrong-retention" else "wip",
    )
    edge = core.ReleaseDependency(kind="workspace", identity=request_identity(request))
    owner = changed(
        owner,
        closure=changed(owner.closure, disposition="settle"),
        release_dependencies=(owner.release_dependencies[0], edge),
        checkpoints=() if checkpoint == "absent" else (retained,),
    )
    intent = changed(state.intents.intents[1], request_id=request.request_id, request=request)
    state = with_owner(
        state,
        owner,
        settlement=core.SettlementState(pending=(pending_settlement(state, ref),)),
        intents=changed(state.intents, intents=(state.intents.intents[0], intent)),
    )
    proof = observation(ref, request_identity(request), status=status, terminal=terminal)
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
    assert result.state.settlement.settlements == ()


@pytest.mark.parametrize("causal_decision", [False, True])
@given(
    corruption=st.sampled_from(
        [
            "none",
            "request",
            "operation",
            "wire",
            "scope",
            "deadline",
            "retry",
            "park",
            "target",
        ]
    )
)
@example(corruption="none")
def test_reopen_cannot_dispatch_a_modified_registered_command(
    corruption: str, *, causal_decision: bool
) -> None:
    state, event, codec = reopen_fixture()
    request = event.request
    if not causal_decision:
        request = changed(request, decision_id=None)
    corruptions: dict[str, dict[str, object]] = {
        "request": {"request_id": None},
        "operation": {"operation_id": core.OperationId(root="other")},
        "wire": {"operation": changed(request.operation, payload_json="{}")},
        "scope": {"scope": core.Scope(owner=state.run.run_id, generation=1)},
        "deadline": {"deadline_at": 99.0},
        "retry": {"retry_limit": state.run.limits.max_retries + 1},
    }
    request = request.model_copy(update=corruptions.get(corruption, {}))
    if corruption in ("park", "target"):
        norm = event.normalization
        updates = (
            {"park_authority": core.RequestId(root="other")}
            if corruption == "park"
            else {"attempt": changed(norm.attempt, generation=1)}
        )
        event = changed(event, normalization=norm.model_copy(update=updates))
    event = changed(event, request=request)
    if corruption == "none":
        with pytest.raises(core.KernelNotImplementedError) as boundary:
            core.step(state, event, reducers=REDUCERS)
        assert boundary.value.event_kind == "attempt_reopen_requested"
    else:
        result = checked_step(state, event, codec=codec)
        assert result.requests == ()
        assert result.state.attempts == state.attempts


def acquired_reopen_fixture() -> tuple[
    core.CoreState, core.ScopeReopenRequested, core.OperationRegistry
]:
    state, event, codec = reopen_fixture()
    owner = changed(
        state.attempts.attempts[0],
        phase=core.AttemptPhase.ACQUIRING,
        admission_id=core.DecisionId(root="reopen"),
        sessions=(state.evaluation.continuations[0].invocation.session_id,),
    )
    assert owner.closure is not None
    scope = core.Scope(owner=owner.attempt_id, generation=owner.generation)
    assert owner.checkpoint is not None
    assert owner.admission_id is not None
    spec = changed(
        state.sessions.invocations[0].turn.session,
        session_id=state.evaluation.continuations[0].invocation.session_id,
    )
    resource = core.ResourceId(root="existing-conversation")
    requests = (
        core.RestoreRevision(
            request_id=core.RequestId(root="restored"),
            decision_id=owner.admission_id,
            scope=scope,
            attempt=event.normalization.attempt,
            admission_id=owner.admission_id,
            deadline_at=state.run.deadline_at,
            revision=owner.checkpoint,
        ),
        core.EnsureSession(
            request_id=core.RequestId(root="old-session"),
            scope=scope,
            spec=spec,
            admission_id=owner.closure.admission_id,
            deadline_at=state.run.deadline_at,
        ),
        core.EnsureSession(
            request_id=core.RequestId(root="reacquired-session"),
            scope=scope,
            spec=spec,
            admission_id=owner.admission_id,
            deadline_at=state.run.deadline_at,
            required_resource=resource,
        ),
    )
    intents = tuple(
        proof_intent(
            request,
            state.run.deadline_at,
            observation(
                event.normalization.attempt,
                request_identity(request),
                admission_id=request.admission_id,
                resource_id=resource if isinstance(request, core.EnsureSession) else None,
            ),
        )
        for request in requests
    )
    sessions = changed(
        state.sessions,
        sessions=(
            core.SessionView(
                spec=spec,
                scope=scope,
                generation=0,
                phase=core.SessionPhase.IDLE,
                accepted=True,
                resource_id=resource,
            ),
        ),
        acquisition_groups=(
            core.SessionAcquisitionGroup(
                attempt=event.normalization.attempt,
                admission_id=owner.admission_id,
                scope=scope,
                session_ids=owner.sessions,
                phase="ready",
            ),
        ),
    )
    state = with_owner(
        state,
        owner,
        sessions=sessions,
        intents=changed(state.intents, intents=intents),
    )
    return state, event, codec


@given(
    proof=st.tuples(
        st.sampled_from(["workspace", "session", "old-session", "group"]),
        st.sampled_from(list(core.ObservationStatus)),
        st.booleans(),
        st.booleans(),
        st.sampled_from(["exact", "absent", "wrong"]),
    )
)
@example(proof=("workspace", core.ObservationStatus.SUCCEEDED, True, True, "exact"))
def test_reopen_dispatch_waits_for_exact_workspace_and_physical_session_reacquisition(
    proof: tuple[str, core.ObservationStatus, bool, bool, str],
) -> None:
    part, status, accepted, terminal, identity = proof
    state, event, codec = acquired_reopen_fixture()
    intents = list(state.intents.intents)
    if part == "group":
        group = changed(
            state.sessions.acquisition_groups[0],
            phase="ready"
            if status == core.ObservationStatus.SUCCEEDED and accepted and terminal
            else "acquiring",
            session_ids=state.attempts.attempts[0].sessions if identity == "exact" else (),
        )
        state = changed(state, sessions=changed(state.sessions, acquisition_groups=(group,)))
    else:
        index = {"workspace": 0, "old-session": 1, "session": 2}[part]
        row = intents[index]
        assert row.observation is not None
        observation_updates: dict[str, object] = {
            "status": status,
            "accepted": accepted,
            "terminal": terminal,
        }
        if identity != "exact":
            if part == "workspace":
                observation_updates["admission_id"] = (
                    None if identity == "absent" else core.DecisionId(root="other")
                )
            else:
                observation_updates["resource_id"] = (
                    None if identity == "absent" else core.ResourceId(root="new-conversation")
                )
        intents[index] = changed(
            row, observation=row.observation.model_copy(update=observation_updates)
        )
        state = changed(state, intents=changed(state.intents, intents=tuple(intents)))
    signal = core.ReacquisitionReady(
        attempt=event.normalization.attempt,
        request_id=request_identity(event.request),
        admission_id=core.DecisionId(root="reopen"),
    )
    result = checked_step(state, signal, codec=codec)
    positive = (
        status == core.ObservationStatus.SUCCEEDED
        and accepted
        and (terminal or part == "old-session")
        and identity == "exact"
    )
    if positive:
        assert len(result.requests) == 1
        assert isinstance(result.requests[0], core.ExecuteRegisteredOperation)
        assert result.requests[0].operation == event.request.operation
    else:
        assert result.requests == ()
    assert result.state.attempts.attempts[0].charges == state.attempts.attempts[0].charges
    assert result.state.attempts.attempts[0].closure == state.attempts.attempts[0].closure


@pytest.mark.parametrize("canonical", [False, True])
@given(phase=st.sampled_from(list(core.SessionPhase)), pending=st.booleans())
@example(phase=core.SessionPhase.EXECUTING, pending=False)
@example(phase=core.SessionPhase.IDLE, pending=True)
def test_reopen_dispatch_requires_quiescent_reacquired_session(
    phase: core.SessionPhase, *, pending: bool, canonical: bool
) -> None:
    state, event, codec = acquired_reopen_fixture()
    session = changed(
        state.sessions.sessions[0],
        phase=phase,
        pending_intents=(core.RequestId(root="unfinished-turn"),) if pending else (),
    )
    state = changed(state, sessions=changed(state.sessions, sessions=(session,)))
    if not canonical:
        continuation = changed(
            state.evaluation.continuations[0], continuation_id=core.ContinuationId(root="foreign")
        )
        state = changed(state, evaluation=changed(state.evaluation, continuations=(continuation,)))
    result = checked_step(
        state,
        core.ReacquisitionReady(
            attempt=event.normalization.attempt,
            request_id=request_identity(event.request),
            admission_id=core.DecisionId(root="reopen"),
        ),
        codec=codec,
    )
    positive = (
        canonical
        and not pending
        and phase
        in (
            core.SessionPhase.IDLE,
            core.SessionPhase.CHECKPOINTED,
            core.SessionPhase.SUSPENDED,
        )
    )
    assert bool(result.requests) == positive
    assert all(isinstance(request, core.ExecuteRegisteredOperation) for request in result.requests)
    assert result.state.attempts == state.attempts


@given(
    disposition=st.sampled_from(["park", "cancel", "settle"]),
    supplied=st.sampled_from(["discard", "wip", "candidate"]),
    facts=st.tuples(
        st.sampled_from(list(core.ObservationStatus)), st.booleans(), st.booleans(), st.booleans()
    ),
)
@example(
    disposition="cancel",
    supplied="discard",
    facts=(core.ObservationStatus.SUCCEEDED, True, True, True),
)
@example(
    disposition="park", supplied="wip", facts=(core.ObservationStatus.SUCCEEDED, True, True, True)
)
@example(
    disposition="settle",
    supplied="candidate",
    facts=(core.ObservationStatus.SUCCEEDED, True, True, True),
)
def test_f4_disposal_requires_exact_disposition_and_positive_root_proof(
    disposition: Literal["park", "cancel", "settle"],
    supplied: Literal["discard", "wip", "candidate"],
    facts: tuple[core.ObservationStatus, bool, bool, bool],
) -> None:
    status, terminal, root_released, manifest_complete = facts
    durable = (
        status == core.ObservationStatus.SUCCEEDED
        and terminal
        and root_released
        and manifest_complete
    )
    state, ref = closing_fixture()
    owner = state.attempts.attempts[0]
    assert owner.closure is not None
    scope = core.Scope(owner=ref.attempt_id, generation=ref.generation)
    common: dict[str, object] = {
        "scope": scope,
        "attempt": ref,
        "admission_id": owner.admission_id,
        "depends_on": (owner.closure.authority,),
        "deadline_at": state.run.deadline_at,
    }
    if supplied == "discard":
        request: core.DiscardWorkspace | core.SnapshotAndRetain | core.RetainRevision = (
            core.DiscardWorkspace.model_validate(
                {
                    "request_id": core.RequestId(root="withdraw:discard"),
                    **common,
                }
            )
        )
    elif disposition == "park":
        request = core.SnapshotAndRetain.model_validate(
            {
                "request_id": core.RequestId(root="withdraw:retention"),
                "retention": supplied,
                **common,
            }
        )
    else:
        request = core.RetainRevision.model_validate(
            {
                "request_id": core.RequestId(root="withdraw:retention"),
                "retention": supplied,
                "revision": state.run.facts.baseline,
                **common,
            }
        )
    edge = core.ReleaseDependency(kind="workspace", identity=request_identity(request))
    owner = changed(
        owner,
        closure=changed(owner.closure, disposition=disposition),
        release_dependencies=(edge,),
        checkpoints=()
        if supplied == "discard"
        else (
            core.AttemptCheckpoint(
                invocation=None,
                request_id=request_identity(request),
                revision=state.run.facts.baseline,
                retention=supplied,
            ),
        ),
    )
    intent = changed(state.intents.intents[1], request_id=request.request_id, request=request)
    pending = changed(pending_settlement(state, ref), retention="candidate")
    state = with_owner(
        state,
        owner,
        intents=changed(state.intents, intents=(state.intents.intents[0], intent)),
        settlement=core.SettlementState(pending=(pending,))
        if disposition == "settle"
        else state.settlement,
    )
    state = recorded_proof(state, observation(ref, core.RequestId(root="withdraw")))
    proof = observation(
        ref,
        request_identity(request),
        status=status,
        terminal=terminal,
        released=root_released,
        children_complete=manifest_complete,
        resource_id=core.ResourceId(root="retention-root"),
    )
    state = recorded_proof(state, proof)
    event = core.ReleaseDependencyObserved(attempt=ref, dependency=edge, observation=proof)
    expected = {"park": "wip", "cancel": "discard", "settle": "candidate"}[disposition]
    if supplied == expected and not durable:
        result = checked_step(state, event)
        assert all(isinstance(request, core.InspectRequest) for request in result.requests)
    elif supplied == expected and supplied == "discard":
        with pytest.raises(core.KernelNotImplementedError) as boundary:
            core.step(state, event, reducers=REDUCERS)
        assert boundary.value.event_kind == "slot_released"
    elif supplied == expected:
        result = checked_step(state, event)
        assert len(result.requests) == 1
        discard = result.requests[0]
        assert isinstance(discard, core.DiscardWorkspace)
        assert owner.closure is not None
        assert discard.depends_on == (owner.closure.authority, request_identity(request))
        assert result.state.attempts.attempts[0].phase == core.AttemptPhase.CLOSING
        discard_proof = observation(ref, request_identity(discard))
        disposed = recorded_proof(result.state, discard_proof)
        with pytest.raises(core.KernelNotImplementedError) as boundary:
            core.step(
                disposed,
                core.ReleaseDependencyObserved(
                    attempt=ref,
                    dependency=core.ReleaseDependency(
                        kind="workspace", identity=request_identity(discard)
                    ),
                    observation=discard_proof,
                ),
                reducers=REDUCERS,
            )
        assert boundary.value.event_kind == "slot_released"
    else:
        result = checked_step(state, event)
        assert result.state.attempts.attempts[0].phase == core.AttemptPhase.CLOSING
        assert result.state.settlement.settlements == ()


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("aggregate", [False, True])
@given(
    prefix=st.text(alphabet="abc", min_size=1, max_size=8),
    suffix=st.text(alphabet="xyz", min_size=1, max_size=8),
)
def test_opaque_inspection_identities_are_injective_across_sources_and_children(
    prefix: str, suffix: str, *, historical: bool, aggregate: bool
) -> None:
    state, ref = closing_fixture()
    sources = (core.RequestId(root=prefix), core.RequestId(root=f"{prefix}:inspect:child:x"))
    resources = (core.ResourceId(root=f"x:inspect:child:{suffix}"), core.ResourceId(root=suffix))
    scope = core.Scope(owner=ref.attempt_id, generation=ref.generation)
    children = []
    intents = list(state.intents.intents)
    events = []
    for source, resource in zip(sources, resources, strict=True):
        proof = observation(
            ref,
            source,
            resource_id=resource,
            admission_id=core.DecisionId(root="old" if historical else "admission"),
            status=core.ObservationStatus.UNKNOWN,
            terminal=False,
            released=False,
            children_complete=False,
        )
        other = changed(
            proof,
            request_id=core.RequestId(root=f"aggregate:{source.root}"),
            event_id=core.EventId(root=f"aggregate:{source.root}"),
        )
        marks = (
            (proof,)
            if aggregate
            else tuple(sorted((proof, other), key=lambda mark: mark.request_id.root))
        )
        children.append(
            core.ChildLease(
                resource_id=resource,
                scope=scope,
                source_requests=tuple(mark.request_id for mark in marks),
                observation=proof if aggregate else other,
                observation_watermarks=tuple(
                    core.ChildObservationWatermark(source_request=mark.request_id, observation=mark)
                    for mark in marks
                ),
                watermark_history_complete=True,
            )
        )
        request = core.DiscardWorkspace(
            request_id=source,
            scope=scope,
            attempt=ref,
            admission_id=proof.admission_id,
            deadline_at=state.run.deadline_at,
        )
        root_proof = proof if aggregate else changed(proof, resource_id=None, children=(resource,))
        intents.append(
            changed(
                proof_intent(request, state.run.deadline_at, root_proof),
                phase=core.IntentPhase.RECONCILING,
            )
        )
        if not aggregate:
            other_request = changed(request, request_id=other.request_id)
            intents.append(
                changed(
                    proof_intent(other_request, state.run.deadline_at, other),
                    phase=core.IntentPhase.RECONCILING,
                )
            )
        events.append(
            core.ReleaseDependencyObserved(
                attempt=ref,
                dependency=core.ReleaseDependency(kind="job", identity=resource),
                observation=proof,
            )
        )
    owner = changed(
        state.attempts.attempts[0],
        release_dependencies=(
            *state.attempts.attempts[0].release_dependencies,
            *(event.dependency for event in events),
        ),
    )
    state = with_owner(
        state,
        owner,
        intents=changed(state.intents, intents=tuple(intents), children=tuple(children)),
    )
    ids = []
    for event in events:
        result = checked_step(state, event)
        assert len(result.requests) == 1
        request = result.requests[0]
        assert isinstance(request, core.InspectRequest)
        assert request.resource_id == event.observation.resource_id
        assert request.target == event.observation.request_id
        ids.append(request.request_id)
        state = result.state
        repeated = checked_step(state, event)
        assert repeated.requests == ()
    assert len(set(ids)) == 2
    assert state.scheduling.slots


@given(
    proof=st.tuples(
        st.sampled_from(list(core.ObservationStatus)),
        st.booleans(),
        st.booleans(),
        st.booleans(),
        st.booleans(),
        st.sampled_from(["exact", "missing-source", "missing-episode", "wrong-episode"]),
    )
)
@example(proof=(core.ObservationStatus.SUCCEEDED, True, True, True, True, "exact"))
def test_child_release_requires_complete_positive_history_from_every_source(
    proof: tuple[core.ObservationStatus, bool, bool, bool, bool, str],
) -> None:
    status, terminal, released, complete, history_complete, correspondence = proof
    state, ref = closing_fixture()
    resource = core.ResourceId(root="shared-child")
    source_a = core.RequestId(root="source-a")
    source_b = core.RequestId(root="source-b")
    first = observation(ref, source_a, resource_id=resource)
    second = observation(
        ref,
        source_b,
        resource_id=resource,
        status=status,
        terminal=terminal,
        released=released,
        children_complete=complete,
        admission_id=(
            None
            if correspondence == "missing-episode"
            else core.DecisionId(root="other")
            if correspondence == "wrong-episode"
            else core.DecisionId(root="admission")
        ),
    )
    child = core.ChildLease(
        resource_id=resource,
        scope=first.scope,
        source_requests=(source_a, source_b),
        observation=first,
        observation_watermarks=(
            core.ChildObservationWatermark(source_request=source_a, observation=first),
            core.ChildObservationWatermark(source_request=source_b, observation=second),
        ),
        watermark_history_complete=history_complete,
    )
    sources = tuple(
        core.Intent(
            request_id=mark.request_id,
            request=core.DiscardWorkspace(
                request_id=mark.request_id,
                scope=mark.scope,
                attempt=ref,
                admission_id=core.DecisionId(root="admission"),
                deadline_at=state.run.deadline_at,
            ),
            payload_digest="source-proof",
            lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
            phase=core.IntentPhase.COMPLETED,
            observation=mark,
            reconcile_deadline_at=state.run.deadline_at,
        )
        for mark in (first, second)
    )
    state = changed(
        state,
        intents=changed(
            state.intents,
            children=(child,),
            intents=(*state.intents.intents, *sources)
            if correspondence != "missing-source"
            else state.intents.intents,
        ),
    )
    root = observation(ref, core.RequestId(root="withdraw"))
    state = recorded_proof(state, root)
    result = checked_step(
        state,
        core.ReleaseDependencyObserved(
            attempt=ref,
            dependency=state.attempts.attempts[0].release_dependencies[0],
            observation=root,
        ),
    )
    edge = core.ReleaseDependency(kind="job", identity=resource)
    positive = (
        history_complete
        and correspondence == "exact"
        and status not in (core.ObservationStatus.UNKNOWN, core.ObservationStatus.PENDING)
        and terminal
        and released
        and complete
    )
    assert (edge not in result.state.attempts.attempts[0].release_dependencies) == positive
    assert result.state.settlement == state.settlement


@pytest.mark.parametrize(
    "completion", [None, core.CompletionStatus.FAILED, core.CompletionStatus.CANCELLED]
)
@given(canonical=st.booleans(), accepted=st.booleans(), terminal=st.booleans())
def test_unknown_scope_reopen_inspects_before_reacquisition_or_typed_outcome(
    *, canonical: bool, accepted: bool, terminal: bool, completion: core.CompletionStatus | None
) -> None:
    state, event, codec = reopen_fixture()
    state = changed(
        state,
        run=changed(state.run, receipts=(changed(state.run.receipts[0], completion=completion),)),
    )
    owner = changed(
        state.attempts.attempts[0],
        phase=core.AttemptPhase.ACQUIRING,
        admission_id=core.DecisionId(root="reopen"),
    )
    request = changed(event.request, admission_id=owner.admission_id)
    assert request.request_id is not None
    observed = observation(
        event.normalization.attempt,
        request.request_id,
        scope=request.scope,
        admission_id=owner.admission_id,
        status=core.ObservationStatus.UNKNOWN,
        accepted=accepted,
        terminal=terminal,
        released=False,
        children_complete=False,
    )
    intent = core.Intent(
        request_id=request_identity(request),
        request=request,
        payload_digest="reopen",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        phase=core.IntentPhase.RECONCILING,
        reconcile_deadline_at=state.run.deadline_at,
        observation=observed if canonical else None,
    )
    state = with_owner(state, owner, intents=changed(state.intents, intents=(intent,)))
    signal = core.ScopeAdmissionReopened(
        attempt=event.normalization.attempt,
        continuation_id=event.normalization.continuation_id,
        park_authority=event.normalization.park_authority,
        operation_id=request.operation_id,
        observation=observed,
        admission="unknown",
    )
    result = checked_step(state, signal, codec=codec)
    if canonical:
        assert len(result.requests) == 1
        assert isinstance(result.requests[0], core.InspectRequest)
        assert result.requests[0].target == request.request_id
        assert checked_step(result.state, signal, codec=codec).requests == ()
    else:
        assert result.requests == ()
    assert result.state.attempts == state.attempts


@given(
    guard=st.sampled_from(
        [
            "exact",
            "admission",
            "request",
            "rejected",
            "wrong-feedback",
            "phase",
            "park",
            "checkpoint",
            "late-child",
        ]
    )
)
@example(guard="exact")
def test_reopen_admitted_requires_exact_accepted_episode_and_park_proof(guard: str) -> None:
    state, event, codec = reopen_fixture()
    admission = core.DecisionId(root="reopen" if guard != "admission" else "other")
    request_id = event.request.request_id
    assert request_id is not None
    if guard == "request":
        request_id = core.RequestId(root="other")
    if guard in ("rejected", "wrong-feedback"):
        receipt = state.run.receipts[0]
        feedback: core.Accepted | core.Rejected = (
            core.Rejected(
                decision_id=receipt.decision_id,
                code=core.RejectionCode.OWNERSHIP,
                path=("reopen",),
                detail="declined",
            )
            if guard == "rejected"
            else core.Accepted(decision_id=core.DecisionId(root="other"))
        )
        state = changed(
            state, run=changed(state.run, receipts=(changed(receipt, feedback=feedback),))
        )
    if guard in ("phase", "checkpoint"):
        owner = changed(
            state.attempts.attempts[0],
            phase=core.AttemptPhase.ACTIVE if guard == "phase" else core.AttemptPhase.PARKED,
            checkpoints=() if guard == "checkpoint" else state.attempts.attempts[0].checkpoints,
        )
        state = with_owner(state, owner)
    if guard == "park":
        continuation = changed(
            state.evaluation.continuations[0], park_authority=core.RequestId(root="other")
        )
        state = changed(state, evaluation=changed(state.evaluation, continuations=(continuation,)))
    if guard == "late-child":
        child = core.ChildLease(
            resource_id=core.ResourceId(root="late"),
            scope=core.Scope(owner=event.normalization.attempt.attempt_id, generation=0),
            source_requests=(core.RequestId(root="late-source"),),
        )
        state = changed(state, intents=changed(state.intents, children=(child,)))
    signal = core.ScopeReopenAdmitted(
        attempt=event.normalization.attempt,
        request_id=request_id,
        admission_id=admission,
    )
    if guard in ("exact", "late-child"):
        with pytest.raises(core.KernelNotImplementedError) as boundary:
            core.step(state, signal, reducers=REDUCERS)
        assert boundary.value.event_kind == (
            "slot_charge_ended" if guard == "late-child" else "attempt_reacquire_requested"
        )
    else:
        result = checked_step(state, signal, codec=codec)
        assert result.requests == ()
        assert result.state.attempts == state.attempts


@given(admission=st.sampled_from(["reopen", "admission", "stale"]))
@example(admission="reopen")
def test_queued_reopen_withdrawal_targets_the_new_queue_episode(admission: str) -> None:
    state, event, codec = reopen_fixture()
    assert event.request.request_id is not None
    state = changed(
        state,
        scheduling=changed(
            state.scheduling,
            slots=(),
            queue=(
                core.AttemptReopenRequest(
                    decision_id=core.DecisionId(root="reopen"),
                    request_id=request_identity(event.request),
                    attempt=event.normalization.attempt,
                ),
            ),
        ),
    )
    signal = changed(
        retirement(event.normalization.attempt), admission_id=core.DecisionId(root=admission)
    )
    if admission == "reopen":
        with pytest.raises(core.KernelNotImplementedError) as boundary:
            core.step(state, signal, reducers=REDUCERS)
        assert boundary.value.event_kind == "queue_entry_retired"
    else:
        result = checked_step(state, signal, codec=codec)
        assert result.state.attempts == state.attempts
        assert result.requests == ()


@pytest.mark.parametrize(
    "completion", [None, core.CompletionStatus.FAILED, core.CompletionStatus.CANCELLED]
)
@given(
    proof=st.tuples(
        st.sampled_from(
            [
                core.ObservationStatus.SUCCEEDED,
                core.ObservationStatus.PENDING,
                core.ObservationStatus.FAILED,
            ]
        ),
        st.booleans(),
        st.booleans(),
        st.sampled_from(["exact", "missing", "scope", "admission"]),
    )
)
@example(proof=(core.ObservationStatus.SUCCEEDED, True, True, "exact"))
def test_scope_reopen_completion_requires_registered_exact_positive_outcome(
    proof: tuple[core.ObservationStatus, bool, bool, str],
    *,
    completion: core.CompletionStatus | None,
) -> None:
    status, accepted, terminal, outcome_identity = proof
    state, event, codec = acquired_reopen_fixture()
    state = changed(
        state,
        run=changed(state.run, receipts=(changed(state.run.receipts[0], completion=completion),)),
    )
    owner = state.attempts.attempts[0]
    request = changed(event.request, admission_id=owner.admission_id)
    assert request.request_id is not None
    observed = observation(
        event.normalization.attempt,
        request.request_id,
        scope=request.scope,
        admission_id=owner.admission_id,
        status=status,
        accepted=accepted,
        terminal=terminal,
    )
    schema = request.operation.schema_ref
    outcome = core.ScopedAdmissionReopenOutcome(
        scope=core.Scope(
            owner=owner.attempt_id, generation=owner.generation + (outcome_identity == "scope")
        ),
        admission="closed" if outcome_identity == "admission" else "reopened",
    )
    intent = changed(
        core.Intent(
            request_id=request_identity(request),
            request=request,
            payload_digest="reopened",
            lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
            phase=core.IntentPhase.COMPLETED,
            observation=observed,
            reconcile_deadline_at=state.run.deadline_at,
        ),
        outcome_schema=None if outcome_identity == "missing" else schema.outcome_schema,
        outcome_json=None
        if outcome_identity == "missing"
        else codec.encode_outcome(schema, outcome),
    )
    state = reload_state(
        changed(state, intents=changed(state.intents, intents=(*state.intents.intents, intent))),
        codec,
    )
    signal = core.ScopeAdmissionReopened(
        attempt=event.normalization.attempt,
        continuation_id=event.normalization.continuation_id,
        park_authority=event.normalization.park_authority,
        operation_id=request.operation_id,
        observation=observed,
        admission=outcome.admission,
    )
    positive = (
        status == core.ObservationStatus.SUCCEEDED
        and accepted
        and terminal
        and outcome_identity == "exact"
        and completion is None
    )
    failure = terminal and (
        status == core.ObservationStatus.FAILED
        or (status == core.ObservationStatus.SUCCEEDED and outcome_identity == "admission")
        or (
            status == core.ObservationStatus.SUCCEEDED
            and outcome_identity == "exact"
            and completion is not None
        )
    )
    if failure:
        with pytest.raises(core.KernelNotImplementedError) as boundary:
            core.step(state, signal, reducers=REDUCERS)
        assert boundary.value.event_kind == "slot_charge_ended"
        return
    result = checked_step(state, signal, codec=codec)
    assert result.requests == ()
    if positive:
        assert result.state.attempts.attempts[0].phase == core.AttemptPhase.ACTIVE
        assert result.state.attempts.attempts[0].closure is None
        completed = tuple(row for row in result.state.run.receipts if row.completion is not None)
        assert len(completed) == 1
        assert completed[0].decision_id == core.DecisionId(root="reopen")
        assert completed[0].completion == core.CompletionStatus.SUCCEEDED
        repeated = checked_step(result.state, signal, codec=codec)
        assert repeated.state.run.receipts == result.state.run.receipts
        assert repeated.requests == ()
        assert repeated.events == ()
    else:
        assert result.state.attempts == state.attempts
