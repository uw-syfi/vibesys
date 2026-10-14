"""Invocation accounting and checkpoint proofs through the public core facade."""

import json
from hashlib import sha256

import pytest

from vs_core.api import (
    Accepted,
    Access,
    AttemptBudget,
    AttemptChargeRefundRequested,
    AttemptCheckpoint,
    AttemptExhausted,
    AttemptId,
    AttemptPhase,
    AttemptRef,
    AttemptsContext,
    AttemptsState,
    AttemptView,
    ChargeId,
    ChargeKind,
    ChargeReceipt,
    CoreState,
    DecisionId,
    DecisionReceipt,
    DispatchTurn,
    EventId,
    InterruptClaim,
    Invocation,
    InvocationChargeRequested,
    InvocationCheckpointRequested,
    InvocationId,
    InvocationRef,
    ItemId,
    LifecycleClass,
    Observation,
    ObservationStatus,
    Rejected,
    RejectionCode,
    RequestId,
    RequestPhase,
    RequestRecord,
    RequestTurn,
    ResourceId,
    RoleId,
    SchemaRef,
    Scope,
    SessionAcquisitionGroup,
    SessionId,
    SessionPhase,
    SessionSpec,
    SessionView,
    SnapshotAndRetain,
    TurnSpec,
    Until,
    WorkspaceMode,
    WorkspacePlan,
    advance_attempt,
    initial_state,
)
from vs_core.api.testing import sample_limits

from .guard_rows import guard_rows

STATUSES = [ObservationStatus.SUCCEEDED, ObservationStatus.REJECTED]
CORRELATIONS = ["exact", "unrelated", "unregistered"]
EPISODES = ["absent", "old", "current"]
RECEIPTS = [
    "absent",
    "rejected",
    "mismatched",
    "exact",
    "foreign-request-decision",
    "missing-request-membership",
    "foreign-session-scope",
]
LIFECYCLES = [
    (phase, status)
    for phase in (SessionPhase.EXECUTING, SessionPhase.UNKNOWN, SessionPhase.TERMINAL)
    for status in (
        ObservationStatus.PENDING,
        ObservationStatus.UNKNOWN,
        ObservationStatus.SUCCEEDED,
    )
]


