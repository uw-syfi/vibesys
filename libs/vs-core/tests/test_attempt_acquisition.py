"""Acquisition/accounting properties through the persisted public state machine."""

from itertools import product

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    Access,
    AttemptAdmitted,
    AttemptBudget,
    AttemptChargeRefundRequested,
    AttemptCheckpoint,
    AttemptClosure,
    AttemptId,
    AttemptPhase,
    AttemptRef,
    AttemptRegistered,
    AttemptRequest,
    AttemptSetupFailed,
    AttemptsState,
    AttemptView,
    ChargeId,
    ChargeKind,
    ChargeReceipt,
    ContractError,
    ContractValidationError,
    CoreState,
    DecisionId,
    EnsureWorkspace,
    EventCursor,
    EventId,
    HostFence,
    HostId,
    InitialSessionsReady,
    InspectRequest,
    Intent,
    IntentPhase,
    InterruptClaim,
    Invocation,
    InvocationChargeRequested,
    InvocationCheckpointed,
    InvocationCheckpointRequested,
    InvocationEnded,
    InvocationId,
    InvocationRef,
    ItemId,
    KernelNotImplementedError,
    LifecycleClass,
    Limits,
    Observation,
    ObservationStatus,
    OperationRegistry,
    RequestId,
    ResourceId,
    RoleId,
    RunEnvelope,
    SchemaRef,
    Scope,
    SessionAcquisitionGroup,
    SessionId,
    SessionPhase,
    SessionSpec,
    SessionView,
    SetupFailureKind,
    SnapshotAndRetain,
    StrategyState,
    TurnSpec,
    WorkspaceMode,
    WorkspaceObserved,
    WorkspacePlan,
    initial_state,
    project,
    step,
)


def reload_state(state: CoreState) -> CoreState:
    """Crash/reload through the public envelope codec, including strict nested state."""
    envelope = RunEnvelope[StrategyState](
        schema_version=ENVELOPE_SCHEMA_VERSION,
        fence=HostFence(host_id=HostId(root="host"), epoch=1),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=StrategyState(schema_version=1),
        event_cursor=EventCursor(sequence=state.revision),
    )
    codec = OperationRegistry()
    return codec.decode_envelope(RunEnvelope[StrategyState], codec.encode_envelope(envelope)).core


def registration(identity: str, charge: int = 1) -> AttemptRegistered:
    state = initial_state()
    return AttemptRegistered(
        request=AttemptRequest(
            decision_id=DecisionId(root=identity),
            attempt_id=AttemptId(root=identity),
            item_id=ItemId(root=identity),
            generation=0,
            admission_charge=charge,
        ),
        workspace=WorkspacePlan(mode=WorkspaceMode.ISOLATED_CHILD, base=state.run.facts.baseline),
        budget=AttemptBudget(admission_charge=charge),
    )


@given(st.lists(st.integers(min_value=0, max_value=5), min_size=1, max_size=25))
def test_queued_registration_replay_charges_once_and_reload_is_identical(order: list[int]) -> None:
    """K4 queued admission is charged before capacity, even with reordered duplicates."""
    state = initial_state()
    seen: set[int] = set()
    for identity in order:
        event = registration(str(identity))
        before = state.model_dump_json()
        result = step(state, event)
        assert result == step(reload_state(state), event)
        assert state.model_dump_json() == before
        state = reload_state(result.state)
        seen.add(identity)
        attempts = project(state).attempts
        assert len(attempts) == len(seen)
        assert sum(receipt.charged for attempt in attempts for receipt in attempt.charges) == len(
            seen
        )
        assert all(attempt.phase == AttemptPhase.QUEUED for attempt in attempts)
        assert all(
            receipt.kind == ChargeKind.ADMISSION
            for attempt in attempts
            for receipt in attempt.charges
        )
        assert result.requests == ()
        assert result.events == ()


def owned_state(phase: AttemptPhase = AttemptPhase.ACQUIRING) -> CoreState:
    """Seed immutable sibling context without invoking another lifecycle leaf."""
    event = registration("owner")
    owner = AttemptView(
        attempt_id=event.request.attempt_id,
        item_id=event.request.item_id,
        generation=0,
        phase=phase,
        workspace=event.workspace,
        budget=AttemptBudget(paid_invocation_limit=2, retry_limit=1, refund_limit=2),
        admission_id=event.request.decision_id,
    )
    return initial_state().model_copy(update={"attempts": AttemptsState(attempts=(owner,))})


