from collections.abc import Mapping
from typing import ClassVar, Literal, Protocol, Self

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

import vs_core.api as core
from vs_core.api import ObservationStatus, SessionPhase
from vs_sim.api.testing import examples

from .guard_rows import by_pairs
from .reopen_facts import released_job
from .restart import restarts


class _Copyable(Protocol):
    def model_copy(self, *, update: Mapping[str, object] | None = None) -> Self: ...


def changed[T: _Copyable](value: T, **fields: object) -> T:
    return value.model_copy(update=fields)


def reload_state(
    state: core.CoreState, codec: core.OperationRegistry | None = None
) -> core.CoreState:
    if codec is None and not restarts():
        return state
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


def fake_evaluation(
    state: core.EvaluationState, context: core.EvaluationContext, event: core.EvaluationEvent
) -> core.AreaChange[core.EvaluationState]:
    """The production Evaluation reducer, except for the reopened-scope notice.

    These fixtures prove Attempts B retirement. They do not carry the canonical
    yielded turn and session that the production Evaluation reducer demands
    before it accepts a reopened continuation scope, so that one event only
    checks that its attempt is active again. Evaluation's own tests cover the
    rest.
    """
    if isinstance(event, core.ContinuationScopeReopened):
        assert any(
            row.phase == core.AttemptPhase.ACTIVE and row.closure is None
            for row in context.attempts.attempts
        )
        return core.AreaChange(state=state)
    return core.advance_evaluation(state, context, event)


REDUCERS = core.CoreReducers(evaluation=fake_evaluation)


def request_identity(request: core.Request) -> core.RequestId:
    assert request.request_id is not None
    return request.request_id


def proof_record(
    request: core.Request,
    deadline: float,
    proof: core.Observation | None = None,
    *,
    lifecycle: core.LifecycleClass = core.LifecycleClass.IDEMPOTENT_WRITE,
) -> core.RequestRecord:
    return core.RequestRecord(
        request_id=request_identity(request),
        request=request,
        payload_digest="durable",
        lifecycle=lifecycle,
        phase=core.RequestPhase.PREPARED if proof is None else core.RequestPhase.COMPLETED,
        observation=proof,
        until=core.Until(at=deadline),
        addressed_by=(),
    )


def with_owner(state: core.CoreState, owner: core.AttemptView, **areas: object) -> core.CoreState:
    return state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,)), **areas})


def recorded_proof(state: core.CoreState, proof: core.Observation) -> core.CoreState:
    return changed(
        state,
        records=changed(
            state.records,
            records=tuple(
                changed(row, observation=proof) if row.request_id == proof.request_id else row
                for row in state.records.records
            ),
        ),
    )


def checked_step(
    state: core.CoreState, event: core.CoreEvent, *, codec: core.OperationRegistry | None = None
) -> core.Transition:
    if codec is None and not restarts():
        return core.step(state, event, reducers=REDUCERS)
    before = state.model_dump_json()
    result = core.step(state, event, reducers=REDUCERS)
    assert core.step(reload_state(state, codec), event, reducers=REDUCERS) == result
    assert state.model_dump_json() == before
    assert result.state.scheduling == state.scheduling
    assert result.state.settlement == state.settlement
    reloaded = reload_state(result.state, codec)
    assert reloaded == result.state
    assert core.project(result.state) == core.project(reloaded)
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
            "status": ObservationStatus.SUCCEEDED,
            "accepted": True,
            "terminal": True,
            "released": True,
            "children_complete": True,
            "admission_id": core.DecisionId(root="admission"),
            **facts,
        }
    )


