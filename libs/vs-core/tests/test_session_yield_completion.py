"""Yield semantic completion through the published session transition API."""

import hashlib
import json

import pytest

import vs_core.api as core


def yield_state() -> tuple[core.CoreState, core.DispatchTurn, core.Continuation]:
    """Seed authentic immutable sibling authority without executing its stub."""
    state = core.initial_state()
    attempt_id = core.AttemptId(root="yield-owner")
    owner_scope = core.Scope(owner=attempt_id, generation=0)
    admission = core.DecisionId(root="yield-admission")
    spec = core.TurnSpec(
        session=core.SessionSpec(
            session_id=core.SessionId(root="yield-session"),
            role_id=core.RoleId(root="worker"),
            policy="reuse",
            lifetime="owner",
            access=core.Access.WRITE_CANDIDATE,
        ),
        invocation_id=core.InvocationId(root="yield-turn"),
        workspace=owner_scope,
        prompts=(),
        output_schema=core.SchemaRef(name="worker", version=1),
        deadline_at=100.0,
        charge_class="paid",
    )
    ref = core.InvocationRef(
        session_id=spec.session.session_id, invocation_id=spec.invocation_id, generation=0
    )
    request = core.DispatchTurn(
        request_id=core.RequestId(root="yield-dispatch"),
        decision_id=core.DecisionId(root="yield-decision"),
        scope=owner_scope,
        admission_id=admission,
        deadline_at=100.0,
        turn=spec,
    )
    assert request.request_id is not None
    intent = core.Intent(
        request_id=request.request_id,
        request=request,
        payload_digest=hashlib.sha256(
            json.dumps(
                request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
        lifecycle=core.LifecycleClass.SESSION_TURN,
        phase=core.IntentPhase.DISPATCHED,
        reconcile_deadline_at=100.0,
    )
    owner = core.AttemptView(
        attempt_id=attempt_id,
        item_id=core.ItemId(root="yield-item"),
        generation=0,
        phase=core.AttemptPhase.ACTIVE,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
        admission_id=admission,
    )
    invocation = core.Invocation(
        invocation=ref, scope=owner_scope, turn=spec, phase=core.SessionPhase.EXECUTING
    )
    session = core.SessionView(
        spec=spec.session,
        scope=owner_scope,
        generation=0,
        phase=core.SessionPhase.EXECUTING,
        invocation=spec.invocation_id,
        resource_id=core.ResourceId(root="yield-conversation"),
    )
    continuation = core.Continuation(
        continuation_id=core.ContinuationId(root="yield-wait"),
        invocation=ref,
        next_invocation=ref.model_copy(update={"invocation_id": core.InvocationId(root="resume")}),
        jobs=(),
        deadline_at=80.0,
        phase=core.ContinuationPhase.WAITING,
    )
    return (
        state.model_copy(
            update={
                "attempts": core.AttemptsState(attempts=(owner,)),
                "sessions": core.SessionsState(sessions=(session,), invocations=(invocation,)),
                "intents": core.IntentsState(intents=(intent,)),
            }
        ),
        request,
        continuation,
    )


@pytest.mark.parametrize("suspended", [False, True])
@pytest.mark.parametrize(
    "status",
    [
        core.ObservationStatus.SUCCEEDED,
        core.ObservationStatus.FAILED,
        core.ObservationStatus.CANCELLED,
    ],
)
def test_suspended_success_does_not_complete_before_retained_checkpoint(
    *, suspended: bool, status: core.ObservationStatus
) -> None:
    """The public session API exposes signals before unavailable sibling dispatch."""
    state, request, suspension = yield_state()
    assert request.request_id is not None
    assert request.decision_id is not None
    observation = core.Observation(
        event_id=core.EventId(root="yield-observed"),
        request_id=request.request_id,
        scope=request.scope,
        admission_id=request.admission_id,
        sequence=1,
        observed_at=1.0,
        status=status,
        accepted=True,
        terminal=True,
        resource_id=core.ResourceId(root="yield-conversation"),
    )
    event = core.TurnObserved(
        invocation=state.sessions.invocations[0].invocation,
        observation=observation,
        suspension=suspension if suspended else None,
    )
    context = core.SessionsContext(
        run=state.run,
        attempts=state.attempts,
        evaluation=state.evaluation,
        intents=state.intents,
    )
    before = state.model_dump_json()
    result = core.advance_session(state.sessions, context, event)
    completion = tuple(row for row in result.signals if isinstance(row, core.DecisionCompleted))
    if suspended and status == core.ObservationStatus.SUCCEEDED:
        assert completion == ()
        assert any(isinstance(row, core.InvocationCheckpointRequested) for row in result.signals)
    else:
        expected = {
            core.ObservationStatus.SUCCEEDED: core.CompletionStatus.SUCCEEDED,
            core.ObservationStatus.FAILED: core.CompletionStatus.FAILED,
            core.ObservationStatus.CANCELLED: core.CompletionStatus.CANCELLED,
        }[status]
        assert completion == (
            core.DecisionCompleted(decision_id=request.decision_id, status=expected),
        )
    replay = core.advance_session(result.state, context, event)
    assert replay.signals == ()
    assert replay.events == ()
    assert state.model_dump_json() == before