def owner_ref() -> AttemptRef:
    return AttemptRef(attempt_id=AttemptId(root="owner"), generation=0)


def observation(
    request_id: RequestId,
    status: ObservationStatus = ObservationStatus.SUCCEEDED,
) -> Observation:
    return Observation(
        event_id=EventId(root="observed"),
        request_id=request_id,
        scope=Scope(owner=AttemptId(root="owner"), generation=0),
        sequence=1,
        observed_at=1.0,
        status=status,
        accepted=True,
        terminal=True,
        admission_id=DecisionId(root="owner"),
    )


def acquiring_state() -> CoreState:
    """Seed the durable workspace request used as observation authority."""
    state = owned_state()
    owner = state.attempts.attempts[0]
    identity = RequestId(root="workspace")
    request = EnsureWorkspace(
        request_id=identity,
        scope=Scope(owner=owner.attempt_id, generation=owner.generation),
        admission_id=owner.admission_id,
        deadline_at=1000.0,
        attempt=owner_ref(),
        plan=owner.workspace,
    )

    intent = Intent(
        request_id=identity,
        request=request,
        payload_digest="workspace-proof",
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
        phase=IntentPhase.DISPATCHED,
        reconcile_deadline_at=1000.0,
    )
    owner = owner.model_copy(update={"pending_intents": (identity,)})
    return state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
        }
    )


def invocation_state(charge_class: str, predecessor: InvocationRef | None = None) -> CoreState:
    state = owned_state(AttemptPhase.ACTIVE)
    scope = Scope(owner=AttemptId(root="owner"), generation=0)
    spec = SessionSpec(
        session_id=SessionId(root="session"),
        role_id=RoleId(root="worker"),
        policy="fresh",
        lifetime="owner",
        access=Access.WRITE_CANDIDATE,
    )
    invocation = Invocation(
        invocation=InvocationRef(
            session_id=spec.session_id, invocation_id=InvocationId(root="invocation"), generation=0
        ),
        scope=scope,
        turn=TurnSpec(
            session=spec,
            invocation_id=InvocationId(root="invocation"),
            workspace=scope,
            prompts=(),
            output_schema=SchemaRef(name="output", version=1),
            deadline_at=1000.0,
            charge_class=charge_class,
            predecessor=predecessor,
        ),
        phase=SessionPhase.EXECUTING,
    )
    session = SessionView(
        spec=spec,
        scope=scope,
        generation=0,
        phase=SessionPhase.EXECUTING,
        invocation=invocation.invocation.invocation_id,
        resource_id=ResourceId(root="conversation"),
    )
    return state.model_copy(
        update={
            "sessions": state.sessions.model_copy(
                update={
                    "invocations": (invocation,),
                    "sessions": (session,),
                    "acquisition_groups": (
                        SessionAcquisitionGroup(
                            attempt=owner_ref(),
                            admission_id=DecisionId(root="owner"),
                            scope=scope,
                            session_ids=(spec.session_id,),
                            phase="ready",
                        ),
                    ),
                }
            ),
            "run": state.run.model_copy(
                update={"limits": Limits(max_turns=10, max_attempts=10, max_retries=5)}
            ),
        }
    )


@pytest.mark.parametrize("charge_class", ["paid", "correction", "resume", "free"])
@pytest.mark.parametrize("predecessor_present", [False, True])
@given(guard=st.sampled_from(("closure", "turn-limit")))
def test_dispatch_guards_cover_every_charge_class_and_optional_predecessor(
    charge_class: str, guard: str, *, predecessor_present: bool
) -> None:
    predecessor = (
        InvocationRef(
            session_id=SessionId(root="session"),
            invocation_id=InvocationId(root="predecessor"),
            generation=0,
        )
        if predecessor_present
        else None
    )
    state = invocation_state(charge_class, predecessor)
    owner = state.attempts.attempts[0]
    if guard == "closure":
        owner = owner.model_copy(
            update={
                "closure": AttemptClosure(
                    disposition="cancel",
                    requested_at=0.0,
                    authority=RequestId(root="retire"),
                    admission_id=DecisionId(root="owner"),
                )
            }
        )
        state = state.model_copy(update={"attempts": AttemptsState(attempts=(owner,))})
    else:
        state = state.model_copy(
            update={"run": state.run.model_copy(update={"limits": Limits(max_turns=0)})}
        )
    event = InvocationChargeRequested(
        attempt=owner_ref(), invocation=state.sessions.invocations[0].invocation
    )
    result = step(state, event)
    assert result.state.attempts == state.attempts
    assert result.requests == ()