def invocation_fixture(
    admission: str = "current",
    observed_episode: str = "current",
    correlation: str = "exact",
    lifecycle: tuple[SessionPhase, ObservationStatus] | None = None,
    *,
    receipt_kind: str = "exact",
) -> CoreState:
    phase, status = lifecycle or (SessionPhase.EXECUTING, ObservationStatus.SUCCEEDED)
    state = initial_state()
    scope = Scope(owner=AttemptId(root="owner"), generation=0)
    episode = None if admission == "absent" else DecisionId(root="current")
    spec = SessionSpec(
        session_id=SessionId(root="session"),
        role_id=RoleId(root="writer"),
        policy="fresh",
        lifetime="owner",
        access=Access.WRITE_CANDIDATE,
    )
    ref = InvocationRef(
        session_id=spec.session_id, invocation_id=InvocationId(root="writer"), generation=0
    )
    turn = TurnSpec(
        session=spec,
        invocation_id=ref.invocation_id,
        workspace=scope,
        prompts=(),
        output_schema=SchemaRef(name="output", version=1),
        charge_class="paid",
    )
    request = DispatchTurn(
        request_id=RequestId(root="turn"),
        scope=scope,
        admission_id=episode,
        decision_id=DecisionId(root="request-turn"),
        turn=turn,
    )
    assert request.request_id is not None
    assert request.decision_id is not None
    assert isinstance(scope.owner, AttemptId)
    observed = (
        None
        if observed_episode == "absent"
        else Observation(
            event_id=EventId(root="observed"),
            request_id=RequestId(root="unrelated")
            if correlation == "unrelated"
            else request.request_id,
            scope=scope,
            sequence=1,
            observed_at=1,
            status=status,
            accepted=status != ObservationStatus.REJECTED,
            terminal=status in (ObservationStatus.SUCCEEDED, ObservationStatus.REJECTED),
            admission_id=DecisionId(root="old") if observed_episode == "old" else episode,
        )
    )
    invocation = Invocation(
        until=Until(at=100.0),
        invocation=ref,
        scope=scope,
        turn=turn,
        phase=phase,
        observation=observed,
    )
    owner = AttemptView(
        attempt_id=scope.owner,
        item_id=ItemId(root="item"),
        generation=0,
        phase=AttemptPhase.ACTIVE,
        admission_id=episode,
        workspace=WorkspacePlan(mode=WorkspaceMode.ISOLATED_CHILD, base=state.run.facts.baseline),
        budget=AttemptBudget(paid_invocation_limit=2, refund_limit=2),
    )
    session = SessionView(
        spec=spec,
        scope=scope,
        generation=0,
        phase=SessionPhase.EXECUTING,
        invocation=ref.invocation_id,
        resource_id=ResourceId(root="conversation"),
    )
    record = RequestRecord(
        request_id=request.request_id,
        request=request,
        payload_digest="turn",
        lifecycle=LifecycleClass.SESSION_TURN,
        phase=RequestPhase.COMPLETED,
        observation=observed if correlation == "exact" else None,
        until=Until(at=1000),
    )
    decision = RequestTurn(decision_id=request.decision_id, scope=scope, turn=turn)
    if receipt_kind == "mismatched":
        decision = decision.model_copy(
            update={"turn": turn.model_copy(update={"invocation_id": InvocationId(root="foreign")})}
        )
    receipt = DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=sha256(
            json.dumps(
                decision.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest(),
        feedback=(
            Rejected(
                decision_id=decision.decision_id,
                code=RejectionCode.BUDGET,
                path=("decision",),
                detail="denied",
            )
            if receipt_kind == "rejected"
            else Accepted(decision_id=decision.decision_id)
        ),
        request_ids=(request.request_id,),
    )
    claim = InterruptClaim(
        invocation=ref,
        authority=RequestId(root="interrupt"),
        refund=1,
        phase="draining",
        checkpoint_authority=RequestId(root="checkpoint"),
    )
    if receipt_kind == "foreign-request-decision":
        request = request.model_copy(update={"decision_id": DecisionId(root="foreign")})
        record = record.model_copy(update={"request": request})
    elif receipt_kind == "missing-request-membership":
        receipt = receipt.model_copy(update={"request_ids": ()})
    elif receipt_kind == "foreign-session-scope":
        session = session.model_copy(
            update={"scope": Scope(owner=AttemptId(root="foreign"), generation=0)}
        )
    state = state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "sessions": state.sessions.model_copy(
                update={
                    "sessions": (session,),
                    "invocations": (invocation,),
                    "interrupts": (claim,),
                    "acquisition_groups": ()
                    if episode is None
                    else (
                        SessionAcquisitionGroup(
                            attempt=AttemptRef(attempt_id=owner.attempt_id, generation=0),
                            admission_id=episode,
                            scope=scope,
                            session_ids=(spec.session_id,),
                            phase="ready",
                        ),
                    ),
                }
            ),
            "records": state.records.model_copy(
                update={"records": () if correlation == "unregistered" else (record,)}
            ),
            "run": state.run.model_copy(
                update={
                    "receipts": () if receipt_kind == "absent" else (receipt,),
                    "limits": sample_limits(max_turns=10, max_refunds=2),
                }
            ),
        }
    )
    return CoreState.model_validate_json(state.model_dump_json())


def context(state: CoreState) -> AttemptsContext:
    return AttemptsContext(
        run=state.run,
        scheduling=state.scheduling,
        sessions=state.sessions,
        evaluation=state.evaluation,
        settlement=state.settlement,
        records=state.records,
    )


def checkpoint_event(state: CoreState) -> InvocationCheckpointRequested:
    owner = state.attempts.attempts[0]
    return InvocationCheckpointRequested(
        attempt=AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation),
        invocation=state.sessions.invocations[0].invocation,
        retention="wip",
        authority=RequestId(root="checkpoint"),
    )


