"""Prepared recovery preserves occupancy and cleanup admission authority."""

from typing import ClassVar, Literal, TypedDict

from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import BaseModel

import vs_core.api as core


class CleanupOutcome(core.Value):
    retained: bool


class CleanupRequest(core.OperationRequest):
    kind: Literal["test.revision.cleanup"] = "test.revision.cleanup"
    lifecycle: Literal[core.LifecycleClass.IDEMPOTENT_WRITE] = core.LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = CleanupOutcome


class RequestFields(TypedDict):
    request_id: core.RequestId
    scope: core.Scope
    admission_id: core.DecisionId
    deadline_at: float


def episode_state(
    phase: core.AttemptPhase, kind: str, authority: str, *, matching_admission: bool
) -> tuple[core.CoreState, core.Intent, bool]:
    """Persist a prepared request alongside one independently unresolved anchor."""
    state = core.initial_state()
    attempt = core.AttemptRef(attempt_id=core.AttemptId(root="attempt"), generation=0)
    admission = core.DecisionId(root="admission")
    scope = core.Scope(owner=attempt.attempt_id, generation=0)
    identity = core.RequestId(root="prepared")
    common: RequestFields = {
        "request_id": identity,
        "scope": scope,
        "admission_id": admission if matching_admission else core.DecisionId(root="old"),
        "deadline_at": 100.0,
    }
    session = core.SessionId(root="session")
    requests = {
        "ensure": core.EnsureSession(
            **common,
            spec=core.SessionSpec(
                session_id=session,
                role_id=core.RoleId(root="worker"),
                policy="reuse",
                lifetime="owner",
                access=core.Access.WRITE_ARTIFACTS,
            ),
        ),
        "close-scope": core.CloseAttemptScope(**common, attempt=attempt),
        "close-session": core.CloseSession(**common, session_id=session),
        "discard": core.DiscardWorkspace(**common, attempt=attempt),
        "retain": core.RetainRevision(
            **common, attempt=attempt, revision=state.run.facts.baseline, retention="wip"
        ),
        "snapshot": core.SnapshotAndRetain(**common, attempt=attempt, retention="wip"),
    }
    request = requests[kind]
    closure = None
    dependencies = ()
    if authority != "none":
        closure = core.AttemptClosure(
            disposition="park",
            requested_at=1.0,
            authority=identity if authority == "request" else core.RequestId(root="retirement"),
            admission_id=admission,
        )
        if authority == "dependency":
            dependencies = (core.ReleaseDependency(kind="request", identity=identity),)
        if authority == "session":
            dependencies = (core.ReleaseDependency(kind="session", identity=session),)
    owner = core.AttemptView(
        attempt_id=attempt.attempt_id,
        item_id=core.ItemId(root="item"),
        generation=0,
        phase=phase,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
        admission_id=admission,
        closure=closure,
        release_dependencies=dependencies,
    )
    prepared = core.Intent(
        request_id=identity,
        request=request,
        payload_digest="prepared",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        phase=core.IntentPhase.PREPARED,
        reconcile_deadline_at=100.0,
    )
    anchor_request = requests["ensure"].model_copy(
        update={"request_id": core.RequestId(root="anchor")}
    )
    anchor = prepared.model_copy(
        update={
            "request_id": anchor_request.request_id,
            "request": anchor_request,
            "phase": core.IntentPhase.DISPATCHED,
        }
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"status": core.RunStatus.PAUSED}),
            "attempts": core.AttemptsState(attempts=(owner,)),
            "intents": core.IntentsState(intents=(prepared, anchor)),
        }
    )
    active = phase in (core.AttemptPhase.ACTIVE, core.AttemptPhase.ACQUIRING)
    cleanup_authorized = kind != "ensure" and (
        authority in ("request", "dependency")
        or (authority == "session" and kind == "close-session")
    )
    return state, prepared, matching_admission and (active or cleanup_authorized)


