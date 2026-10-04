"""Acquisition/accounting properties through the persisted public state machine."""

import json
from hashlib import sha256
from itertools import product
from typing import ClassVar, Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import BaseModel

from vs_core.api import (
    ENVELOPE_SCHEMA_VERSION,
    Accepted,
    Access,
    AttemptAdmitted,
    AttemptBudget,
    AttemptChargeRefundRequested,
    AttemptCheckpoint,
    AttemptClosure,
    AttemptId,
    AttemptPhase,
    AttemptReacquireRequested,
    AttemptRef,
    AttemptRegistered,
    AttemptRequest,
    AttemptSetupFailed,
    AttemptsState,
    AttemptView,
    Capabilities,
    ChargeId,
    ChargeKind,
    ChargeReceipt,
    CloseAttemptScope,
    Continuation,
    ContinuationId,
    ContinuationPhase,
    ContractError,
    ContractValidationError,
    CoreState,
    DecisionId,
    DecisionReceipt,
    DispatchTurn,
    EnsureSession,
    EnsureWorkspace,
    EventCursor,
    EventId,
    ExecuteRegisteredOperation,
    HostFence,
    HostId,
    InitialSessionsFailed,
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
    Operation,
    OperationDescriptor,
    OperationId,
    OperationRegistration,
    OperationRegistry,
    OperationRequest,
    PoolId,
    ReleaseDependency,
    RequestId,
    RequestTurn,
    ResourceId,
    RestoreRevision,
    RevisionAuthority,
    RevisionOperationObserved,
    RevisionOperationRequested,
    RoleId,
    RunEnvelope,
    SchemaRef,
    Scope,
    SessionAcquisitionGroup,
    SessionId,
    SessionObserved,
    SessionPhase,
    SessionSpec,
    SessionView,
    SetupFailureKind,
    Slot,
    SnapshotAndRetain,
    StartAttempt,
    StrategyState,
    Transition,
    TurnRequested,
    TurnSpec,
    Value,
    WorkspaceMode,
    WorkspaceObserved,
    WorkspacePlan,
    initial_state,
    project,
    step,
)


def reload_state(state: CoreState, codec: OperationRegistry | None = None) -> CoreState:
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
    codec = codec or OperationRegistry()
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


def occupy_slot(state: CoreState, event: AttemptRegistered | AttemptAdmitted) -> CoreState:
    """Publish the exact capacity episode that scheduling grants before admission."""
    slot = Slot(
        attempt=AttemptRef(
            attempt_id=event.request.attempt_id, generation=event.request.generation
        ),
        admission_id=event.request.decision_id,
        pools=event.request.pools,
        admitted_at=state.run.now_at,
    )
    scheduling = state.scheduling.model_copy(update={"slots": (*state.scheduling.slots, slot)})
    return state.model_copy(update={"scheduling": scheduling})


def canonical_start(state: CoreState, event: AttemptRegistered | AttemptAdmitted) -> CoreState:
    """Publish the exact accepted decision that authorizes this registration."""
    decision = StartAttempt(
        decision_id=event.request.decision_id,
        scope=Scope(owner=state.run.run_id, generation=state.run.generation),
        attempt_id=event.request.attempt_id,
        item_id=event.request.item_id,
        workspace=event.workspace,
        budget=event.budget,
        initial_sessions=event.initial_sessions,
    )
    payload = json.dumps(
        decision.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    receipt = DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=sha256(payload.encode()).hexdigest(),
        feedback=Accepted(decision_id=decision.decision_id),
    )
    receipts = tuple(row for row in state.run.receipts if row.decision_id != receipt.decision_id)
    return state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "receipts": (*receipts, receipt),
                    "limits": state.run.limits.model_copy(update={"max_attempts": 100}),
                }
            )
        }
    )


@given(st.lists(st.integers(min_value=0, max_value=5), min_size=1, max_size=25))
def test_queued_registration_replay_charges_once_and_reload_is_identical(order: list[int]) -> None:
    """K4 queued admission is charged before capacity, even with reordered duplicates."""
    state = initial_state()
    seen: set[int] = set()
    for identity in order:
        event = registration(str(identity))
        state = canonical_start(state, event)
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


def assert_setup_retired(result: Transition) -> AttemptView:
    """A conclusive setup failure closes the owner's episode with a cancel closure.

    Acquisition decides that setup failed; retirement owns the closure that
    follows. The owner holds a slot and an accepted start, so retirement
    accepts the request and asks the sandbox to close the attempt scope.
    """
    owner = result.state.attempts.attempts[0]
    assert owner.phase == AttemptPhase.CLOSING
    assert owner.closure is not None
    assert owner.closure.disposition == "cancel"
    assert owner.closure.admission_id == DecisionId(root="owner")
    assert any(isinstance(request, CloseAttemptScope) for request in result.requests)
    return owner


def owner_ref() -> AttemptRef:
    return AttemptRef(attempt_id=AttemptId(root="owner"), generation=0)


def canonical_turns(state: CoreState) -> CoreState:
    """Seed exact canonical invocation requests for synthetic lifecycle fixtures."""
    intents = list(state.intents.intents)
    sessions = list(state.sessions.sessions)
    receipts = list(state.run.receipts)
    for invocation in state.sessions.invocations:
        identity = (
            invocation.observation.request_id
            if invocation.observation is not None
            else RequestId(root=f"turn-{invocation.invocation.invocation_id.root}")
        )
        decision = RequestTurn(
            decision_id=DecisionId(root=f"origin-{identity.root}"),
            scope=invocation.scope,
            turn=invocation.turn,
        )
        payload = json.dumps(
            decision.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        receipt = DecisionReceipt(
            decision_id=decision.decision_id,
            decision=decision,
            payload_digest=sha256(payload.encode()).hexdigest(),
            feedback=Accepted(decision_id=decision.decision_id),
            request_ids=(identity,),
        )
        receipts = [
            row
            for row in receipts
            if row.decision_id != receipt.decision_id
            and not (
                isinstance(row.decision, RequestTurn)
                and row.decision.turn.invocation_id == invocation.turn.invocation_id
            )
        ]
        receipts.append(receipt)
        request = DispatchTurn(
            decision_id=decision.decision_id,
            request_id=identity,
            scope=invocation.scope,
            admission_id=state.attempts.attempts[0].admission_id,
            deadline_at=invocation.turn.deadline_at,
            turn=invocation.turn,
        )
        intent = Intent(
            request_id=identity,
            request=request,
            payload_digest="turn-proof",
            lifecycle=LifecycleClass.SESSION_TURN,
            phase=IntentPhase.DISPATCHED,
            reconcile_deadline_at=1000.0,
        )
        intents = [
            row
            for row in intents
            if row.request_id != identity
            and not (
                isinstance(row.request, DispatchTurn)
                and row.request.turn.invocation_id == invocation.turn.invocation_id
            )
        ]
        intents.append(intent)
        if not any(row.spec.session_id == invocation.invocation.session_id for row in sessions):
            sessions.append(
                SessionView(
                    spec=invocation.turn.session,
                    scope=invocation.scope,
                    generation=invocation.invocation.generation,
                    phase=SessionPhase.EXECUTING,
                    invocation=invocation.invocation.invocation_id,
                    resource_id=ResourceId(root=f"lease-{invocation.invocation.session_id.root}"),
                )
            )
    return state.model_copy(
        update={
            "run": state.run.model_copy(update={"receipts": tuple(receipts)}),
            "intents": state.intents.model_copy(update={"intents": tuple(intents)}),
            "sessions": state.sessions.model_copy(update={"sessions": tuple(sessions)}),
        }
    )


def assert_charge_authorized(state: CoreState, event: InvocationChargeRequested) -> None:
    """Assert A's charge proof, including the temporary unavailable sibling boundary."""
    state = canonical_turns(state)
    boundary: KernelNotImplementedError | None = None
    result = None
    try:
        result = step(state, event)
    except KernelNotImplementedError as error:
        boundary = error
    if boundary is not None:
        assert boundary.subarea == "_session_turns"
        assert boundary.event_kind == "invocation_charges_authorized"
    else:
        assert result is not None
        owner = next(
            attempt
            for attempt in project(result.state).attempts
            if attempt.attempt_id == event.attempt.attempt_id
        )
        charges = tuple(
            receipt
            for receipt in owner.charges
            if receipt.invocation_id == event.invocation.invocation_id
        )
        turn = next(
            row.turn for row in state.sessions.invocations if row.invocation == event.invocation
        )
        assert sum(receipt.charged for receipt in charges if receipt.kind == ChargeKind.TURN) == 1
        assert sum(
            receipt.charged for receipt in charges if receipt.kind == ChargeKind.ATTEMPT
        ) == int(turn.charge_class == "paid")
        assert len({receipt.charge_id for receipt in charges}) == len(charges)


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
    state = state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
        }
    )
    # A real acquiring attempt holds its Scheduling slot and its accepted start,
    # which is the authority retirement needs when setup fails.
    event = registration("owner")
    return canonical_start(occupy_slot(state, event), event)