def closing_fixture() -> tuple[core.CoreState, core.AttemptRef]:
    state, ref = fixture()
    event = retirement(ref)
    scope = core.Scope(owner=ref.attempt_id, generation=ref.generation)
    fields: dict[str, object] = {
        "scope": scope,
        "attempt": ref,
        "admission_id": event.admission_id,
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
    records = tuple(proof_record(request, state.run.deadline_at) for request in requests)
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
    return with_owner(state, owner, records=changed(state.records, records=records)), ref


def undisposed_fixture() -> tuple[core.CoreState, core.AttemptRef]:
    state, ref = closing_fixture()
    owner = state.attempts.attempts[0]
    owner = changed(owner, release_dependencies=owner.release_dependencies[:1])
    return with_owner(
        state, owner, records=changed(state.records, records=state.records.records[:1])
    ), ref


@given(
    status=st.sampled_from(list(ObservationStatus)),
    terminal=st.booleans(),
    released=st.booleans(),
    complete=st.booleans(),
    episode=st.sampled_from([None, "admission", "stale"]),
)
def test_scope_close_guard_requires_positive_exact_complete_manifest(
    status: ObservationStatus,
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
    if episode in ("stale", None):
        # The kernel rejects a proof outside the canonical episode on ingress.
        with pytest.raises(core.ContractError, match=r"observation"):
            core.step(state, event, reducers=REDUCERS)
        return
    result = checked_step(state, event)
    positive = (
        status == ObservationStatus.SUCCEEDED and terminal and complete and episode == "admission"
    )
    if not positive:
        assert dependency in result.state.attempts.attempts[0].release_dependencies
    else:
        assert dependency not in result.state.attempts.attempts[0].release_dependencies


@given(
    statuses=st.lists(
        st.tuples(
            st.sampled_from(list(ObservationStatus)),
            st.sampled_from(["park", "cancel", "settle"]),
        ),
        min_size=1,
        max_size=15,
    ),
)
def test_f8_reordered_withdrawal_and_unknown_release_preserve_cleanup_and_capacity(
    statuses: list[tuple[ObservationStatus, Literal["park", "cancel", "settle"]]],
) -> None:
    state, ref = closing_fixture()
    dependency = core.ReleaseDependency(
        kind="workspace", identity=core.RequestId(root="withdraw:discard")
    )
    charges = state.attempts.attempts[0].charges
    assert isinstance(dependency.identity, core.RequestId)
    for index, (status, disposition) in enumerate(statuses):
        retired = checked_step(state, retirement(ref, disposition))
        assert retired.state.attempts == state.attempts
        assert retired.requests == ()
        proof = observation(
            ref, dependency.identity, status=status, sequence=index % 3, released=False
        )
        state = recorded_proof(state, proof)
        result = checked_step(
            state,
            core.ReleaseDependencyObserved(attempt=ref, dependency=dependency, observation=proof),
        )
        assert result.state.attempts.attempts[0].charges == charges
        if status in (ObservationStatus.REJECTED, ObservationStatus.FAILED):
            # The owner refused the discard: nothing is left to wait for, so the attempt ends.
            assert dependency not in result.state.attempts.attempts[0].release_dependencies
            return
        assert all(isinstance(request, core.InspectRequest) for request in result.requests)
        assert dependency in result.state.attempts.attempts[0].release_dependencies
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
        result = core.step(state, retirement(ref), reducers=REDUCERS)
        closed = result.state.attempts.attempts[0]
        assert closed.closure is not None
        assert closed.closure.disposition == "cancel"
    else:
        result = checked_step(state, retirement(ref))
        assert (result.state.attempts, result.requests) == (state.attempts, ())


@pytest.mark.parametrize("signal", ["settle", "retention", "park", "cancel"])
@settings(max_examples=examples(pr=4, nightly=100))
@given(
    pending=st.sampled_from([None, "matching", "foreign", "duplicate", "final"]),
    authority=st.booleans(),
    retention=st.sampled_from(["discard", "wip", "candidate"]),
    revision=st.booleans(),
)
def test_settle_requires_exact_pending_settlement_and_closure_authority(
    pending: str | None,
    retention: Literal["discard", "wip", "candidate"],
    *,
    authority: bool,
    revision: bool,
    signal: Literal["settle", "retention", "park", "cancel"],
) -> None:
    state, ref = fixture()
    if pending is not None:
        settlement = changed(
            pending_settlement(state, ref),
            attempt=changed(ref, generation=1) if pending == "foreign" else ref,
        )
        rows = (settlement,) * (2 if pending == "duplicate" else 1)
        state = changed(
            state,
            settlement=core.SettlementState.model_validate(
                {"settlements" if pending == "final" else "pending": rows}
            ),
        )
    event = (
        core.RetentionRequired(
            attempt=ref,
            retention=retention,
            revision=state.run.facts.baseline if revision else None,
        )
        if signal == "retention"
        else changed(
            retirement(ref, "settle" if signal == "settle" else signal),
            authority=core.RequestId(root="pending:close" if authority else "foreign"),
        )
    )
    positive = (
        (signal == "settle" and pending == "matching" and authority)
        or (signal == "retention" and pending == "matching" and retention == "wip" and revision)
        or (signal in ("park", "cancel") and pending in (None, "foreign"))
    )
    if positive:
        result = core.step(state, event, reducers=REDUCERS)
        assert result.state.attempts != state.attempts
        assert result.state.attempts.attempts[0].closure is not None
    else:
        result = checked_step(state, event)
        assert (result.state.attempts, result.requests) == (state.attempts, ())


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
            request=RegisteredWriter(turn=invocation.turn),
        )
    )
    request = core.ExecuteRegisteredOperation(
        request_id=core.RequestId(root="operation:writer"),
        decision_id=decision.decision_id,
        operation_id=core.OperationId(root="operation:writer"),
        scope=invocation.scope,
        admission_id=core.DecisionId(root="admission"),
        operation=codec.encode(decision.request),
        retry_limit=state.run.limits.request_retries.limit,
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
            normalized_turn=changed(invocation.turn, max_turns=invocation.turn.max_turns + 1),
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
    invocation = changed(invocation, registered_operation=request.operation_id)
    return state, invocation, request, codec


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
        charge_class=charge,
        predecessor=changed(invocation, invocation_id=core.InvocationId(root="predecessor"))
        if predecessor
        else None,
    )
    return core.Invocation(
        until=core.Until(at=100.0),
        invocation=invocation,
        scope=scope,
        turn=turn,
        phase=SessionPhase.TERMINAL,
    )


