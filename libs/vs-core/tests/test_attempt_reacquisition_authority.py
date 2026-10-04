"""Continuation-source authority across composed restore and reacquisition."""

import json
from hashlib import sha256
from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import (
    Access,
    AttemptBudget,
    AttemptCheckpoint,
    AttemptClosure,
    AttemptId,
    AttemptPhase,
    AttemptReacquireRequested,
    AttemptRef,
    AttemptsState,
    AttemptView,
    Continuation,
    ContinuationId,
    ContinuationPhase,
    CoreState,
    DecisionId,
    EnsureSession,
    EventId,
    Intent,
    IntentPhase,
    Invocation,
    InvocationId,
    InvocationRef,
    ItemId,
    LifecycleClass,
    Observation,
    ObservationStatus,
    RequestId,
    ResourceId,
    RestoreRevision,
    RoleId,
    RunId,
    SchemaRef,
    Scope,
    SessionAcquisitionGroup,
    SessionId,
    SessionObserved,
    SessionPhase,
    SessionSpec,
    SessionView,
    TurnSpec,
    WorkspaceMode,
    WorkspaceObserved,
    WorkspacePlan,
    initial_state,
    step,
)

SourceVariant = Literal[
    "absent",
    "generation",
    "scope",
    "resource",
    "invocation",
    "exact",
    "run",
    "foreign-run",
    "stale-run",
    "foreign-attempt",
]


def retained_reopen(
    *,
    initial_manifest: bool = True,
    source_variant: SourceVariant = "exact",
    terminal_session: bool = False,
    current_closure: bool = False,
    admission: Literal["absent", "stale", "current"] = "current",
) -> tuple[CoreState, AttemptReacquireRequested]:
    state = initial_state()
    scope = Scope(owner=AttemptId(root="owner"), generation=0)
    source = InvocationRef(
        session_id=SessionId(root="dynamic-session"),
        invocation_id=InvocationId(root="yielded"),
        generation=0,
    )
    spec = SessionSpec(
        session_id=source.session_id,
        role_id=RoleId(root="worker"),
        policy="reuse"
        if source_variant in ("run", "foreign-run", "stale-run", "foreign-attempt")
        else "fresh",
        lifetime="owner",
        access=Access.WRITE_CANDIDATE,
    )
    turn = TurnSpec(
        session=spec,
        invocation_id=source.invocation_id,
        workspace=scope,
        prompts=(),
        output_schema=SchemaRef(name="output", version=1),
        deadline_at=1000,
        charge_class="paid",
    )
    invocation = Invocation(invocation=source, scope=scope, turn=turn, phase=SessionPhase.SUSPENDED)
    continuation = Continuation(
        continuation_id=ContinuationId(root="continuation"),
        invocation=source,
        next_invocation=source.model_copy(update={"invocation_id": InvocationId(root="resume")}),
        jobs=(),
        deadline_at=1000,
        phase=ContinuationPhase.REOPENING,
        park_authority=RequestId(root="park"),
        reopen_authority=RequestId(root="reopen"),
    )
    assert isinstance(scope.owner, AttemptId)
    assert continuation.park_authority is not None
    owner = AttemptView(
        attempt_id=scope.owner,
        item_id=ItemId(root="item"),
        generation=0,
        phase=AttemptPhase.ACQUIRING,
        admission_id=None if admission == "absent" else DecisionId(root="new-episode"),
        workspace=WorkspacePlan(mode=WorkspaceMode.ISOLATED_CHILD, base=state.run.facts.baseline),
        budget=AttemptBudget(),
        sessions=(source.session_id,) if initial_manifest else (),
        closure=AttemptClosure(
            disposition="park",
            requested_at=0,
            authority=continuation.park_authority,
            admission_id=DecisionId(root="new-episode" if current_closure else "old-episode"),
        ),
        checkpoints=(
            AttemptCheckpoint(
                invocation=source,
                request_id=RequestId(root="checkpoint"),
                revision=state.run.facts.baseline,
                retention="wip",
            ),
        ),
    )
    retained = SessionView(
        spec=spec,
        scope={
            "scope": Scope(owner=AttemptId(root="other"), generation=0),
            "foreign-attempt": Scope(owner=AttemptId(root="other"), generation=0),
            "run": Scope(owner=state.run.run_id, generation=state.run.generation),
            "foreign-run": Scope(owner=RunId(root="foreign"), generation=state.run.generation),
            "stale-run": Scope(owner=state.run.run_id, generation=state.run.generation + 1),
        }.get(source_variant, scope),
        generation=999 if source_variant == "generation" else 0,
        phase=SessionPhase.TERMINAL if terminal_session else SessionPhase.SUSPENDED,
        resource_id=None if source_variant == "resource" else ResourceId(root="retained"),
        invocation=InvocationId(root="other")
        if source_variant == "invocation"
        else source.invocation_id,
    )
    persisted = state.model_copy(
        update={
            "attempts": AttemptsState(attempts=(owner,)),
            "sessions": state.sessions.model_copy(
                update={
                    "invocations": (invocation,),
                    "sessions": () if source_variant == "absent" else (retained,),
                }
            ),
            "evaluation": state.evaluation.model_copy(update={"continuations": (continuation,)}),
        }
    )
    assert owner.checkpoint is not None
    event = AttemptReacquireRequested(
        attempt=AttemptRef(attempt_id=owner.attempt_id, generation=0),
        continuation_id=continuation.continuation_id,
        request_id=RequestId(root="reopen"),
        admission_id=DecisionId(root="stale" if admission == "stale" else "new-episode"),
        base=owner.checkpoint,
    )
    return CoreState.model_validate(persisted.model_dump()), event