@pytest.mark.parametrize(
    ("status", "correlation", "admission", "observed_episode", "receipt_kind"),
    guard_rows(
        STATUSES,
        CORRELATIONS,
        ["absent", "current"],
        EPISODES,
        RECEIPTS,
        positive=(ObservationStatus.SUCCEEDED, "exact", "current", "current", "exact"),
    ),
)
def test_checkpoint_requires_exact_current_terminal_invocation_proof(
    status: ObservationStatus,
    correlation: str,
    admission: str,
    observed_episode: str,
    receipt_kind: str,
) -> None:
    state = invocation_fixture(
        admission,
        observed_episode,
        correlation,
        (SessionPhase.EXECUTING, status),
        receipt_kind=receipt_kind,
    )
    owner = state.attempts.attempts[0]
    paid = ChargeReceipt(
        charge_id=ChargeId(root="paid"),
        kind=ChargeKind.ATTEMPT,
        charged=1,
        invocation_id=state.sessions.invocations[0].invocation.invocation_id,
    )
    state = state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner.model_copy(update={"charges": (paid,)}),))
        }
    )
    result = advance_attempt(state.attempts, context(state), checkpoint_event(state))
    proven = (
        receipt_kind == "exact"
        and admission == "current"
        and observed_episode == "current"
        and correlation == "exact"
    )
    assert (
        any(isinstance(outbound.request, SnapshotAndRetain) for outbound in result.requests)
        == proven
    )
    if not proven:
        assert result.state == state.attempts


@pytest.mark.parametrize(
    ("status", "correlation", "observed_episode", "receipt_kind"),
    guard_rows(
        STATUSES,
        CORRELATIONS,
        EPISODES,
        RECEIPTS,
        positive=(ObservationStatus.SUCCEEDED, "exact", "current", "exact"),
    ),
)
def test_refund_requires_exact_current_terminal_invocation_proof(
    status: ObservationStatus,
    correlation: str,
    observed_episode: str,
    receipt_kind: str,
) -> None:
    state = invocation_fixture(
        observed_episode=observed_episode,
        correlation=correlation,
        lifecycle=(SessionPhase.TERMINAL, status),
        receipt_kind=receipt_kind,
    )
    owner = state.attempts.attempts[0]
    ref = state.sessions.invocations[0].invocation
    paid = ChargeReceipt(
        charge_id=ChargeId(root="paid"),
        kind=ChargeKind.ATTEMPT,
        charged=1,
        invocation_id=ref.invocation_id,
    )
    checkpoint = AttemptCheckpoint(
        invocation=ref,
        request_id=RequestId(root="checkpoint"),
        revision=state.run.facts.baseline,
        retention="wip",
    )
    state = state.model_copy(
        update={
            "attempts": AttemptsState(
                attempts=(
                    owner.model_copy(update={"charges": (paid,), "checkpoints": (checkpoint,)}),
                )
            ),
            "sessions": state.sessions.model_copy(
                update={
                    "interrupts": (
                        state.sessions.interrupts[0].model_copy(update={"phase": "checkpointed"}),
                    )
                }
            ),
        }
    )
    event = AttemptChargeRefundRequested(
        attempt=AttemptRef(attempt_id=owner.attempt_id, generation=0),
        charge_id=paid.charge_id,
        amount=1,
        reason="interrupted",
        authority=RequestId(root="interrupt"),
        checkpoint_authority=checkpoint.request_id,
    )
    result = advance_attempt(state.attempts, context(state), event)
    proven = receipt_kind == "exact" and observed_episode == "current" and correlation == "exact"
    assert sum(receipt.refunded for receipt in result.state.attempts[0].charges) == int(proven)
    if not proven:
        assert result.state == state.attempts


@pytest.mark.parametrize(
    ("receipt_kind", "admission", "observed_episode", "correlation", "lifecycle"),
    # The rule reads the episode, the correlation and the status together.
    guard_rows(
        RECEIPTS,
        ["absent", "current"],
        EPISODES,
        CORRELATIONS,
        LIFECYCLES,
        positive=(
            "exact",
            "current",
            "current",
            "exact",
            (SessionPhase.EXECUTING, ObservationStatus.SUCCEEDED),
        ),
        joint=(2, 3, 4),
    ),
)
def test_billing_requires_current_unambiguous_invocation_evidence(
    admission: str,
    observed_episode: str,
    correlation: str,
    lifecycle: tuple[SessionPhase, ObservationStatus],
    *,
    receipt_kind: str,
) -> None:
    phase, status = lifecycle
    state = invocation_fixture(
        admission, observed_episode, correlation, lifecycle, receipt_kind=receipt_kind
    )
    owner = state.attempts.attempts[0]
    event = InvocationChargeRequested(
        attempt=AttemptRef(attempt_id=owner.attempt_id, generation=0),
        invocation=state.sessions.invocations[0].invocation,
    )
    result = advance_attempt(state.attempts, context(state), event)
    proven = (
        receipt_kind == "exact"
        and admission == "current"
        and phase == SessionPhase.EXECUTING
        and (
            correlation == "exact" or (observed_episode == "absent" and correlation == "unrelated")
        )
        and observed_episode in ("absent", "current")
        and (observed_episode == "absent" or status != ObservationStatus.UNKNOWN)
    )
    charges = result.state.attempts[0].charges
    assert sum(row.charged for row in charges if row.kind == ChargeKind.TURN) == int(proven)
    assert sum(row.charged for row in charges if row.kind == ChargeKind.ATTEMPT) == int(proven)
    if not proven:
        assert result.state == state.attempts