@by_pairs(
    ("charge", ["paid", "correction", "resume", "free"]),
    ("predecessor", [False, True]),
    ("reuse", [False, True]),
    ("registered", [False, True]),
)
@settings(max_examples=examples(pr=1, nightly=100))
@given(
    proof=st.tuples(
        st.sampled_from(list(ObservationStatus)),
        st.booleans(),
        st.booleans(),
        st.booleans(),
        st.sampled_from([None, "admission", "stale"]),
        st.sampled_from(["accepted", "rejected", "wrong-id", "mutated"]),
    )
)
@example(proof=(ObservationStatus.SUCCEEDED, True, True, True, "admission", "accepted"))
# The release is positive only when every part of the proof is: each of these fails exactly one,
# in every kind of writer, so a pull request needs few drawn proofs beside them.
@example(proof=(ObservationStatus.UNKNOWN, True, True, True, "admission", "accepted"))
@example(proof=(ObservationStatus.PENDING, True, True, True, "admission", "accepted"))
@example(proof=(ObservationStatus.SUCCEEDED, False, True, True, "admission", "accepted"))
@example(proof=(ObservationStatus.SUCCEEDED, True, False, True, "admission", "accepted"))
@example(proof=(ObservationStatus.SUCCEEDED, True, True, False, "admission", "accepted"))
@example(proof=(ObservationStatus.SUCCEEDED, True, True, True, None, "accepted"))
@example(proof=(ObservationStatus.SUCCEEDED, True, True, True, "stale", "accepted"))
@example(proof=(ObservationStatus.SUCCEEDED, True, True, True, "admission", "rejected"))
@example(proof=(ObservationStatus.SUCCEEDED, True, True, True, "admission", "wrong-id"))
@example(proof=(ObservationStatus.SUCCEEDED, True, True, True, "admission", "mutated"))
def test_every_writer_variant_requires_exact_complete_lease_release_before_disposal(
    charge: Literal["paid", "correction", "resume", "free"],
    proof: tuple[ObservationStatus, bool, bool, bool, str | None, str],
    *,
    predecessor: bool,
    reuse: bool,
    registered: bool,
) -> None:
    status, terminal, released, complete, episode, guard = proof
    state, ref = undisposed_fixture()
    owner = state.attempts.attempts[0]
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
    record = proof_record(
        request, state.run.deadline_at, writer_proof, lifecycle=core.LifecycleClass.SESSION_TURN
    )
    lease_id = core.RequestId(root="writer-lease")
    lease = proof_record(
        core.CloseSession(
            request_id=lease_id,
            scope=invocation.scope,
            session_id=invocation.invocation.session_id,
            admission_id=core.DecisionId(root="admission"),
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
                    phase=SessionPhase.TERMINAL,
                    accepted=True,
                    resource_id=core.ResourceId(root="writer-conversation"),
                ),
            ),
        ),
        records=changed(
            state.records,
            records=(*state.records.records, record)
            if reuse
            else (*state.records.records, record, lease),
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
        status not in (ObservationStatus.UNKNOWN, ObservationStatus.PENDING)
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
    status=st.sampled_from(list(ObservationStatus)),
    terminal=st.booleans(),
    complete=st.booleans(),
    canonical=st.booleans(),
)
@example(status=ObservationStatus.SUCCEEDED, terminal=True, complete=True, canonical=True)
def test_empty_writer_set_never_authorizes_disposal_before_positive_scope_fence(
    status: ObservationStatus, *, terminal: bool, complete: bool, canonical: bool
) -> None:
    state, ref = undisposed_fixture()
    owner = state.attempts.attempts[0]
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
    if canonical and status == ObservationStatus.SUCCEEDED and terminal and complete:
        assert len(result.requests) == 1
        assert isinstance(result.requests[0], core.DiscardWorkspace)
        assert core.pending_requests(result.state.records)[-1] == result.requests[0]
    else:
        assert all(isinstance(request, core.InspectRequest) for request in result.requests)