def assert_ready_forwarded_without_registered_dispatch(
    state: CoreState, requests: tuple[object, ...]
) -> None:
    """Retirement owns dispatch, and it needs the accepted reopen operation.

    These fixtures prove acquisition's readiness gate only. They carry no
    accepted scope-reopen decision, so retirement correctly dispatches
    nothing and the attempt keeps acquiring. The dispatch itself is covered
    in test_attempt_retirement against a complete reopen fixture.
    """
    assert requests == ()
    assert state.attempts.attempts[0].phase == AttemptPhase.ACQUIRING


def complete_restore(state: CoreState, event: AttemptReacquireRequested) -> CoreState:
    intent = next(row for row in state.intents.intents if isinstance(row.request, RestoreRevision))
    observed = Observation(
        event_id=EventId(root="restored"),
        request_id=intent.request_id,
        scope=intent.request.scope,
        sequence=1,
        observed_at=1,
        status=ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
        admission_id=event.admission_id,
    )
    committed = state.model_copy(
        update={
            "intents": state.intents.model_copy(
                update={
                    "intents": tuple(
                        row.model_copy(
                            update={"phase": IntentPhase.COMPLETED, "observation": observed}
                        )
                        if row == intent
                        else row
                        for row in state.intents.intents
                    )
                }
            )
        }
    )
    result = step(
        CoreState.model_validate(committed.model_dump()),
        WorkspaceObserved(attempt=event.attempt, observation=observed, revision=event.base),
    )
    assert result.requests == ()
    return result.state


def test_nonempty_retained_reopen_has_session_work_after_restore() -> None:
    """Finding 3: old park closure must not strand the current episode."""
    state, event = retained_reopen()
    prepared = step(state, event)
    assert any(isinstance(request, RestoreRevision) for request in prepared.requests)
    assert len(prepared.state.sessions.acquisition_groups) == 1
    assert any(isinstance(request, EnsureSession) for request in prepared.requests)
    restored = complete_restore(prepared.state, event)
    assert restored.attempts.attempts[0].closure == state.attempts.attempts[0].closure
    assert restored.sessions.acquisition_groups[0].phase == "acquiring"
    assert restored.sessions.sessions[0].pending_intents


def test_empty_initial_manifest_cannot_restore_without_dynamic_source() -> None:
    """Finding 5: a later-acquired continuation session remains required."""
    state, event = retained_reopen(initial_manifest=False, source_variant="absent")
    result = step(state, event)
    assert result.requests == ()
    assert result.state.attempts == state.attempts
    assert result.state.sessions.acquisition_groups == ()