@pytest.mark.parametrize("predecessor_present", [False, True])
def test_paid_budget_guard_also_applies_with_an_optional_predecessor(
    *, predecessor_present: bool
) -> None:
    predecessor = (
        InvocationRef(
            session_id=SessionId(root="session"),
            invocation_id=InvocationId(root="predecessor"),
            generation=0,
        )
        if predecessor_present
        else None
    )
    state = invocation_state("paid", predecessor)
    owner = state.attempts.attempts[0].model_copy(
        update={"budget": AttemptBudget(paid_invocation_limit=0)}
    )
    state = state.model_copy(update={"attempts": AttemptsState(attempts=(owner,))})
    result = step(
        state,
        InvocationChargeRequested(
            attempt=owner_ref(), invocation=state.sessions.invocations[0].invocation
        ),
    )
    assert result.state.attempts == state.attempts
    assert result.requests == ()


@pytest.mark.parametrize("charge_class", ["paid", "correction", "resume", "free"])
@pytest.mark.parametrize("predecessor_present", [False, True])
def test_retry_authority_is_required_for_all_predecessor_variants(
    charge_class: str, *, predecessor_present: bool
) -> None:
    predecessor = (
        InvocationRef(
            session_id=SessionId(root="session"),
            invocation_id=InvocationId(root="prior"),
            generation=0,
        )
        if predecessor_present
        else None
    )
    # Free without a predecessor has no retry requirement; verify its valid
    # charge reaches sessions rather than mistaking the stub for an A failure.
    state = invocation_state(charge_class, predecessor)
    owner = state.attempts.attempts[0].model_copy(update={"budget": AttemptBudget(retry_limit=0)})
    state = state.model_copy(update={"attempts": AttemptsState(attempts=(owner,))})
    event = InvocationChargeRequested(
        attempt=owner_ref(), invocation=state.sessions.invocations[0].invocation
    )
    if not predecessor_present and charge_class in ("paid", "free"):
        with pytest.raises(KernelNotImplementedError) as raised:
            step(state, event)
        assert raised.value.subarea == "_session_turns"
        assert raised.value.event_kind == "invocation_charges_authorized"
        return
    result = step(state, event)
    assert result.state.attempts == state.attempts
    assert result.requests == ()


@pytest.mark.parametrize("charge_class", ["paid", "correction"])
@given(chain_classes=st.lists(st.sampled_from(("paid", "correction")), min_size=1, max_size=5))
def test_mixed_predecessor_chain_cannot_reset_the_retry_bound(
    charge_class: str, chain_classes: list[str]
) -> None:
    state = invocation_state(charge_class)
    target = state.sessions.invocations[0]
    previous: InvocationRef | None = None
    chain: list[Invocation] = []
    for index, previous_class in enumerate(("paid", *chain_classes)):
        ref = target.invocation.model_copy(
            update={"invocation_id": InvocationId(root=f"prior-{index}")}
        )
        turn = target.turn.model_copy(
            update={
                "invocation_id": ref.invocation_id,
                "charge_class": previous_class,
                "predecessor": previous,
            }
        )
        chain.append(
            target.model_copy(
                update={
                    "invocation": ref,
                    "turn": turn,
                    "phase": SessionPhase.TERMINAL,
                    "observation": observation(RequestId(root=f"prior-{index}")),
                }
            )
        )
        previous = ref
    target = target.model_copy(
        update={"turn": target.turn.model_copy(update={"predecessor": previous})}
    )
    owner = state.attempts.attempts[0].model_copy(
        update={"budget": AttemptBudget(paid_invocation_limit=10, retry_limit=len(chain_classes))}
    )
    state = state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "sessions": state.sessions.model_copy(update={"invocations": (*chain, target)}),
        }
    )
    result = step(
        state, InvocationChargeRequested(attempt=owner_ref(), invocation=target.invocation)
    )
    assert result.state.attempts == state.attempts
    assert result.requests == ()