def invocation_state(
    charge_class: Literal["paid", "correction", "resume", "free"],
    predecessor: InvocationRef | None = None,
) -> CoreState:
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
    charge_class: Literal["paid", "correction", "resume", "free"],
    guard: str,
    *,
    predecessor_present: bool,
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
    charge_class: Literal["paid", "correction", "resume", "free"], *, predecessor_present: bool
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
        assert_charge_authorized(state, event)
        return
    result = step(state, event)
    assert result.state.attempts == state.attempts
    assert result.requests == ()


@pytest.mark.parametrize("charge_class", ["paid", "correction"])
@given(
    chain_classes=st.lists(st.sampled_from(("paid", "correction", "free")), min_size=1, max_size=5)
)
@example(chain_classes=["free"])
def test_mixed_predecessor_chain_cannot_reset_the_retry_bound(
    charge_class: Literal["paid", "correction", "resume", "free"], chain_classes: list[str]
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
    charge_class: Literal["paid", "correction", "resume", "free"],
    mismatch: str,
    *,
    predecessor_present: bool,
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
    policy: str,
    charge_class: Literal["paid", "correction", "resume", "free"],
    *,
    resource_present: bool,
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
        assert_charge_authorized(state, event)


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
    state = occupy_slot(canonical_start(state, event), event)
    if predecessor_present or mode == WorkspaceMode.EXCLUSIVE_ROOT:
        with pytest.raises(ContractValidationError, match=r"workspace|parked_predecessor"):
            step(state, event)
    else:
        result = step(state, event)
        assert len(result.requests) == 1
        assert isinstance(result.requests[0], EnsureWorkspace)


@pytest.mark.parametrize("phase", [AttemptPhase.TERMINAL, AttemptPhase.PARKED])
def test_exclusive_root_is_released_when_its_holder_leaves_the_root(phase: AttemptPhase) -> None:
    """The root hold ends with the holder's phase; no separate release request exists."""
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
        workspace=WorkspacePlan(mode=WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline),
        budget=event.budget,
    )
    result = step(occupy_slot(canonical_start(state, event), event), event)
    assert [type(request) for request in result.requests] == [EnsureWorkspace]


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
    return canonical_turns(
        state.model_copy(
            update={
                "attempts": AttemptsState(attempts=(owner,)),
                "sessions": state.sessions.model_copy(
                    update={"invocations": (invocation,), "interrupts": (claim,)}
                ),
            }
        )
    )