@given(location=st.sampled_from(["retained", "latest", "both"]), released=st.booleans())
def test_typed_job_manifest_always_fences_unreleased_descendants(
    location: str, *, released: bool
) -> None:
    state, ref = closing_fixture()
    child = core.ResourceId(root="retained-child")
    proof = observation(
        ref,
        core.RequestId(root="withdraw"),
        resource_id=core.ResourceId(root="parent"),
        released=released,
    )
    state = recorded_proof(state, proof)
    job = core.RegisteredOwnedJob(
        wait=core.WaitOn(timer=0.0),
        operation_id=core.OperationId(root="parent-operation"),
        request_id=proof.request_id,
        scope=proof.scope,
        resource_pool=core.PoolId(root="pool"),
        resource_id=proof.resource_id,
        status=proof.status,
        observation=changed(proof, children=(child,) if location != "retained" else ()),
        children=(child,) if location != "latest" else (),
    )
    state = changed(state, evaluation=core.EvaluationState(registered_jobs=(job,)))
    result = checked_step(
        state,
        core.ReleaseDependencyObserved(
            attempt=ref,
            dependency=state.attempts.attempts[0].release_dependencies[0],
            observation=proof,
        ),
    )
    assert core.ReleaseDependency(kind="job", identity=child) in (
        result.state.attempts.attempts[0].release_dependencies
    )
    assert not any(isinstance(request, core.DiscardWorkspace) for request in result.requests)


@pytest.mark.parametrize("job_released", [True, False])
def test_a_job_ledger_release_proves_a_job_a_close_left_as_an_unsettled_child(
    *, job_released: bool
) -> None:
    """Chaos seeds 268 and 479: a close left a job behind that it could not settle.

    The job's own ledger later proved it released. That proof releases the job edge; without
    it the attempt never retires.
    """
    state, ref = closing_fixture()
    job, job_record = released_job(state.attempts.attempts[0])
    if not job_released:
        job = changed(job, observation=None)
    resource = job.resource_id
    assert resource is not None
    proof = observation(ref, core.RequestId(root="withdraw"))
    state = recorded_proof(state, proof)
    state = changed(
        state,
        evaluation=core.EvaluationState(registered_jobs=(job,)),
        records=changed(state.records, records=(*state.records.records, job_record)),
    )

    result = checked_step(
        state,
        core.ReleaseDependencyObserved(
            attempt=ref,
            dependency=state.attempts.attempts[0].release_dependencies[0],
            observation=proof,
        ),
    )

    edge = core.ReleaseDependency(kind="job", identity=resource)
    assert (edge in result.state.attempts.attempts[0].release_dependencies) != job_released


def test_an_outstanding_job_edge_blocks_disposal_after_a_positive_scope_fence() -> None:
    state, ref = undisposed_fixture()
    owner = state.attempts.attempts[0]
    job_edge = core.ReleaseDependency(kind="job", identity=core.ResourceId(root="leftover"))
    state = with_owner(
        state, changed(owner, release_dependencies=(*owner.release_dependencies, job_edge))
    )
    proof = observation(ref, core.RequestId(root="withdraw"))
    state = recorded_proof(state, proof)
    result = checked_step(
        state,
        core.ReleaseDependencyObserved(
            attempt=ref, dependency=owner.release_dependencies[0], observation=proof
        ),
    )
    assert job_edge in result.state.attempts.attempts[0].release_dependencies
    assert not any(isinstance(request, core.DiscardWorkspace) for request in result.requests)