@pytest.mark.parametrize("charge_class", ["paid", "correction", "resume", "free"])
@pytest.mark.parametrize("predecessor_present", [False, True])
@pytest.mark.parametrize("mismatch", ["resource", "invocation", "session", "workspace"])
def test_dispatch_requires_exact_invocation_workspace_and_session_lease(
    charge_class: str, mismatch: str, *, predecessor_present: bool
) -> None:
    predecessor = (
        InvocationRef(
            session_id=SessionId(root="session"),
            invocation_id=InvocationId(root="previous"),
            generation=0,
        )
        if predecessor_present
        else None
    )
    state = invocation_state(charge_class, predecessor)
    invocation = state.sessions.invocations[0]
    session = state.sessions.sessions[0]
    if mismatch == "resource":
        reused = session.spec.model_copy(update={"policy": "reuse"})
        session = session.model_copy(update={"resource_id": None, "spec": reused})
        invocation = invocation.model_copy(
            update={"turn": invocation.turn.model_copy(update={"session": reused})}
        )
    elif mismatch == "invocation":
        invocation = invocation.model_copy(
            update={
                "turn": invocation.turn.model_copy(
                    update={"invocation_id": InvocationId(root="foreign")}
                )
            }
        )
    elif mismatch == "session":
        spec = session.spec.model_copy(update={"session_id": SessionId(root="foreign")})
        invocation = invocation.model_copy(
            update={"turn": invocation.turn.model_copy(update={"session": spec})}
        )
        session = session.model_copy(update={"spec": spec})
    else:
        invocation = invocation.model_copy(
            update={
                "turn": invocation.turn.model_copy(
                    update={"workspace": Scope(owner=AttemptId(root="foreign"), generation=0)}
                )
            }
        )
    state = state.model_copy(
        update={
            "sessions": state.sessions.model_copy(
                update={"sessions": (session,), "invocations": (invocation,)}
            )
        }
    )
    result = step(
        state, InvocationChargeRequested(attempt=owner_ref(), invocation=invocation.invocation)
    )
    assert result.state.attempts == state.attempts
    assert result.requests == ()


@pytest.mark.parametrize("policy", ["fresh", "reuse"])
@pytest.mark.parametrize("resource_present", [False, True])
@pytest.mark.parametrize("charge_class", ["paid", "free"])
def test_reused_conversations_require_resource_correspondence(
    policy: str, charge_class: str, *, resource_present: bool
) -> None:
    state = invocation_state(charge_class)
    invocation = state.sessions.invocations[0]
    session = state.sessions.sessions[0]
    spec = session.spec.model_copy(update={"policy": policy})
    session = session.model_copy(
        update={
            "spec": spec,
            "resource_id": ResourceId(root="conversation") if resource_present else None,
        }
    )
    invocation = invocation.model_copy(
        update={"turn": invocation.turn.model_copy(update={"session": spec})}
    )
    state = state.model_copy(
        update={
            "sessions": state.sessions.model_copy(
                update={"sessions": (session,), "invocations": (invocation,)}
            )
        }
    )
    event = InvocationChargeRequested(attempt=owner_ref(), invocation=invocation.invocation)
    if policy == "reuse" and not resource_present:
        result = step(state, event)
        assert result.state.attempts == state.attempts
        assert result.requests == ()
    else:
        with pytest.raises(KernelNotImplementedError) as raised:
            step(state, event)
        assert raised.value.subarea == "_session_turns"
        assert raised.value.event_kind == "invocation_charges_authorized"


@pytest.mark.parametrize("mode", list(WorkspaceMode))
@pytest.mark.parametrize("predecessor_present", [False, True])
@pytest.mark.parametrize(
    "phase",
    [AttemptPhase.ACQUIRING, AttemptPhase.ACTIVE, AttemptPhase.CLOSING, AttemptPhase.BLOCKED],
)
def test_root_admission_guard_is_independent_of_optional_parked_predecessor(
    mode: WorkspaceMode, phase: AttemptPhase, *, predecessor_present: bool
) -> None:
    state = owned_state(phase)
    owner = state.attempts.attempts[0].model_copy(
        update={
            "workspace": WorkspacePlan(
                mode=WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
            )
        }
    )
    state = state.model_copy(update={"attempts": AttemptsState(attempts=(owner,))})
    event = registration("next")
    event = AttemptAdmitted(
        admission_id=event.request.decision_id,
        request=event.request,
        workspace=WorkspacePlan(
            mode=mode,
            base=state.run.facts.baseline,
            parked_predecessor=owner_ref() if predecessor_present else None,
        ),
        budget=event.budget,
    )
    if predecessor_present or mode == WorkspaceMode.EXCLUSIVE_ROOT:
        with pytest.raises(ContractValidationError, match=r"workspace|parked_predecessor"):
            step(state, event)
    else:
        result = step(state, event)
        assert len(result.requests) == 1
        assert isinstance(result.requests[0], EnsureWorkspace)