@pytest.mark.parametrize("proof", ["absent", "unknown", "pending", "nonterminal", "terminal"])
@pytest.mark.parametrize("access", list(Access))
@given(retention=st.sampled_from(("wip", "candidate")))
def test_checkpoint_waits_for_every_competing_writer_to_terminate(
    proof: str, retention: Literal["wip", "candidate"], access: Access
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
                update={
                    "invocation_id": competing.invocation.invocation_id,
                    "session": competing.turn.session.model_copy(
                        update={"access": access, "session_id": competing.invocation.session_id}
                    ),
                }
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
    state = canonical_turns(state)
    event = InvocationCheckpointRequested(
        attempt=owner_ref(),
        invocation=target.invocation,
        retention=retention,
        authority=RequestId(root="checkpoint"),
    )
    if retention == "candidate" and (proof == "terminal" or access != Access.WRITE_CANDIDATE):
        with pytest.raises(KernelNotImplementedError) as raised:
            step(state, event)
        assert raised.value.subarea == "_attempt_acquisition"
        assert raised.value.event_kind == "invocation_checkpoint_requested"
        return
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
    assert prepared.requests[0].request_id is not None
    assert isinstance(prepared.requests[0], SnapshotAndRetain)
    assert prepared.requests[0].invocation == invocation
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
    intent = next(
        row for row in state.intents.intents if row.request_id == event.checkpoint_request
    )
    intent = intent.model_copy(
        update={
            "phase": IntentPhase.COMPLETED,
            "observation": observation(intent.request_id).model_copy(
                update={"revision": event.revision}
            ),
        }
    )
    committed = state.model_copy(
        update={
            "intents": state.intents.model_copy(
                update={
                    "intents": tuple(
                        intent if row.request_id == intent.request_id else row
                        for row in state.intents.intents
                    )
                }
            )
        }
    )
    boundary: KernelNotImplementedError | None = None
    completed = None
    try:
        completed = step(reload_state(committed), event)
    except KernelNotImplementedError as error:
        boundary = error
    if boundary is not None:
        assert boundary.event_kind == "invocation_checkpoint_available"
        assert boundary.subarea == "_session_inputs"
    else:
        assert completed is not None
        owner = project(completed.state).attempts[0]
        assert owner.checkpoints == (
            AttemptCheckpoint(
                invocation=invocation,
                request_id=event.checkpoint_request,
                revision=event.revision,
                retention="wip",
            ),
        )
        assert event.checkpoint_request not in owner.pending_intents
        repeated = step(reload_state(completed.state), event)
        assert repeated.state.attempts == completed.state.attempts
        assert repeated.requests == ()


@pytest.mark.parametrize("bound", ["none", "other", "unobserved-revision"])
def test_checkpoint_receipt_requires_a_request_bound_to_its_invocation(bound: str) -> None:
    """A committed snapshot that misnames its invocation or revision records nothing."""
    state = checkpoint_state()
    invocation = state.sessions.invocations[0].invocation
    prepared = step(
        state,
        InvocationCheckpointRequested(
            attempt=owner_ref(),
            invocation=invocation,
            retention="wip",
            authority=RequestId(root="checkpoint"),
        ),
    )
    state = reload_state(prepared.state)
    baseline = state.run.facts.baseline
    request = prepared.requests[0]
    assert isinstance(request, SnapshotAndRetain)
    assert request.request_id is not None
    other = InvocationRef(
        session_id=invocation.session_id,
        invocation_id=InvocationId(root="other"),
        generation=invocation.generation,
    )
    forged = request.model_copy(
        update={"invocation": {"none": None, "other": other}.get(bound, invocation)}
    )
    intent = next(row for row in state.intents.intents if row.request_id == request.request_id)
    intent = intent.model_copy(
        update={
            "request": forged,
            "phase": IntentPhase.COMPLETED,
            "observation": observation(request.request_id).model_copy(
                update={"revision": None if bound == "unobserved-revision" else baseline}
            ),
        }
    )
    state = state.model_copy(
        update={
            "intents": state.intents.model_copy(
                update={
                    "intents": tuple(
                        intent if row.request_id == request.request_id else row
                        for row in state.intents.intents
                    )
                }
            )
        }
    )
    event = InvocationCheckpointed(
        attempt=owner_ref(),
        revision=state.run.facts.baseline,
        charge=state.attempts.attempts[0].charges[0],
        invocation=invocation,
        checkpoint_request=request.request_id,
    )
    result = step(state, event)
    assert result.state.attempts.attempts[0].checkpoints == ()


@pytest.mark.parametrize("action", ["charge", "checkpoint", "checkpointed", "refund", "ended"])
@pytest.mark.parametrize(
    "proof",
    [
        (ObservationStatus.UNKNOWN, False),
        (ObservationStatus.UNKNOWN, True),
        (ObservationStatus.SUCCEEDED, False),
    ],
)
@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("resource_present", [False, True])
def test_unknown_invocation_acceptance_always_inspects(
    action: str, proof: tuple[ObservationStatus, bool], *, terminal: bool, resource_present: bool
) -> None:
    status, accepted = proof
    state = checkpoint_state()
    target = state.sessions.invocations[0]
    observed = observation(RequestId(root="turn"), status).model_copy(
        update={
            "accepted": accepted,
            "terminal": terminal,
            "resource_id": ResourceId(root="root") if resource_present else None,
        }
    )
    target = target.model_copy(update={"observation": observed})
    request = DispatchTurn(
        request_id=observed.request_id,
        scope=target.scope,
        admission_id=observed.admission_id,
        deadline_at=1000.0,
        turn=target.turn,
    )
    assert request.request_id is not None
    source = Intent(
        request_id=request.request_id,
        request=request,
        payload_digest="turn-proof",
        lifecycle=LifecycleClass.SESSION_TURN,
        phase=IntentPhase.RECONCILING,
        observation=observed,
        reconcile_deadline_at=1000.0,
    )
    state = state.model_copy(
        update={
            "sessions": state.sessions.model_copy(update={"invocations": (target,)}),
            "intents": state.intents.model_copy(update={"intents": (source,)}),
        }
    )
    events = {
        "charge": InvocationChargeRequested(attempt=owner_ref(), invocation=target.invocation),
        "checkpoint": InvocationCheckpointRequested(
            attempt=owner_ref(),
            invocation=target.invocation,
            retention="wip",
            authority=RequestId(root="checkpoint"),
        ),
        "checkpointed": InvocationCheckpointed(
            attempt=owner_ref(),
            invocation=target.invocation,
            checkpoint_request=RequestId(root="checkpoint"),
            revision=state.run.facts.baseline,
            charge=state.attempts.attempts[0].charges[0],
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
    assert result.requests[0].resource_id is None


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
    state = occupy_slot(step(canonical_start(initial_state(), event), event).state, event)
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
    "slot_case",
    [
        "absent",
        "other-attempt",
        "stale-generation",
        "other-admission",
        "other-pools",
        "duplicate",
        "current",
    ],
)
def test_queued_attempt_acquires_only_under_its_exact_capacity_slot(slot_case: str) -> None:
    event = registration("owner")
    state = step(canonical_start(initial_state(), event), event).state
    admitted = AttemptAdmitted(
        admission_id=event.request.decision_id,
        request=event.request,
        workspace=event.workspace,
        budget=event.budget,
    )
    exact = occupy_slot(state, event).scheduling.slots[0]
    slots = {
        "absent": (),
        "other-attempt": (
            exact.model_copy(
                update={"attempt": AttemptRef(attempt_id=AttemptId(root="other"), generation=0)}
            ),
        ),
        "stale-generation": (
            exact.model_copy(
                update={"attempt": AttemptRef(attempt_id=exact.attempt.attempt_id, generation=1)}
            ),
        ),
        "other-admission": (exact.model_copy(update={"admission_id": DecisionId(root="old")}),),
        "other-pools": (exact.model_copy(update={"pools": (PoolId(root="gpu"),)}),),
        "duplicate": (exact, exact),
        "current": (exact,),
    }[slot_case]
    state = state.model_copy(
        update={"scheduling": state.scheduling.model_copy(update={"slots": slots})}
    )
    result = step(state, admitted)
    if slot_case == "current":
        assert [type(request) for request in result.requests] == [EnsureWorkspace]
        assert project(result.state).attempts[0].phase == AttemptPhase.ACQUIRING
    else:
        assert result.requests == ()
        assert project(result.state).attempts[0].phase == AttemptPhase.QUEUED
        assert result.state.attempts == state.attempts


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
    if generation != 0 or episode != "owner":
        before = state.model_dump_json()
        with pytest.raises(ContractError, match=r"scope|admission_id|episode"):
            step(state, event)
        assert state.model_dump_json() == before
        return
    if (
        episode == "owner"
        and terminal
        and status
        in (ObservationStatus.FAILED, ObservationStatus.REJECTED, ObservationStatus.CANCELLED)
    ):
        assert_setup_retired(step(state, event))
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
    """A conclusive setup failure spends one cycle regardless of failure taxonomy."""
    state = acquiring_state()
    for sequence in order:
        observed = observation(RequestId(root="workspace"), ObservationStatus.FAILED).model_copy(
            update={"sequence": sequence, "accepted": False}
        )
        event = AttemptSetupFailed(attempt=owner_ref(), observation=observed, failure=failure)
        result = step(reload_state(state), event)
        assert result == step(reload_state(state), event)
        owner = assert_setup_retired(result)
        # One conclusive setup cycle is charged once, whatever the failure kind.
        assert sum(c.charged for c in owner.charges if c.kind == ChargeKind.ATTEMPT) == 1


@pytest.mark.parametrize("failure", tuple(SetupFailureKind))
@pytest.mark.parametrize(
    "proof",
    [
        (ObservationStatus.UNKNOWN, False),
        (ObservationStatus.UNKNOWN, True),
        (ObservationStatus.SUCCEEDED, False),
    ],
)
@pytest.mark.parametrize("terminal", [False, True])
def test_unknown_setup_acceptance_requests_inspection_without_new_charge(
    failure: SetupFailureKind, proof: tuple[ObservationStatus, bool], *, terminal: bool
) -> None:
    status, accepted = proof
    state = acquiring_state()
    observed = observation(RequestId(root="workspace"), status).model_copy(
        update={"accepted": accepted, "terminal": terminal}
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
def test_checkpoint_requires_writer_termination_proof(
    retention: Literal["wip", "candidate"],
) -> None:
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
    proof: tuple[ChargeKind, Literal["interrupted", "unsupported"], bool, bool, bool],
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


@given(
    order=st.lists(
        st.tuples(st.integers(min_value=0, max_value=1), st.sampled_from(tuple(ObservationStatus))),
        min_size=1,
        max_size=12,
    )
)
def test_stale_workspace_observations_cannot_replace_canonical_ready_proof(
    order: list[tuple[int, ObservationStatus]],
) -> None:
    state = acquiring_state()
    proof = observation(RequestId(root="workspace")).model_copy(update={"sequence": 2})
    intent = state.intents.intents[0].model_copy(
        update={"phase": IntentPhase.COMPLETED, "sequence": 2, "observation": proof}
    )
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"intents": (intent,)})}
    )
    for sequence, status in order:
        observed = proof.model_copy(update={"sequence": sequence, "status": status})
        event = WorkspaceObserved(
            attempt=owner_ref(), observation=observed, revision=state.run.facts.baseline
        )
        result = step(state, event)
        assert result == step(reload_state(state), event)
        assert result.state.attempts == state.attempts
        assert result.requests == ()
        assert result.events == ()
        state = reload_state(result.state)


@pytest.mark.parametrize("group_phase", ["acquiring", "failed", "ready"])
@pytest.mark.parametrize("proof_committed", [False, True])
def test_initial_group_readiness_requires_workspace_commit_and_never_revives_failed_group(
    group_phase: Literal["acquiring", "ready", "failed"], *, proof_committed: bool
) -> None:
    state = acquiring_state()
    session_id = SessionId(root="session")
    owner = state.attempts.attempts[0].model_copy(update={"sessions": (session_id,)})
    group = SessionAcquisitionGroup(
        attempt=owner_ref(),
        admission_id=DecisionId(root="owner"),
        scope=Scope(owner=AttemptId(root="owner"), generation=0),
        session_ids=(session_id,),
        phase=group_phase,
    )
    intent = state.intents.intents[0]
    if proof_committed:
        intent = intent.model_copy(
            update={"phase": IntentPhase.COMPLETED, "observation": observation(intent.request_id)}
        )
        owner = owner.model_copy(update={"pending_intents": ()})
    state = state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "sessions": state.sessions.model_copy(update={"acquisition_groups": (group,)}),
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
        }
    )
    event = InitialSessionsReady(
        attempt=owner_ref(), admission_id=DecisionId(root="owner"), session_ids=(session_id,)
    )
    result = step(state, event)
    assert result.requests == ()
    # Scheduling holds no slot for this fixture episode, so it stays untouched.
    assert result.state.scheduling == state.scheduling
    if group_phase == "ready" and proof_committed:
        # Only a committed workspace and a ready group activate the attempt.
        assert [row.phase for row in result.state.attempts.attempts] == [AttemptPhase.ACTIVE]
    else:
        assert result.state.attempts == state.attempts