@pytest.mark.parametrize("admission", ["absent", "stale", "current"])
@pytest.mark.parametrize("initial_manifest", [False, True])
@pytest.mark.parametrize(
    "source_variant",
    [
        "absent",
        "generation",
        "scope",
        "resource",
        "invocation",
        "exact",
        "run",
        "foreign-run",
        "stale-run",
        "foreign-attempt",
    ],
)
@pytest.mark.parametrize(
    "lease", [(terminal, current) for terminal in (False, True) for current in (False, True)]
)
@given(replays=st.integers(min_value=1, max_value=3))
def test_retained_reopen_authority_matrix(
    *,
    initial_manifest: bool,
    source_variant: SourceVariant,
    lease: tuple[bool, bool],
    replays: int,
    admission: Literal["absent", "stale", "current"],
) -> None:
    terminal_session, current_closure = lease
    state, event = retained_reopen(
        initial_manifest=initial_manifest,
        source_variant=source_variant,
        terminal_session=terminal_session,
        current_closure=current_closure,
        admission=admission,
    )
    prepared = step(state, event)
    authorized = (
        source_variant in ("exact", "run") and not current_closure and admission == "current"
    )
    assert bool(prepared.requests) == authorized
    assert prepared.state.attempts.attempts[0].closure == state.attempts.attempts[0].closure
    if not authorized:
        assert prepared.state.sessions.acquisition_groups == ()
        return
    assert len(prepared.state.sessions.acquisition_groups) == 1
    group = prepared.state.sessions.acquisition_groups[0]
    assert group.session_ids == (state.evaluation.continuations[0].invocation.session_id,)
    restored = complete_restore(prepared.state, event)
    for _ in range(replays):
        repeated = step(CoreState.model_validate(restored.model_dump()), event)
        assert repeated.requests == ()
        assert repeated.state.sessions.acquisition_groups == restored.sessions.acquisition_groups
        restored = repeated.state
    request = next(
        row.request for row in restored.intents.intents if isinstance(row.request, EnsureSession)
    )
    assert request.request_id is not None
    observed = Observation(
        event_id=EventId(root="reattached"),
        request_id=request.request_id,
        scope=request.scope,
        sequence=1,
        observed_at=2,
        status=ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
        resource_id=request.required_resource,
        admission_id=event.admission_id,
    )
    committed = restored.model_copy(
        update={
            "intents": restored.intents.model_copy(
                update={
                    "intents": tuple(
                        row.model_copy(
                            update={"phase": IntentPhase.COMPLETED, "observation": observed}
                        )
                        if row.request_id == request.request_id
                        else row
                        for row in restored.intents.intents
                    )
                }
            )
        }
    )
    result = step(
        CoreState.model_validate(committed.model_dump()),
        SessionObserved(session_id=request.spec.session_id, observation=observed),
    )
    assert_ready_forwarded_without_registered_dispatch(result.state, result.requests)


@pytest.mark.parametrize("initial_manifest", [False, True])
@pytest.mark.parametrize("source_variant", ["exact", "run"])
@pytest.mark.parametrize("proof", ["absent", "rejected", "stale", "unrelated", "exact"])
def test_reacquisition_readiness_requires_canonical_session_acquisition(
    *,
    initial_manifest: bool,
    source_variant: SourceVariant,
    proof: str,
) -> None:
    """A ready group cannot substitute for the source's canonical lease proof."""
    state, event = retained_reopen(initial_manifest=initial_manifest, source_variant=source_variant)
    prepared = step(state, event).state
    session = prepared.sessions.sessions[0].model_copy(
        update={"phase": SessionPhase.SUSPENDED, "pending_intents": ()}
    )
    request_id = RequestId(root="retained-acquisition")
    request = EnsureSession(
        request_id=request_id,
        scope=session.scope,
        admission_id=event.admission_id,
        deadline_at=1000,
        spec=session.spec,
        required_resource=session.resource_id,
    )
    payload_digest = sha256(
        json.dumps(
            request.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    acquisition = Intent(
        request_id=request_id,
        request=request,
        payload_digest=payload_digest,
        lifecycle=LifecycleClass.IDEMPOTENT_WRITE,
        phase=IntentPhase.COMPLETED,
        reconcile_deadline_at=1000,
    )
    observed = Observation(
        event_id=EventId(root="retained-proof"),
        request_id=RequestId(root="unrelated") if proof == "unrelated" else acquisition.request_id,
        scope=acquisition.request.scope,
        sequence=1,
        observed_at=1,
        status=ObservationStatus.REJECTED if proof == "rejected" else ObservationStatus.SUCCEEDED,
        accepted=proof != "rejected",
        terminal=True,
        admission_id=DecisionId(root="old-episode") if proof == "stale" else event.admission_id,
        resource_id=ResourceId(root="retained"),
    )
    canonical = acquisition.model_copy(
        update={"phase": IntentPhase.COMPLETED, "observation": observed}
    )
    group = SessionAcquisitionGroup(
        attempt=event.attempt,
        admission_id=event.admission_id,
        scope=state.sessions.invocations[0].scope,
        session_ids=(session.spec.session_id,),
        phase="ready",
    )
    persisted = prepared.model_copy(
        update={
            "sessions": prepared.sessions.model_copy(
                update={"sessions": (session,), "acquisition_groups": (group,)}
            ),
            "intents": prepared.intents.model_copy(
                update={
                    "intents": (
                        *tuple(
                            row
                            for row in prepared.intents.intents
                            if not isinstance(row.request, EnsureSession)
                        ),
                        *((canonical,) if proof != "absent" else ()),
                    )
                }
            ),
        }
    )
    persisted = CoreState.model_validate(persisted.model_dump())
    if proof == "exact":
        restored = complete_restore(persisted, event)
        assert_ready_forwarded_without_registered_dispatch(restored, ())
    else:
        restored = complete_restore(persisted, event)
        assert restored.attempts.attempts[0].phase == AttemptPhase.ACQUIRING