@example(phase=core.AttemptPhase.CLOSING, kind="snapshot", authority="dependency", matching=True)
@example(phase=core.AttemptPhase.PARKED, kind="close-session", authority="session", matching=True)
@given(
    phase=st.sampled_from(list(core.AttemptPhase)),
    kind=st.sampled_from(
        ["ensure", "close-scope", "close-session", "discard", "retain", "snapshot"]
    ),
    authority=st.sampled_from(["none", "request", "dependency", "session", "foreign"]),
    matching=st.booleans(),
)
def test_prepared_cleanup_recovers_only_with_current_episode_authority(
    phase: core.AttemptPhase, kind: str, authority: str, *, matching: bool
) -> None:
    """An unwritten request needs active admission or exact retiring cleanup proof."""
    state, prepared, authorized = episode_state(phase, kind, authority, matching_admission=matching)
    event = core.RecoveryStarted(epoch=1, now_at=10.0)
    result = core.step(state, event)
    assert result == core.step(core.CoreState.model_validate_json(state.model_dump_json()), event)
    check = next(
        row for row in result.state.intents.recovery.checks if row.target == prepared.request_id
    )
    assert check.resolution == ("safe-prepared" if authorized else "pending")
    inspections = {row.target for row in result.requests if isinstance(row, core.InspectRequest)}
    assert (prepared.request_id in inspections) is not authorized
    assert result.state.attempts == state.attempts
    assert result.state.intents.intents[0] == prepared
    assert result.state.run.receipts == state.run.receipts
    assert result.events == ()


@given(
    revision_authority=st.sampled_from(list(core.RevisionAuthority)),
    dependency=st.sampled_from(["operation", "request", "foreign"]),
)
def test_prepared_registered_cleanup_uses_descriptor_and_exact_release_identity(
    revision_authority: core.RevisionAuthority, dependency: str
) -> None:
    """A closed admission only permits declared revision cleanup on its release graph."""
    state, prepared, _ = episode_state(
        core.AttemptPhase.CLOSING, "snapshot", "foreign", matching_admission=True
    )
    descriptor = core.OperationDescriptor(
        kind="test.revision.cleanup",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        request_schema=core.SchemaRef(name="cleanup", version=1),
        outcome_schema=core.SchemaRef(name="cleanup-outcome", version=1),
        revision_authority=revision_authority,
        inspect=True,
    )
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor, request_model=CleanupRequest, outcome_model=CleanupOutcome
            ),
        )
    )
    operation = core.OperationId(root="cleanup-operation")
    request = core.ExecuteRegisteredOperation(
        request_id=prepared.request_id,
        scope=prepared.request.scope,
        admission_id=prepared.request.admission_id,
        deadline_at=100.0,
        operation_id=operation,
        operation=codec.encode(CleanupRequest()),
        retry_limit=0,
    )
    record = prepared.model_copy(update={"request": request})
    release = core.ReleaseDependency(
        kind="operation" if dependency == "operation" else "request",
        identity=operation
        if dependency == "operation"
        else prepared.request_id
        if dependency == "request"
        else core.RequestId(root="foreign"),
    )
    owner = state.attempts.attempts[0].model_copy(update={"release_dependencies": (release,)})
    state = state.model_copy(
        update={
            "registry": (descriptor,),
            "run": state.run.model_copy(
                update={"capabilities": core.Capabilities(operations=(descriptor,))}
            ),
            "attempts": core.AttemptsState(attempts=(owner,)),
            "intents": state.intents.model_copy(
                update={"intents": (record, state.intents.intents[1])}
            ),
        }
    )
    result = core.step(state, core.RecoveryStarted(epoch=1, now_at=10.0))
    check = next(
        row for row in result.state.intents.recovery.checks if row.target == prepared.request_id
    )
    cleanup = revision_authority in (
        core.RevisionAuthority.SNAPSHOT,
        core.RevisionAuthority.RETAIN,
        core.RevisionAuthority.DISCARD,
    )
    authorized = cleanup and dependency != "foreign"
    assert check.resolution == ("safe-prepared" if authorized else "pending")
    assert result.state.attempts == state.attempts
    assert result.state.intents.intents[0] == record
    assert result.state.run.receipts == state.run.receipts
    assert result.events == ()