def failed_group_state() -> CoreState:
    state = invocation_state("paid")
    owner = state.attempts.attempts[0].model_copy(
        update={"phase": AttemptPhase.ACQUIRING, "sessions": (SessionId(root="session"),)}
    )
    request = EnsureSession(
        request_id=RequestId(root="member"),
        scope=Scope(owner=owner.attempt_id, generation=0),
        deadline_at=1000.0,
        admission_id=DecisionId(root="owner"),
        spec=state.sessions.sessions[0].spec,
    )
    assert request.request_id is not None
    intent = Intent(
        request_id=request.request_id,
        request=request,
        payload_digest="member-proof",
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
        phase=IntentPhase.COMPLETED,
        reconcile_deadline_at=1000.0,
        observation=observation(request.request_id, ObservationStatus.FAILED),
    )
    group = state.sessions.acquisition_groups[0].model_copy(
        update={"phase": "failed", "failure_request": request.request_id}
    )
    state = state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "sessions": state.sessions.model_copy(update={"acquisition_groups": (group,)}),
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
        }
    )
    event = registration("owner")
    return canonical_start(occupy_slot(state, event), event)


@pytest.mark.parametrize("failure", list(SetupFailureKind))
@pytest.mark.parametrize("member", ["session", "foreign"])
@pytest.mark.parametrize("episode", ["owner", "old"])
def test_initial_session_failure_is_bound_to_exact_group_member_and_episode(
    failure: SetupFailureKind, member: str, episode: str
) -> None:
    state = failed_group_state()
    assert state.intents.intents[0].observation is not None
    event = InitialSessionsFailed(
        attempt=owner_ref(),
        admission_id=DecisionId(root=episode),
        session_id=SessionId(root=member),
        observation=state.intents.intents[0].observation,
        failure=failure,
    )
    if member == "session" and episode == "owner":
        assert_setup_retired(step(state, event))
    else:
        result = step(state, event)
        assert result.state.attempts == state.attempts
        assert result.requests == ()


def reopening_state() -> CoreState:
    state = owned_state()
    previous = InvocationRef(
        session_id=SessionId(root="session"), invocation_id=InvocationId(root="prior"), generation=0
    )
    continuation = Continuation(
        continuation_id=ContinuationId(root="continuation"),
        invocation=previous,
        next_invocation=previous.model_copy(
            update={"invocation_id": InvocationId(root="successor")}
        ),
        jobs=(),
        deadline_at=1000.0,
        phase=ContinuationPhase.REOPENING,
        reopen_authority=RequestId(root="reopen"),
        park_authority=RequestId(root="park"),
    )
    checkpoint = AttemptCheckpoint(
        invocation=previous,
        request_id=RequestId(root="checkpoint"),
        revision=state.run.facts.baseline,
        retention="wip",
    )
    closure = AttemptClosure(
        disposition="park",
        requested_at=0.0,
        authority=RequestId(root="park"),
        admission_id=DecisionId(root="previous-episode"),
    )
    owner = state.attempts.attempts[0].model_copy(
        update={"checkpoints": (checkpoint,), "closure": closure}
    )
    original = invocation_state("paid").sessions.invocations[0]
    original = original.model_copy(
        update={
            "invocation": previous,
            "turn": original.turn.model_copy(update={"invocation_id": previous.invocation_id}),
            "phase": SessionPhase.TERMINAL,
            "observation": observation(RequestId(root="original")),
        }
    )
    return state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "sessions": state.sessions.model_copy(
                update={
                    "invocations": (original,),
                    "sessions": (
                        SessionView(
                            spec=original.turn.session,
                            scope=original.scope,
                            generation=previous.generation,
                            phase=SessionPhase.TERMINAL,
                            invocation=previous.invocation_id,
                            resource_id=ResourceId(root="retained-conversation"),
                        ),
                    ),
                }
            ),
            "evaluation": state.evaluation.model_copy(update={"continuations": (continuation,)}),
        }
    )


@pytest.mark.parametrize("phase", list(ContinuationPhase))
@pytest.mark.parametrize("episode", ["owner", "old"])
@given(replays=st.integers(min_value=1, max_value=4))
def test_reacquisition_restores_retained_revision_for_exact_new_admission_only(
    phase: ContinuationPhase, episode: str, replays: int
) -> None:
    state = reopening_state()
    continuation = state.evaluation.continuations[0].model_copy(update={"phase": phase})
    state = state.model_copy(
        update={
            "evaluation": state.evaluation.model_copy(update={"continuations": (continuation,)})
        }
    )
    event = AttemptReacquireRequested(
        attempt=owner_ref(),
        continuation_id=continuation.continuation_id,
        request_id=RequestId(root="reopen"),
        admission_id=DecisionId(root=episode),
        base=state.run.facts.baseline,
    )
    result = step(state, event)
    assert result == step(reload_state(state), event)
    assert result.state.attempts.attempts[0].charges == ()
    assert result.state.attempts.attempts[0].closure == state.attempts.attempts[0].closure
    if phase == ContinuationPhase.REOPENING and episode == "owner":
        assert len(result.requests) == 2
        assert isinstance(result.requests[1], EnsureSession)
        assert isinstance(result.requests[0], RestoreRevision)
        assert result.requests[0].revision == event.base
        assert result.requests[0].admission_id == event.admission_id
        for _ in range(replays):
            repeated = step(reload_state(result.state), event)
            assert repeated.state.attempts == result.state.attempts
            assert repeated.requests == ()
            result = repeated
    else:
        assert result.requests == ()
        assert result.state.attempts == state.attempts


@pytest.mark.parametrize("predecessor_present", [False, True])
@pytest.mark.parametrize("source_matches", [False, True])
def test_authorized_resume_does_not_consume_correction_retry_authority(
    *, predecessor_present: bool, source_matches: bool
) -> None:
    predecessor = InvocationRef(
        session_id=SessionId(root="session"), invocation_id=InvocationId(root="prior"), generation=0
    )
    state = invocation_state("resume", predecessor if predecessor_present else None)
    invocation = state.sessions.invocations[0]
    continuation_id = ContinuationId(root="resume")
    invocation = invocation.model_copy(
        update={"turn": invocation.turn.model_copy(update={"continuation_id": continuation_id})}
    )
    continuation = Continuation(
        continuation_id=continuation_id,
        invocation=predecessor
        if source_matches
        else predecessor.model_copy(update={"invocation_id": InvocationId(root="foreign")}),
        next_invocation=invocation.invocation,
        jobs=(),
        deadline_at=1000.0,
        phase=ContinuationPhase.AUTHORIZED,
    )
    owner = state.attempts.attempts[0].model_copy(
        update={"budget": AttemptBudget(paid_invocation_limit=0, retry_limit=0)}
    )
    source = invocation.model_copy(
        update={
            "invocation": predecessor,
            "turn": invocation.turn.model_copy(
                update={
                    "invocation_id": predecessor.invocation_id,
                    "predecessor": None,
                    "charge_class": "paid",
                    "continuation_id": None,
                }
            ),
            "phase": SessionPhase.TERMINAL,
            "observation": observation(RequestId(root="prior")),
        }
    )
    state = state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "sessions": state.sessions.model_copy(update={"invocations": (source, invocation)}),
            "evaluation": state.evaluation.model_copy(update={"continuations": (continuation,)}),
        }
    )
    event = InvocationChargeRequested(attempt=owner_ref(), invocation=invocation.invocation)
    if not source_matches:
        result = step(state, event)
        assert result.requests == ()
        assert result.state.attempts == state.attempts
    else:
        assert_charge_authorized(state, event)


@given(
    left=st.text(alphabet=":aé漢.-", min_size=1, max_size=12),
    right=st.text(alphabet=":bé字._", min_size=1, max_size=12),
)
@example(left="a", right="b")
def test_opaque_identity_delimiters_cannot_collide_workspace_request_ids(
    left: str, right: str
) -> None:
    state = initial_state()
    cases = ((f"{left}:0:{right}", "c"), (left, f"{right}:0:c"))
    request_ids: list[RequestId] = []
    for attempt_id, admission_id in cases:
        event = registration(attempt_id)
        request = event.request.model_copy(update={"decision_id": DecisionId(root=admission_id)})
        event = event.model_copy(update={"request": request})
        state = canonical_start(state, event)
        registered = step(state, event)
        state = occupy_slot(reload_state(registered.state), event)
        admitted = AttemptAdmitted(
            admission_id=request.decision_id,
            request=request,
            workspace=event.workspace,
            budget=event.budget,
        )
        acquired = step(state, admitted)
        assert acquired == step(reload_state(state), admitted)
        assert len(acquired.requests) == 1
        assert isinstance(acquired.requests[0], EnsureWorkspace)
        assert acquired.requests[0].request_id is not None
        request_ids.append(acquired.requests[0].request_id)
        state = reload_state(acquired.state)
    assert len(set(request_ids)) == 2
    assert len(state.intents.intents) == 2
    assert len(project(state).attempts) == 2