def checkpoint_state() -> CoreState:
    state = invocation_state("paid")
    invocation = state.sessions.invocations[0].model_copy(
        update={"phase": SessionPhase.TERMINAL, "observation": observation(RequestId(root="turn"))}
    )
    receipt = ChargeReceipt(
        charge_id=ChargeId(root="paid"),
        kind=ChargeKind.ATTEMPT,
        charged=1,
        invocation_id=invocation.invocation.invocation_id,
        source_request=RequestId(root="turn"),
    )
    owner = state.attempts.attempts[0].model_copy(update={"charges": (receipt,)})
    claim = InterruptClaim(
        invocation=invocation.invocation,
        authority=RequestId(root="interrupt"),
        refund=1,
        phase="draining",
        checkpoint_authority=RequestId(root="checkpoint"),
    )
    return state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "sessions": state.sessions.model_copy(
                update={"invocations": (invocation,), "interrupts": (claim,)}
            ),
        }
    )


@pytest.mark.parametrize("proof", ["absent", "unknown", "pending", "nonterminal", "terminal"])
@pytest.mark.parametrize("access", list(Access))
@given(retention=st.sampled_from(("wip", "candidate")))
def test_checkpoint_waits_for_every_competing_writer_to_terminate(
    proof: str, retention: str, access: Access
) -> None:
    state = checkpoint_state()
    target = state.sessions.invocations[0]
    competing = target.model_copy(
        update={
            "invocation": InvocationRef(
                session_id=SessionId(root="other"),
                invocation_id=InvocationId(root="other"),
                generation=0,
            ),
            "observation": None,
        }
    )
    competing = competing.model_copy(
        update={
            "turn": competing.turn.model_copy(
                update={"session": competing.turn.session.model_copy(update={"access": access})}
            )
        }
    )
    if proof != "absent":
        status = {"unknown": ObservationStatus.UNKNOWN, "pending": ObservationStatus.PENDING}.get(
            proof, ObservationStatus.SUCCEEDED
        )
        competing = competing.model_copy(
            update={
                "observation": observation(RequestId(root="other"), status).model_copy(
                    update={"terminal": proof == "terminal"}
                )
            }
        )
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"invocations": (target, competing)})}
    )
    event = InvocationCheckpointRequested(
        attempt=owner_ref(),
        invocation=target.invocation,
        retention=retention,
        authority=RequestId(root="checkpoint"),
    )
    result = step(state, event)
    assert result == step(reload_state(state), event)
    assert result.state.attempts.attempts[0].checkpoints == ()
    if (proof == "terminal" or access != Access.WRITE_CANDIDATE) and retention == "wip":
        assert len(result.requests) == 1
        assert isinstance(result.requests[0], SnapshotAndRetain)
    else:
        assert result.requests == ()


def test_checkpoint_signal_requires_committed_retention_not_its_request() -> None:
    """K4 interruption boundary: writer proof, durable WIP intent, durable retain proof."""
    state = checkpoint_state()
    invocation = state.sessions.invocations[0].invocation
    requested = InvocationCheckpointRequested(
        attempt=owner_ref(),
        invocation=invocation,
        retention="wip",
        authority=RequestId(root="checkpoint"),
    )
    prepared = step(state, requested)
    assert len(prepared.requests) == 1
    assert prepared.state.attempts.attempts[0].checkpoints == ()
    assert prepared.events == ()
    state = reload_state(prepared.state)
    event = InvocationCheckpointed(
        attempt=owner_ref(),
        revision=state.run.facts.baseline,
        charge=state.attempts.attempts[0].charges[0],
        invocation=invocation,
        checkpoint_request=prepared.requests[0].request_id,
    )
    premature = step(state, event)
    assert premature.state.attempts.attempts[0].checkpoints == ()
    assert premature.requests == ()
    intent = state.intents.intents[0]
    intent = intent.model_copy(
        update={"phase": IntentPhase.COMPLETED, "observation": observation(intent.request_id)}
    )
    committed = state.model_copy(
        update={"intents": state.intents.model_copy(update={"intents": (intent,)})}
    )
    with pytest.raises(KernelNotImplementedError) as raised:
        step(reload_state(committed), event)
    assert raised.value.event_kind == "invocation_checkpoint_available"
    assert raised.value.subarea == "_session_turns"