@given(
    checkpoint=st.sampled_from(
        ["exact", "absent", "wrong-request", "wrong-revision", "wrong-retention"]
    ),
    status=st.sampled_from(list(ObservationStatus)),
    terminal=st.booleans(),
)
@example(checkpoint="exact", status=ObservationStatus.SUCCEEDED, terminal=True)
def test_retention_crash_ack_requires_exact_checkpoint_history(
    checkpoint: str, status: ObservationStatus, *, terminal: bool
) -> None:
    state, ref = closing_fixture()
    owner = state.attempts.attempts[0]
    assert owner.closure is not None
    request = core.RetainRevision(
        request_id=core.RequestId(root="withdraw:retention"),
        depends_on=(owner.closure.authority,),
        scope=core.Scope(owner=ref.attempt_id, generation=ref.generation),
        admission_id=owner.admission_id,
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
    record = changed(state.records.records[1], request_id=request.request_id, request=request)
    state = with_owner(
        state,
        owner,
        settlement=core.SettlementState(pending=(pending_settlement(state, ref),)),
        records=changed(state.records, records=(state.records.records[0], record)),
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
    refused = status in (ObservationStatus.REJECTED, ObservationStatus.FAILED) and terminal
    if (checkpoint == "exact" and status == ObservationStatus.SUCCEEDED and terminal) or refused:
        assert edge not in result.state.attempts.attempts[0].release_dependencies
    else:
        assert edge in result.state.attempts.attempts[0].release_dependencies
    assert result.state.attempts.attempts[0].checkpoints == owner.checkpoints


def _assert_refusal_resolves(
    state: core.CoreState,
    event: core.ReleaseDependencyObserved,
    edge: core.ReleaseDependency,
    *,
    discard: bool,
) -> None:
    if discard:
        result = core.step(state, event, reducers=REDUCERS)
        assert result.state.attempts.attempts[0].phase != core.AttemptPhase.CLOSING
        return
    result = checked_step(state, event)
    assert edge not in result.state.attempts.attempts[0].release_dependencies
    assert all(isinstance(request, core.DiscardWorkspace) for request in result.requests)


def _refused(
    status: ObservationStatus, *, terminal: bool, supplied: str, expected: str, disposition: str
) -> bool:
    """Whether the owner answered the disposal "no", so nothing is left to wait for.

    A refused discard ends the attempt and a refused settle retention fails it and discards;
    a park cannot give its state up.
    """
    return (
        status in (ObservationStatus.REJECTED, ObservationStatus.FAILED)
        and terminal
        and supplied == expected
        and (supplied == "discard" or disposition == "settle")
    )


@given(
    disposition=st.sampled_from(["park", "cancel", "settle"]),
    supplied=st.sampled_from(["discard", "wip", "candidate"]),
    facts=st.tuples(
        st.sampled_from(list(ObservationStatus)), st.booleans(), st.booleans(), st.booleans()
    ),
)
@example(
    disposition="cancel",
    supplied="discard",
    facts=(ObservationStatus.SUCCEEDED, True, True, True),
)
@example(disposition="park", supplied="wip", facts=(ObservationStatus.SUCCEEDED, True, True, True))
@example(
    disposition="settle",
    supplied="candidate",
    facts=(ObservationStatus.SUCCEEDED, True, True, True),
)
def test_f4_disposal_requires_exact_disposition_and_positive_root_proof(
    disposition: Literal["park", "cancel", "settle"],
    supplied: Literal["discard", "wip", "candidate"],
    facts: tuple[ObservationStatus, bool, bool, bool],
) -> None:
    status, terminal, root_released, manifest_complete = facts
    durable = (
        status == ObservationStatus.SUCCEEDED and terminal and root_released and manifest_complete
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
    record = changed(state.records.records[1], request_id=request.request_id, request=request)
    pending = changed(pending_settlement(state, ref), retention="candidate")
    state = with_owner(
        state,
        owner,
        records=changed(state.records, records=(state.records.records[0], record)),
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
    refused = _refused(
        status, terminal=terminal, supplied=supplied, expected=expected, disposition=disposition
    )
    if refused:
        _assert_refusal_resolves(state, event, edge, discard=supplied == "discard")
    elif supplied == expected and not durable:
        result = checked_step(state, event)
        assert all(isinstance(request, core.InspectRequest) for request in result.requests)
    elif supplied == expected and supplied == "discard":
        # A discard disposal that is proved releases the slot: the attempt ends.
        result = core.step(state, event, reducers=REDUCERS)
        assert result.state.attempts.attempts[0].phase != core.AttemptPhase.CLOSING
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
        released = core.step(
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
        assert released.state.attempts.attempts[0].phase != core.AttemptPhase.CLOSING
    else:
        result = checked_step(state, event)
        assert result.state.attempts.attempts[0].phase == core.AttemptPhase.CLOSING