class RetentionExtensionOutcome(Value):
    """Typed registered revision proof supplied by an owning library."""

    revision: str


class RetentionExtensionRequest(OperationRequest):
    """A neutral codec value, with workspace authority in its descriptor."""

    kind: Literal["workspace.retain-extension"] = "workspace.retain-extension"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = RetentionExtensionOutcome
    digest: str


def revision_operation_state() -> tuple[CoreState, OperationRegistry, RevisionOperationRequested]:
    codec = OperationRegistry(
        (
            OperationRegistration(
                descriptor=OperationDescriptor(
                    kind="workspace.retain-extension",
                    request_schema=SchemaRef(name="retain-extension", version=1),
                    outcome_schema=SchemaRef(name="retain-extension-outcome", version=1),
                    lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
                    revision_authority=RevisionAuthority.RETAIN,
                    inspect=True,
                ),
                request_model=RetentionExtensionRequest,
                outcome_model=RetentionExtensionOutcome,
            ),
        )
    )
    state = owned_state(AttemptPhase.ACTIVE)
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={"capabilities": Capabilities(operations=codec.descriptors)}
            ),
        }
    )
    wire = codec.encode(RetentionExtensionRequest(digest=state.run.facts.baseline.digest))
    request = ExecuteRegisteredOperation(
        request_id=RequestId(root="revision"),
        scope=Scope(owner=AttemptId(root="owner"), generation=0),
        admission_id=DecisionId(root="owner"),
        deadline_at=1000.0,
        operation_id=OperationId(root="operation:operation"),
        operation=wire,
        retry_limit=0,
    )
    decision = Operation.model_validate(
        {
            "decision_id": DecisionId(root="operation"),
            "scope": request.scope,
            "deadline_at": request.deadline_at,
            "request": RetentionExtensionRequest(digest=state.run.facts.baseline.digest),
        },
        context={"operation_registry": codec},
    )
    payload = json.dumps(
        decision.model_dump(mode="json", serialize_as_any=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert request.request_id is not None
    receipt = DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=sha256(payload.encode()).hexdigest(),
        feedback=Accepted(decision_id=decision.decision_id),
        request_ids=(request.request_id,),
    )
    request = request.model_copy(update={"decision_id": decision.decision_id})
    state = state.model_copy(update={"run": state.run.model_copy(update={"receipts": (receipt,)})})
    return (
        state,
        codec,
        RevisionOperationRequested(request=request, authority=RevisionAuthority.RETAIN),
    )


def test_registered_revision_dispatch_and_acknowledgement_use_exact_public_codec_proof() -> None:
    state, codec, event = revision_operation_state()
    prepared = step(state, event)
    assert prepared == step(reload_state(state, codec), event)
    assert prepared.requests == (event.request,)
    assert event.request.request_id in prepared.state.attempts.attempts[0].pending_intents
    state = reload_state(prepared.state, codec)
    intent = state.intents.intents[0]
    observed = observation(intent.request_id)
    intent = intent.model_copy(
        update={
            "phase": IntentPhase.COMPLETED,
            "sequence": observed.sequence,
            "observation": observed,
        }
    )
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"intents": (intent,)})}
    )
    completion = RevisionOperationObserved(
        operation_id=event.request.operation_id,
        observation=observed,
        revision=state.run.facts.baseline,
    )
    result = step(state, completion)
    assert result == step(reload_state(state, codec), completion)
    assert result.state.attempts.attempts[0].pending_intents == ()
    assert result.state.attempts.attempts[0].checkpoints == ()
    assert result.requests == ()
    duplicate = step(reload_state(result.state, codec), completion)
    assert duplicate.state.attempts == result.state.attempts
    assert duplicate.requests == ()


@pytest.mark.parametrize("wrong_proof", ["kind", "operation", "stale"])
def test_revision_observation_cannot_clear_another_request_or_newer_observation(
    wrong_proof: str,
) -> None:
    if wrong_proof == "kind":
        state = acquiring_state()
        codec = OperationRegistry()
        observed = observation(RequestId(root="workspace"))
        event = RevisionOperationObserved(
            operation_id=OperationId(root="unrelated"),
            observation=observed,
            revision=state.run.facts.baseline,
        )
    else:
        state, codec, request = revision_operation_state()
        state = step(state, request).state
        assert request.request.request_id is not None
        observed = observation(request.request.request_id)
        latest = observed.model_copy(update={"sequence": 2})
        intent = state.intents.intents[0].model_copy(
            update={"phase": IntentPhase.COMPLETED, "sequence": 2, "observation": latest}
        )
        state = state.model_copy(
            update={"intents": state.intents.model_copy(update={"intents": (intent,)})}
        )
        event = RevisionOperationObserved(
            operation_id=OperationId(root="unrelated")
            if wrong_proof == "operation"
            else request.request.operation_id,
            observation=observed,
            revision=state.run.facts.baseline,
        )
    result = step(state, event)
    assert result == step(reload_state(state, codec), event)
    assert result.state.attempts == state.attempts
    assert result.requests == ()


@pytest.mark.parametrize("phase", [AttemptPhase.PARKED, AttemptPhase.TERMINAL])
@pytest.mark.parametrize("unresolved", ["pending", "dependency"])
@pytest.mark.parametrize("predecessor_present", [False, True])
def test_unresolved_old_root_ownership_fences_acquisition_even_if_logically_retired(
    phase: AttemptPhase, unresolved: str, *, predecessor_present: bool
) -> None:
    state = owned_state(phase)
    owner = state.attempts.attempts[0].model_copy(
        update={
            "workspace": WorkspacePlan(
                mode=WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
            ),
            "pending_intents": (RequestId(root="unfinished"),),
        }
    )
    if unresolved == "dependency":
        owner = owner.model_copy(
            update={
                "pending_intents": (),
                "release_dependencies": (
                    ReleaseDependency(kind="session", identity=SessionId(root="live")),
                ),
            }
        )
    state = state.model_copy(update={"attempts": AttemptsState(attempts=(owner,))})
    event = registration("next")
    admitted = AttemptAdmitted(
        admission_id=event.request.decision_id,
        request=event.request,
        budget=event.budget,
        workspace=WorkspacePlan(
            mode=WorkspaceMode.EXCLUSIVE_ROOT,
            base=state.run.facts.baseline,
            parked_predecessor=owner_ref() if predecessor_present else None,
        ),
    )
    state = occupy_slot(canonical_start(state, admitted), admitted)
    with pytest.raises(ContractValidationError, match=r"workspace|parked_predecessor"):
        step(state, admitted)


@pytest.mark.parametrize(
    "wrong_proof",
    [
        "attempt",
        "generation",
        "base",
        "mode",
        "observation-scope",
        "observation-episode",
        "observation-request",
    ],
)
def test_workspace_ready_rejects_misattributed_durable_workspace_proof(wrong_proof: str) -> None:
    state = acquiring_state()
    owner = state.attempts.attempts[0].model_copy(update={"pending_intents": ()})
    intent = state.intents.intents[0]
    request = intent.request
    assert isinstance(request, EnsureWorkspace)
    observed = observation(intent.request_id)
    if wrong_proof == "attempt":
        request = request.model_copy(
            update={"attempt": AttemptRef(attempt_id=AttemptId(root="foreign"), generation=0)}
        )
    elif wrong_proof == "generation":
        request = request.model_copy(
            update={"attempt": owner_ref().model_copy(update={"generation": 1})}
        )
    elif wrong_proof == "base":
        request = request.model_copy(
            update={
                "plan": request.plan.model_copy(
                    update={
                        "base": state.run.facts.baseline.model_copy(update={"digest": "foreign"})
                    }
                )
            }
        )
    elif wrong_proof == "mode":
        request = request.model_copy(
            update={
                "plan": request.plan.model_copy(update={"mode": WorkspaceMode.READ_ONLY_REVISION})
            }
        )
    elif wrong_proof == "observation-scope":
        observed = observed.model_copy(
            update={"scope": Scope(owner=AttemptId(root="foreign"), generation=0)}
        )
    elif wrong_proof == "observation-episode":
        observed = observed.model_copy(update={"admission_id": DecisionId(root="old")})
    else:
        observed = observed.model_copy(update={"request_id": RequestId(root="foreign")})
    intent = intent.model_copy(
        update={"request": request, "phase": IntentPhase.COMPLETED, "observation": observed}
    )
    state = state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
        }
    )
    result = step(
        state,
        InitialSessionsReady(
            attempt=owner_ref(), admission_id=DecisionId(root="owner"), session_ids=()
        ),
    )
    assert result.state.attempts == state.attempts
    assert result.requests == ()