@pytest.mark.parametrize("action", ["charge", "checkpoint", "refund", "ended"])
@pytest.mark.parametrize("accepted", [False, True])
def test_unknown_invocation_acceptance_always_inspects(action: str, *, accepted: bool) -> None:
    state = checkpoint_state()
    target = state.sessions.invocations[0]
    observed = observation(RequestId(root="turn"), ObservationStatus.UNKNOWN).model_copy(
        update={"accepted": accepted, "terminal": False}
    )
    target = target.model_copy(update={"observation": observed})
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"invocations": (target,)})}
    )
    events = {
        "charge": InvocationChargeRequested(attempt=owner_ref(), invocation=target.invocation),
        "checkpoint": InvocationCheckpointRequested(
            attempt=owner_ref(),
            invocation=target.invocation,
            retention="wip",
            authority=RequestId(root="checkpoint"),
        ),
        "refund": AttemptChargeRefundRequested(
            attempt=owner_ref(),
            charge_id=state.attempts.attempts[0].charges[0].charge_id,
            amount=1,
            reason="interrupted",
            authority=RequestId(root="interrupt"),
            checkpoint_authority=RequestId(root="checkpoint"),
        ),
        "ended": InvocationEnded(
            attempt=owner_ref(), invocation=target.invocation, observation=observed
        ),
    }
    result = step(state, events[action])
    assert result.state.attempts == state.attempts
    assert len(result.requests) == 1
    assert isinstance(result.requests[0], InspectRequest)
    assert result.requests[0].target == observed.request_id


@pytest.mark.parametrize("refunded_charge", [None, "paid", "other"])
@given(duplicate_count=st.integers(min_value=1, max_value=5))
def test_a_refund_authority_can_credit_only_one_recorded_receipt(
    refunded_charge: str | None, duplicate_count: int
) -> None:
    state = checkpoint_state()
    owner = state.attempts.attempts[0]
    original = owner.charges[0].model_copy(
        update={"refunded": 1, "refund_sources": (RequestId(root="interrupt"),)}
    )
    second = original.model_copy(
        update={"charge_id": ChargeId(root="second"), "refunded": 0, "refund_sources": ()}
    )
    checkpoint = AttemptCheckpoint(
        invocation=state.sessions.invocations[0].invocation,
        request_id=RequestId(root="checkpoint"),
        revision=state.run.facts.baseline,
        retention="wip",
    )
    owner = owner.model_copy(update={"charges": (original, second), "checkpoints": (checkpoint,)})
    claim = state.sessions.interrupts[0].model_copy(
        update={
            "phase": "checkpointed",
            "refunded_charge": ChargeId(root=refunded_charge) if refunded_charge else None,
        }
    )
    state = state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "sessions": state.sessions.model_copy(update={"interrupts": (claim,)}),
            "run": state.run.model_copy(update={"limits": Limits(max_turns=10, max_refunds=2)}),
        }
    )
    event = AttemptChargeRefundRequested(
        attempt=owner_ref(),
        charge_id=second.charge_id,
        amount=1,
        reason="interrupted",
        authority=claim.authority,
        checkpoint_authority=checkpoint.request_id,
    )
    for _ in range(duplicate_count):
        result = step(reload_state(state), event)
        assert result.state.attempts == state.attempts
        assert result.requests == ()
        state = result.state