@pytest.mark.parametrize("copies", [2, 3])
def test_ambiguous_charge_identity_never_refunds_multiple_receipts(copies: int) -> None:
    """Persisted duplicate charge IDs cannot multiply one interruption refund."""
    state = invocation_fixture(lifecycle=(SessionPhase.TERMINAL, ObservationStatus.SUCCEEDED))
    owner = state.attempts.attempts[0]
    ref = state.sessions.invocations[0].invocation
    charge = ChargeReceipt(
        charge_id=ChargeId(root="paid"),
        kind=ChargeKind.ATTEMPT,
        charged=1,
        invocation_id=ref.invocation_id,
    )
    checkpoint = AttemptCheckpoint(
        invocation=ref,
        request_id=RequestId(root="checkpoint"),
        revision=state.run.facts.baseline,
        retention="wip",
    )
    state = state.model_copy(
        update={
            "attempts": AttemptsState(
                attempts=(
                    owner.model_copy(
                        update={
                            "charges": (charge,) * copies,
                            "checkpoints": (checkpoint,),
                            "budget": owner.budget.model_copy(update={"refund_limit": 1}),
                        }
                    ),
                )
            ),
            "sessions": state.sessions.model_copy(
                update={
                    "interrupts": (
                        state.sessions.interrupts[0].model_copy(update={"phase": "checkpointed"}),
                    )
                }
            ),
            "run": state.run.model_copy(
                update={"limits": sample_limits(max_turns=10, max_refunds=1)}
            ),
        }
    )
    state = CoreState.model_validate_json(state.model_dump_json())
    event = AttemptChargeRefundRequested(
        attempt=AttemptRef(attempt_id=owner.attempt_id, generation=0),
        charge_id=charge.charge_id,
        amount=1,
        reason="interrupted",
        authority=RequestId(root="interrupt"),
        checkpoint_authority=checkpoint.request_id,
    )
    result = advance_attempt(state.attempts, context(state), event)
    assert result.state == state.attempts
    assert result.signals == ()
    assert result.events == ()


@pytest.mark.parametrize("limit", [0, 1, 2, 3])
@pytest.mark.parametrize("spent", [0, 1, 2, 3, 4])
def test_a_paid_invocation_is_charged_exactly_while_the_attempt_has_paid_invocations_left(
    limit: int, spent: int
) -> None:
    """The limit is the number of paid invocations an attempt may spend: no more, no fewer."""
    state = invocation_fixture()
    owner = state.attempts.attempts[0]
    earlier = tuple(
        ChargeReceipt(
            charge_id=ChargeId(root=f"earlier-{index}"),
            kind=ChargeKind.ATTEMPT,
            charged=1,
            invocation_id=InvocationId(root=f"earlier-{index}"),
        )
        for index in range(spent)
    )
    state = state.model_copy(
        update={
            "attempts": AttemptsState(
                attempts=(
                    owner.model_copy(
                        update={
                            "charges": earlier,
                            "budget": owner.budget.model_copy(
                                update={"paid_invocation_limit": limit}
                            ),
                        }
                    ),
                )
            )
        }
    )
    event = InvocationChargeRequested(
        attempt=AttemptRef(attempt_id=owner.attempt_id, generation=0),
        invocation=state.sessions.invocations[0].invocation,
    )

    result = advance_attempt(state.attempts, context(state), event)

    paid = sum(
        row.charged for row in result.state.attempts[0].charges if row.kind == ChargeKind.ATTEMPT
    )
    exhausted = [e for e in result.events if isinstance(e, AttemptExhausted)]
    if spent < limit:
        assert paid == spent + 1
        assert exhausted == []
    else:
        assert paid == spent
        assert [e.reason for e in exhausted] == ["paid-limit"]