@pytest.mark.parametrize("action", ["charge", "checkpoint", "refund"])
@pytest.mark.parametrize("conflict", ["session", "generation", "scope"])
def test_ambiguous_invocation_identity_grants_no_accounting_or_checkpoint_authority(
    action: str, conflict: str
) -> None:
    state = invocation_state("paid") if action == "charge" else checkpoint_state()
    target = state.sessions.invocations[0]
    ref = target.invocation
    if conflict == "session":
        ref = ref.model_copy(update={"session_id": SessionId(root="foreign")})
    elif conflict == "generation":
        ref = ref.model_copy(update={"generation": 1})
    duplicate = target.model_copy(
        update={
            "invocation": ref,
            "scope": Scope(owner=AttemptId(root="foreign"), generation=0)
            if conflict == "scope"
            else target.scope,
            "turn": target.turn.model_copy(
                update={
                    "session": target.turn.session.model_copy(update={"access": Access.READ_ONLY})
                }
            ),
        }
    )
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"invocations": (target, duplicate)})}
    )
    if action == "charge":
        event = InvocationChargeRequested(attempt=owner_ref(), invocation=target.invocation)
    elif action == "checkpoint":
        event = InvocationCheckpointRequested(
            attempt=owner_ref(),
            invocation=target.invocation,
            retention="wip",
            authority=RequestId(root="checkpoint"),
        )
    else:
        event = AttemptChargeRefundRequested(
            attempt=owner_ref(),
            charge_id=state.attempts.attempts[0].charges[0].charge_id,
            amount=1,
            reason="interrupted",
            authority=RequestId(root="interrupt"),
            checkpoint_authority=RequestId(root="checkpoint"),
        )
    result = step(state, event)
    assert result == step(reload_state(state), event)
    assert result.state.attempts == state.attempts
    assert result.requests == ()


@pytest.mark.parametrize("session_owner", ["attempt", "run"])
@pytest.mark.parametrize("policy", ["fresh", "reuse"])
@pytest.mark.parametrize("charge_class", ["paid", "free"])
@given(
    generations=st.tuples(
        st.integers(min_value=0, max_value=4),
        st.integers(min_value=0, max_value=4),
        st.integers(min_value=0, max_value=4),
    )
)
def test_invocation_generation_belongs_to_its_session_not_its_attempt(
    session_owner: str,
    policy: str,
    charge_class: Literal["paid", "correction", "resume", "free"],
    generations: tuple[int, int, int],
) -> None:
    attempt_generation, session_generation, run_generation = generations
    state = invocation_state(charge_class)
    owner = state.attempts.attempts[0].model_copy(update={"generation": attempt_generation})
    target = AttemptRef(attempt_id=owner.attempt_id, generation=attempt_generation)
    attempt_scope = Scope(owner=owner.attempt_id, generation=attempt_generation)
    invocation = state.sessions.invocations[0]
    ref = invocation.invocation.model_copy(update={"generation": session_generation})
    session = state.sessions.sessions[0]
    spec = session.spec.model_copy(update={"policy": policy})
    session_scope = (
        attempt_scope
        if session_owner == "attempt"
        else Scope(owner=state.run.run_id, generation=run_generation)
    )
    session = session.model_copy(
        update={"spec": spec, "scope": session_scope, "generation": session_generation}
    )
    invocation = invocation.model_copy(
        update={
            "invocation": ref,
            "scope": attempt_scope,
            "turn": invocation.turn.model_copy(
                update={"session": spec, "workspace": attempt_scope}
            ),
        }
    )
    group = state.sessions.acquisition_groups[0].model_copy(
        update={"attempt": target, "scope": attempt_scope}
    )
    state = state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "sessions": state.sessions.model_copy(
                update={
                    "sessions": (session,),
                    "invocations": (invocation,),
                    "acquisition_groups": (group,),
                }
            ),
            "run": state.run.model_copy(update={"generation": run_generation}),
        }
    )
    assert_charge_authorized(
        reload_state(state), InvocationChargeRequested(attempt=target, invocation=ref)
    )


def test_unknown_invocation_end_cannot_inspect_an_untracked_request() -> None:
    state = checkpoint_state()
    target = state.sessions.invocations[0]
    observed = observation(RequestId(root="untracked"), ObservationStatus.UNKNOWN)
    result = step(
        state,
        InvocationEnded(attempt=owner_ref(), invocation=target.invocation, observation=observed),
    )
    assert result.state.attempts == state.attempts
    assert result.requests == ()


@pytest.mark.parametrize("authority", list(RevisionAuthority))
@pytest.mark.parametrize("episode", [None, "owner", "old"])
def test_registered_revision_dispatch_requires_declared_authority_and_current_episode(
    authority: RevisionAuthority, episode: str | None
) -> None:
    state, codec, event = revision_operation_state()
    event = event.model_copy(
        update={
            "authority": authority,
            "request": event.request.model_copy(
                update={"admission_id": DecisionId(root=episode) if episode else None}
            ),
        }
    )
    result = step(state, event)
    assert result == step(reload_state(state, codec), event)
    if authority == RevisionAuthority.RETAIN and episode == "owner":
        assert len(result.requests) == 1
        assert result.requests[0].admission_id == DecisionId(root="owner")
        assert event.request.request_id in result.state.attempts.attempts[0].pending_intents
    else:
        assert result.state.attempts == state.attempts
        assert result.requests == ()


@pytest.mark.parametrize("mode", list(WorkspaceMode))
@pytest.mark.parametrize(
    "phase",
    [AttemptPhase.ACQUIRING, AttemptPhase.ACTIVE, AttemptPhase.CLOSING, AttemptPhase.BLOCKED],
)
def test_reacquisition_obeys_the_same_exclusive_root_guard_as_initial_admission(
    mode: WorkspaceMode, phase: AttemptPhase
) -> None:
    state = reopening_state()
    owner = state.attempts.attempts[0].model_copy(
        update={"workspace": state.attempts.attempts[0].workspace.model_copy(update={"mode": mode})}
    )
    competing = owner.model_copy(
        update={
            "attempt_id": AttemptId(root="other"),
            "phase": phase,
            "closure": None,
            "workspace": owner.workspace.model_copy(update={"mode": WorkspaceMode.EXCLUSIVE_ROOT}),
        }
    )
    state = state.model_copy(update={"attempts": AttemptsState(attempts=(owner, competing))})
    event = AttemptReacquireRequested(
        attempt=owner_ref(),
        continuation_id=ContinuationId(root="continuation"),
        request_id=RequestId(root="reopen"),
        admission_id=DecisionId(root="owner"),
        base=state.run.facts.baseline,
    )
    if mode == WorkspaceMode.EXCLUSIVE_ROOT:
        with pytest.raises(ContractValidationError, match="workspace"):
            step(state, event)
    else:
        result = step(state, event)
        assert len(result.requests) == 2
        assert isinstance(result.requests[1], EnsureSession)
        assert isinstance(result.requests[0], RestoreRevision)