@given(st.integers(min_value=0, max_value=5), st.integers(min_value=0, max_value=3))
def test_slot_admission_records_workspace_intent_without_a_second_admission_charge(
    charge: int, duplicate_count: int
) -> None:
    event = registration("owner", charge)
    state = step(initial_state(), event).state
    admitted = AttemptAdmitted(
        admission_id=event.request.decision_id,
        request=event.request,
        workspace=event.workspace,
        budget=event.budget,
    )
    result = step(state, admitted)
    assert result == step(reload_state(state), admitted)
    assert len(result.requests) == 1
    request = result.requests[0]
    assert isinstance(request, EnsureWorkspace)
    owner = project(result.state).attempts[0]
    assert owner.phase == AttemptPhase.ACQUIRING
    assert sum(receipt.charged for receipt in owner.charges) == charge
    assert request.request_id in owner.pending_intents
    assert request.admission_id == owner.admission_id
    assert any(intent.request_id == request.request_id for intent in result.state.intents.intents)
    for _ in range(duplicate_count):
        repeated = step(reload_state(result.state), admitted)
        assert repeated.requests == ()
        assert repeated.state.attempts == result.state.attempts
        result = repeated


@pytest.mark.parametrize(
    "proof",
    product(
        (None, "owner", "old"),
        (0, 1),
        (False, True),
        (False, True),
        tuple(ObservationStatus),
        (False, True),
    ),
)
def test_workspace_never_readies_without_exact_positive_current_episode_proof(
    proof: tuple[str | None, int, bool, bool, ObservationStatus, bool],
) -> None:
    episode, generation, accepted, terminal, status, revision_present = proof
    state = acquiring_state()
    observed = observation(RequestId(root="workspace"), status).model_copy(
        update={
            "admission_id": DecisionId(root=episode) if episode else None,
            "scope": Scope(owner=AttemptId(root="owner"), generation=generation),
            "accepted": accepted,
            "terminal": terminal,
        }
    )
    if (
        episode == "owner"
        and generation == 0
        and accepted
        and terminal
        and status == ObservationStatus.SUCCEEDED
        and revision_present
    ):
        return
    event = WorkspaceObserved(
        attempt=owner_ref(),
        observation=observed,
        revision=state.run.facts.baseline if revision_present else None,
    )
    if generation != 0 or episode == "old":
        with pytest.raises(ContractError, match=r"scope|admission_id"):
            step(state, event)
        return
    if (
        episode == "owner"
        and terminal
        and status
        in (ObservationStatus.FAILED, ObservationStatus.REJECTED, ObservationStatus.CANCELLED)
    ):
        with pytest.raises(KernelNotImplementedError) as raised:
            step(state, event)
        assert raised.value.subarea == "_attempt_retirement"
        assert raised.value.event_kind == "retire_requested"
        return
    result = step(state, event)
    assert project(result.state).attempts[0].phase != AttemptPhase.ACTIVE
    assert result.events == ()


@given(episode=st.sampled_from((None, "old")), generation=st.sampled_from((0, 1)))
def test_stale_initial_session_readiness_cannot_activate_a_new_episode(
    episode: str | None, generation: int
) -> None:
    state = acquiring_state()
    event = InitialSessionsReady(
        attempt=owner_ref().model_copy(update={"generation": generation}),
        admission_id=DecisionId(root=episode or "missing"),
        session_ids=(SessionId(root="session"),),
    )
    result = step(state, event)
    assert result.state.attempts == state.attempts
    assert result.requests == ()


@pytest.mark.parametrize("failure", tuple(SetupFailureKind))
@given(order=st.lists(st.integers(min_value=1, max_value=4), min_size=1, max_size=12))
def test_setup_failure_charges_one_cycle_not_each_failed_member(
    failure: SetupFailureKind, order: list[int]
) -> None:
    """Legacy workspace-creation failure spends one paid retry; Unknown spends none."""
    state = acquiring_state()
    for sequence in order:
        observed = observation(RequestId(root="workspace"), ObservationStatus.FAILED).model_copy(
            update={"sequence": sequence, "accepted": False}
        )
        event = AttemptSetupFailed(attempt=owner_ref(), observation=observed, failure=failure)
        if failure != SetupFailureKind.UNKNOWN:
            # Setup cancellation reaches B only after A records the cycle receipt.
            with pytest.raises(KernelNotImplementedError) as raised:
                step(state, event)
            assert raised.value.subarea == "_attempt_retirement"
            assert raised.value.event_kind == "retire_requested"
            continue
        result = step(state, event)
        assert result == step(reload_state(state), event)
        state = reload_state(result.state)
        paid = tuple(
            receipt
            for receipt in project(state).attempts[0].charges
            if receipt.kind == ChargeKind.ATTEMPT
        )
        assert len(paid) <= 1
        if failure == SetupFailureKind.UNKNOWN:
            assert paid == ()
            assert all(isinstance(request, InspectRequest) for request in result.requests)


@pytest.mark.parametrize("failure", tuple(SetupFailureKind))
@pytest.mark.parametrize("accepted", [False, True])
def test_unknown_setup_acceptance_requests_inspection_without_new_charge(
    failure: SetupFailureKind, *, accepted: bool
) -> None:
    state = acquiring_state()
    observed = observation(RequestId(root="workspace"), ObservationStatus.UNKNOWN).model_copy(
        update={"accepted": accepted, "terminal": False}
    )
    result = step(
        state,
        AttemptSetupFailed(attempt=owner_ref(), observation=observed, failure=failure),
    )
    assert result.state.attempts.attempts[0].charges == ()
    assert len(result.requests) == 1
    assert isinstance(result.requests[0], InspectRequest)
    assert result.requests[0].target == observed.request_id


@pytest.mark.parametrize("failure", list(SetupFailureKind))
@given(sequence=st.integers(min_value=0, max_value=100))
def test_setup_failure_cannot_mint_a_charge_for_an_untracked_request(
    failure: SetupFailureKind, sequence: int
) -> None:
    state = owned_state()
    observed = observation(RequestId(root="never-prepared"), ObservationStatus.FAILED).model_copy(
        update={"sequence": sequence, "accepted": False}
    )
    result = step(
        state, AttemptSetupFailed(attempt=owner_ref(), observation=observed, failure=failure)
    )
    assert result.state.attempts == state.attempts
    assert result.requests == ()


@given(st.sampled_from(tuple(AttemptPhase)))
def test_absent_invocation_cannot_mint_accounting_authority(phase: AttemptPhase) -> None:
    state = owned_state(phase)
    event = InvocationChargeRequested(
        attempt=owner_ref(),
        invocation=InvocationRef(
            session_id=SessionId(root="missing"),
            invocation_id=InvocationId(root="missing"),
            generation=0,
        ),
    )
    result = step(state, event)
    assert result.state.attempts == state.attempts
    assert result.requests == ()


@given(st.sampled_from(("wip", "candidate")))
def test_checkpoint_requires_writer_termination_proof(retention: str) -> None:
    state = owned_state(AttemptPhase.ACTIVE)
    event = InvocationCheckpointRequested(
        attempt=owner_ref(),
        invocation=InvocationRef(
            session_id=SessionId(root="missing"),
            invocation_id=InvocationId(root="missing"),
            generation=0,
        ),
        retention=retention,
        authority=RequestId(root="checkpoint"),
    )
    result = step(state, event)
    assert result.requests == ()
    assert result.state.attempts.attempts[0].checkpoints == ()


@pytest.mark.parametrize(
    "proof",
    product(
        tuple(ChargeKind),
        ("interrupted", "unsupported"),
        (False, True),
        (False, True),
        (False, True),
    ),
)
@given(amount=st.integers(min_value=0, max_value=4))
def test_refund_never_uses_amount_or_optional_identifiers_as_proof(
    proof: tuple[ChargeKind, str, bool, bool, bool],
    amount: int,
) -> None:
    kind, reason, invocation_present, source_present, checkpoint_present = proof
    state = owned_state(AttemptPhase.ACTIVE)
    receipt = ChargeReceipt(
        charge_id=ChargeId(root="charge"),
        kind=kind,
        invocation_id=InvocationId(root="invocation") if invocation_present else None,
        source_request=RequestId(root="source") if source_present else None,
        charged=2,
    )
    owner = state.attempts.attempts[0].model_copy(update={"charges": (receipt,)})
    state = state.model_copy(update={"attempts": AttemptsState(attempts=(owner,))})
    event = AttemptChargeRefundRequested(
        attempt=owner_ref(),
        charge_id=receipt.charge_id,
        amount=amount,
        reason=reason,
        authority=RequestId(root="refund"),
        checkpoint_authority=RequestId(root="checkpoint") if checkpoint_present else None,
    )
    result = step(state, event)
    assert result == step(reload_state(state), event)
    assert result.state.attempts == state.attempts
    assert result.requests == ()