@pytest.mark.parametrize("mode", list(WorkspaceMode))
@pytest.mark.parametrize("access", list(Access))
@pytest.mark.parametrize("charge_class", ["paid", "correction", "resume", "free"])
@pytest.mark.parametrize("predecessor_present", [False, True])
def test_read_only_workspace_never_authorizes_a_candidate_writer(
    mode: WorkspaceMode,
    access: Access,
    charge_class: Literal["paid", "correction", "resume", "free"],
    *,
    predecessor_present: bool,
) -> None:
    state = invocation_state(charge_class)
    target = state.sessions.invocations[0]
    predecessor = target.invocation.model_copy(
        update={"invocation_id": InvocationId(root="previous")}
    )
    session = state.sessions.sessions[0]
    spec = session.spec.model_copy(update={"access": access})
    session = session.model_copy(update={"spec": spec})
    target = target.model_copy(
        update={
            "turn": target.turn.model_copy(
                update={
                    "session": spec,
                    "predecessor": predecessor if predecessor_present else None,
                }
            )
        }
    )
    previous = target.model_copy(
        update={
            "invocation": predecessor,
            "phase": SessionPhase.TERMINAL,
            "observation": observation(RequestId(root="previous")),
            "turn": target.turn.model_copy(
                update={
                    "invocation_id": predecessor.invocation_id,
                    "predecessor": None,
                    "charge_class": "paid",
                }
            ),
        }
    )
    continuation = Continuation(
        continuation_id=ContinuationId(root="resume"),
        invocation=predecessor,
        next_invocation=target.invocation,
        jobs=(),
        deadline_at=1000.0,
        phase=ContinuationPhase.AUTHORIZED,
    )
    if charge_class == "resume":
        target = target.model_copy(
            update={
                "turn": target.turn.model_copy(
                    update={"continuation_id": continuation.continuation_id}
                )
            }
        )
    owner = state.attempts.attempts[0].model_copy(
        update={"workspace": state.attempts.attempts[0].workspace.model_copy(update={"mode": mode})}
    )
    state = state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "sessions": state.sessions.model_copy(
                update={"sessions": (session,), "invocations": (previous, target)}
            ),
            "evaluation": state.evaluation.model_copy(update={"continuations": (continuation,)}),
        }
    )
    event = InvocationChargeRequested(attempt=owner_ref(), invocation=target.invocation)
    if (mode == WorkspaceMode.READ_ONLY_REVISION and access == Access.WRITE_CANDIDATE) or (
        charge_class == "correction" and not predecessor_present
    ):
        result = step(state, event)
        assert result.state.attempts == state.attempts
        assert result.requests == ()
    else:
        assert_charge_authorized(state, event)


def test_registered_restore_cannot_mutate_a_read_only_revision_workspace() -> None:
    state, codec, event = revision_operation_state()
    descriptor = codec.descriptors[0].model_copy(
        update={"revision_authority": RevisionAuthority.RESTORE}
    )
    owner = state.attempts.attempts[0].model_copy(
        update={
            "workspace": state.attempts.attempts[0].workspace.model_copy(
                update={"mode": WorkspaceMode.READ_ONLY_REVISION}
            )
        }
    )
    state = state.model_copy(
        update={
            "registry": (descriptor,),
            "attempts": AttemptsState(attempts=(owner,)),
            "run": state.run.model_copy(
                update={"capabilities": Capabilities(operations=(descriptor,))}
            ),
        }
    )
    event = event.model_copy(update={"authority": RevisionAuthority.RESTORE})
    result = step(state, event)
    assert result.state.attempts == state.attempts
    assert result.requests == ()


@pytest.mark.parametrize("policy", ["fresh", "reuse"])
@pytest.mark.parametrize("charge_class", ["paid", "free"])
@given(replays=st.integers(min_value=1, max_value=3))
def test_attempt_turn_charges_before_session_acquisition_but_cannot_dispatch(
    policy: str, charge_class: Literal["paid", "correction", "resume", "free"], replays: int
) -> None:
    state = owned_state(AttemptPhase.ACTIVE)
    turn = invocation_state(charge_class).sessions.invocations[0].turn
    turn = turn.model_copy(update={"session": turn.session.model_copy(update={"policy": policy})})
    assert isinstance(turn.workspace, Scope)
    decision = RequestTurn(
        decision_id=DecisionId(root="plain-turn"), scope=turn.workspace, turn=turn
    )
    payload = json.dumps(
        decision.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    receipt = DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=sha256(payload.encode()).hexdigest(),
        feedback=Accepted(decision_id=decision.decision_id),
    )
    state = state.model_copy(update={"run": state.run.model_copy(update={"receipts": (receipt,)})})
    event = TurnRequested(scope=turn.workspace, turn=turn)
    prepared = step(state, event)
    assert prepared == step(reload_state(state), event)
    assert len(prepared.requests) == 1
    assert isinstance(prepared.requests[0], EnsureSession)
    charges = project(prepared.state).attempts[0].charges
    assert sum(charge.charged for charge in charges if charge.kind == ChargeKind.ATTEMPT) == int(
        charge_class == "paid"
    )
    assert sum(charge.charged for charge in charges if charge.kind == ChargeKind.TURN) == 1
    assert prepared.state.sessions.sessions[0].phase == SessionPhase.ACQUIRING
    for _ in range(replays):
        repeated = step(reload_state(prepared.state), event)
        assert repeated.state.attempts == prepared.state.attempts
        assert repeated.requests == ()
        prepared = repeated
    pending = observation(
        prepared.state.sessions.sessions[0].pending_intents[0], ObservationStatus.PENDING
    ).model_copy(update={"terminal": False})
    observed = step(
        prepared.state, SessionObserved(session_id=turn.session.session_id, observation=pending)
    )
    assert not any(isinstance(request, DispatchTurn) for request in observed.requests)
    assert observed.state.attempts == prepared.state.attempts


def interrupted_replacement_state(
    charge_class: Literal["paid", "correction", "resume", "free"], refund: int = 1
) -> CoreState:
    state = checkpoint_state()
    original = state.sessions.invocations[0]
    claim = state.sessions.interrupts[0].model_copy(
        update={
            "phase": "completed",
            "refund": refund,
            "refunded_charge": ChargeId(root="paid") if refund else None,
        }
    )
    assert claim.checkpoint_authority is not None
    receipt = (
        state.attempts.attempts[0]
        .charges[0]
        .model_copy(
            update={"refunded": refund, "refund_sources": (claim.authority,) if refund else ()}
        )
    )
    replacement_ref = original.invocation.model_copy(
        update={"invocation_id": InvocationId(root="replacement")}
    )
    replacement = original.model_copy(
        update={
            "invocation": replacement_ref,
            "phase": SessionPhase.EXECUTING,
            "observation": None,
            "turn": original.turn.model_copy(
                update={
                    "invocation_id": replacement_ref.invocation_id,
                    "charge_class": charge_class,
                    "predecessor": original.invocation,
                }
            ),
        }
    )
    owner = state.attempts.attempts[0].model_copy(
        update={
            "charges": (receipt,),
            "budget": state.attempts.attempts[0].budget.model_copy(
                update={"paid_limit": 3, "retry_limit": 0}
            ),
            "checkpoints": (
                AttemptCheckpoint(
                    invocation=original.invocation,
                    request_id=claim.checkpoint_authority,
                    revision=state.run.facts.baseline,
                    retention="wip",
                ),
            ),
        }
    )
    session = state.sessions.sessions[0].model_copy(
        update={
            "phase": SessionPhase.CHECKPOINTED,
            "invocation": replacement_ref.invocation_id,
        }
    )
    return state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "sessions": state.sessions.model_copy(
                update={
                    "invocations": (original, replacement),
                    "interrupts": (claim,),
                    "sessions": (session,),
                }
            ),
        }
    )


@pytest.mark.parametrize("charge_class", ["paid", "free"])
@pytest.mark.parametrize("refund", [0, 1])
@given(replays=st.integers(min_value=1, max_value=4))
def test_completed_interruption_replacement_does_not_spend_correction_retry_currency(
    charge_class: Literal["paid", "correction", "resume", "free"], refund: int, replays: int
) -> None:
    state = interrupted_replacement_state(charge_class, refund)
    invocation = state.sessions.invocations[-1].invocation
    event = InvocationChargeRequested(attempt=owner_ref(), invocation=invocation)
    state = canonical_turns(state)
    result = step(reload_state(state), event)
    charges = project(result.state).attempts[0].charges
    target = tuple(row for row in charges if row.invocation_id == invocation.invocation_id)
    assert sum(row.charged for row in target if row.kind == ChargeKind.TURN) == 1
    assert sum(row.charged for row in target if row.kind == ChargeKind.ATTEMPT) == int(
        charge_class == "paid"
    )
    for _ in range(replays):
        duplicate = step(reload_state(result.state), event)
        assert duplicate.state.attempts == result.state.attempts
        assert duplicate.requests == ()
        result = duplicate


@pytest.mark.parametrize("charge_class", ["paid", "free"])
@pytest.mark.parametrize("proof", ["phase", "checkpoint", "receipt", "source"])
def test_interruption_replacement_requires_every_durable_proof(
    charge_class: Literal["paid", "correction", "resume", "free"], proof: str
) -> None:
    state = interrupted_replacement_state(charge_class)
    owner = state.attempts.attempts[0]
    claim = state.sessions.interrupts[0]
    if proof == "phase":
        claim = claim.model_copy(update={"phase": "checkpointed"})
    elif proof == "checkpoint":
        owner = owner.model_copy(update={"checkpoints": ()})
    elif proof == "receipt":
        claim = claim.model_copy(update={"refunded_charge": ChargeId(root="foreign")})
    else:
        owner = owner.model_copy(
            update={"charges": (owner.charges[0].model_copy(update={"refund_sources": ()}),)}
        )
    state = state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "sessions": state.sessions.model_copy(update={"interrupts": (claim,)}),
        }
    )
    result = step(
        state,
        InvocationChargeRequested(
            attempt=owner_ref(), invocation=state.sessions.invocations[-1].invocation
        ),
    )
    assert result.state.attempts == state.attempts
    assert result.requests == ()
    assert result.events == ()


@pytest.mark.parametrize("charge_class", ["paid", "free"])
def test_first_correction_after_interrupted_replacement_has_one_retry_edge(
    charge_class: Literal["paid", "correction", "resume", "free"],
) -> None:
    state = interrupted_replacement_state(charge_class)
    replacement = state.sessions.invocations[-1]
    state = canonical_turns(state)
    state = step(
        state, InvocationChargeRequested(attempt=owner_ref(), invocation=replacement.invocation)
    ).state
    replacement = replacement.model_copy(
        update={
            "phase": SessionPhase.TERMINAL,
            "observation": observation(RequestId(root="replacement-turn")),
        }
    )
    ref = replacement.invocation.model_copy(
        update={"invocation_id": InvocationId(root="correction")}
    )
    correction = replacement.model_copy(
        update={
            "invocation": ref,
            "phase": SessionPhase.EXECUTING,
            "observation": None,
            "turn": replacement.turn.model_copy(
                update={
                    "invocation_id": ref.invocation_id,
                    "charge_class": "correction",
                    "predecessor": replacement.invocation,
                }
            ),
        }
    )
    owner = state.attempts.attempts[0]
    owner = owner.model_copy(update={"budget": owner.budget.model_copy(update={"retry_limit": 1})})
    session = state.sessions.sessions[0].model_copy(update={"invocation": ref.invocation_id})
    state = state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "sessions": state.sessions.model_copy(
                update={
                    "invocations": (state.sessions.invocations[0], replacement, correction),
                    "sessions": (session,),
                }
            ),
        }
    )
    assert_charge_authorized(state, InvocationChargeRequested(attempt=owner_ref(), invocation=ref))


@pytest.mark.parametrize("capacity", [0, 1])
@pytest.mark.parametrize("bad_proof", ["authority", "checkpoint", "receipt-kind"])
def test_invalid_refund_cannot_emit_exhaustion_feedback(capacity: int, bad_proof: str) -> None:
    state = checkpoint_state()
    owner = state.attempts.attempts[0]
    invocation = state.sessions.invocations[0].invocation
    checkpoint = AttemptCheckpoint(
        invocation=invocation,
        request_id=RequestId(root="checkpoint"),
        revision=state.run.facts.baseline,
        retention="wip",
    )
    receipt = owner.charges[0]
    if bad_proof == "receipt-kind":
        receipt = receipt.model_copy(update={"kind": ChargeKind.ADMISSION})
    owner = owner.model_copy(update={"charges": (receipt,), "checkpoints": (checkpoint,)})
    claim = state.sessions.interrupts[0].model_copy(update={"phase": "checkpointed"})
    state = state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "sessions": state.sessions.model_copy(update={"interrupts": (claim,)}),
            "run": state.run.model_copy(
                update={"limits": Limits(max_turns=10, max_refunds=capacity)}
            ),
        }
    )
    event = AttemptChargeRefundRequested(
        attempt=owner_ref(),
        charge_id=receipt.charge_id,
        amount=1,
        reason="interrupted",
        authority=RequestId(root="foreign" if bad_proof == "authority" else "interrupt"),
        checkpoint_authority=RequestId(
            root="foreign" if bad_proof == "checkpoint" else "checkpoint"
        ),
    )
    result = step(reload_state(state), event)
    assert result.state.attempts == state.attempts
    assert result.events == ()
    assert result.requests == ()


@pytest.mark.parametrize("phase", ["pending", "draining"])
def test_checkpoint_can_bind_unique_interrupt_authority_before_checkpoint_field_is_set(
    phase: str,
) -> None:
    state = checkpoint_state()
    claim = state.sessions.interrupts[0].model_copy(
        update={"phase": phase, "checkpoint_authority": None}
    )
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"interrupts": (claim,)})}
    )
    event = InvocationCheckpointRequested(
        attempt=owner_ref(), invocation=claim.invocation, retention="wip", authority=claim.authority
    )
    result = step(reload_state(state), event)
    assert len(result.requests) == 1
    assert isinstance(result.requests[0], SnapshotAndRetain)
    assert result.requests[0].request_id == claim.authority
    assert project(result.state).attempts[0].checkpoints == ()


def test_checkpoint_completion_rejects_mismatched_canonical_observation_request() -> None:
    state = checkpoint_state()
    invocation = state.sessions.invocations[0].invocation
    requested = step(
        state,
        InvocationCheckpointRequested(
            attempt=owner_ref(),
            invocation=invocation,
            retention="wip",
            authority=RequestId(root="checkpoint"),
        ),
    )
    intent = requested.state.intents.intents[0]
    intent = intent.model_copy(
        update={
            "phase": IntentPhase.COMPLETED,
            "observation": observation(RequestId(root="foreign")),
        }
    )
    state = requested.state.model_copy(
        update={"intents": requested.state.intents.model_copy(update={"intents": (intent,)})}
    )
    event = InvocationCheckpointed(
        attempt=owner_ref(),
        invocation=invocation,
        checkpoint_request=RequestId(root="checkpoint"),
        revision=state.run.facts.baseline,
        charge=state.attempts.attempts[0].charges[0],
    )
    result = step(reload_state(state), event)
    assert result.state.attempts == state.attempts
    assert result.requests == ()
    assert result.events == ()


@pytest.mark.parametrize("status", [ObservationStatus.UNKNOWN, ObservationStatus.SUCCEEDED])
@pytest.mark.parametrize("terminal", [False, True])
def test_registered_revision_unknown_acceptance_inspects_and_preserves_pending_proof(
    status: ObservationStatus, *, terminal: bool
) -> None:
    state, codec, request = revision_operation_state()
    state = step(state, request).state
    assert request.request.request_id is not None
    observed = observation(request.request.request_id, status).model_copy(
        update={"accepted": False, "terminal": terminal}
    )
    intent = state.intents.intents[0].model_copy(
        update={"phase": IntentPhase.RECONCILING, "observation": observed}
    )
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"intents": (intent,)})}
    )
    event = RevisionOperationObserved(
        operation_id=request.request.operation_id,
        observation=observed,
        revision=state.run.facts.baseline,
    )
    result = step(reload_state(state, codec), event)
    assert result.state.attempts == state.attempts
    assert len(result.requests) == 1
    assert isinstance(result.requests[0], InspectRequest)
    assert result.requests[0].target == request.request.request_id


def test_checkpoint_authority_must_identify_exactly_one_interruption() -> None:
    state = checkpoint_state()
    claim = state.sessions.interrupts[0].model_copy(update={"checkpoint_authority": None})
    conflicting = claim.model_copy(
        update={
            "invocation": claim.invocation.model_copy(
                update={"invocation_id": InvocationId(root="other")}
            ),
            "authority": RequestId(root="other-interrupt"),
            "checkpoint_authority": claim.authority,
        }
    )
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"interrupts": (claim, conflicting)})}
    )
    event = InvocationCheckpointRequested(
        attempt=owner_ref(), invocation=claim.invocation, retention="wip", authority=claim.authority
    )
    with pytest.raises(KernelNotImplementedError) as raised:
        step(reload_state(state), event)
    assert raised.value.subarea == "_attempt_acquisition"
    assert raised.value.event_kind == "invocation_checkpoint_requested"
