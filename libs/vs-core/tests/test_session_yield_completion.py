"""Yield completion requires committed checkpoints through the public kernel step."""

import hashlib
import json
from typing import Literal

import pytest

import vs_core.api as core

from .proof_digest import value_digest
from .test_session_sibling_fakes import fake_attempts, fake_evaluation, fake_session_inputs

REDUCERS = core.CoreReducers(
    attempts=fake_attempts, evaluation=fake_evaluation, session_inputs=fake_session_inputs
)


def yield_state(
    origin: Literal["dispatch", "resume"] = "dispatch",
) -> tuple[core.CoreState, core.DispatchTurn | core.ResumeSessionTurn, core.Continuation]:
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
    if origin == "resume":
        continuation_id = core.ContinuationId(root="previous-yield")
        spec = spec.model_copy(
            update={"charge_class": "resume", "continuation_id": continuation_id}
        )
        request = core.ResumeSessionTurn(
            request_id=request.request_id,
            decision_id=request.decision_id,
            scope=owner_scope,
            admission_id=admission,
            deadline_at=100.0,
            turn=spec,
            continuation_id=continuation_id,
        )
    assert request.request_id is not None
    assert request.decision_id is not None
    decision = core.RequestTurn(decision_id=request.decision_id, scope=owner_scope, turn=spec)
    receipt = core.DecisionReceipt(
        decision_id=request.decision_id,
        decision=decision,
        payload_digest=value_digest(decision),
        feedback=core.Accepted(decision_id=request.decision_id),
        request_ids=(request.request_id,),
    )
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
                "run": state.run.model_copy(update={"receipts": (receipt,)}),
                "attempts": core.AttemptsState(attempts=(owner,)),
                "sessions": core.SessionsState(sessions=(session,), invocations=(invocation,)),
                "intents": core.IntentsState(intents=(intent,)),
            }
        ),
        request,
        continuation,
    )


def terminal_yield(
    state: core.CoreState,
    request: core.DispatchTurn | core.ResumeSessionTurn,
    suspension: core.Continuation | None,
    status: core.ObservationStatus = core.ObservationStatus.SUCCEEDED,
) -> core.TurnObserved:
    assert request.request_id is not None
    return core.TurnObserved(
        invocation=state.sessions.invocations[0].invocation,
        observation=core.Observation(
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
        ),
        suspension=suspension,
    )


@pytest.mark.parametrize("origin", ["dispatch", "resume"])
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
    *, origin: Literal["dispatch", "resume"], suspended: bool, status: core.ObservationStatus
) -> None:
    state, request, suspension = yield_state(origin)
    event = terminal_yield(state, request, suspension if suspended else None, status)
    before = state.model_dump_json()
    result = core.step(state, event, reducers=REDUCERS)
    completion = result.state.run.receipts[0].completion
    if suspended and status == core.ObservationStatus.SUCCEEDED:
        assert completion is None
        assert result.state.evaluation.continuations == ()
        assert result.state.sessions.invocations[0].pending_suspension == suspension
        assert any(isinstance(row, core.SnapshotAndRetain) for row in result.requests)
    else:
        assert (
            completion
            == {
                core.ObservationStatus.SUCCEEDED: core.CompletionStatus.SUCCEEDED,
                core.ObservationStatus.FAILED: core.CompletionStatus.FAILED,
                core.ObservationStatus.CANCELLED: core.CompletionStatus.CANCELLED,
            }[status]
        )
    replay = core.step(result.state, event, reducers=REDUCERS)
    assert replay.requests == ()
    assert replay.events == ()
    assert state.model_dump_json() == before


def yielded_checkpoint(
    origin: Literal["dispatch", "resume"] = "dispatch",
) -> tuple[core.CoreState, core.InvocationCheckpointAvailable, core.SnapshotAndRetain]:
    state, request, suspension = yield_state(origin)
    result = core.step(state, terminal_yield(state, request, suspension), reducers=REDUCERS)
    checkpoint_request = next(
        row for row in result.requests if isinstance(row, core.SnapshotAndRetain)
    )
    assert checkpoint_request.request_id is not None
    event = core.InvocationCheckpointAvailable(
        invocation=state.sessions.invocations[0].invocation,
        request_id=checkpoint_request.request_id,
        revision=state.run.facts.baseline,
        retention="wip",
    )
    # Seed the pending semantic receipt at the checkpoint boundary independently
    # of the terminal-observation implementation under comparison.
    pending_run = result.state.run.model_copy(
        update={
            "receipts": tuple(
                row.model_copy(update={"completion": None}) for row in result.state.run.receipts
            )
        }
    )
    return result.state.model_copy(update={"run": pending_run}), event, checkpoint_request


def commit_checkpoint(
    state: core.CoreState, event: core.InvocationCheckpointAvailable
) -> core.CoreState:
    """Supply committed sibling proof, not a Sessions A assertion of retention."""
    owner = state.attempts.attempts[0]
    row = core.AttemptCheckpoint(
        invocation=event.invocation,
        request_id=event.request_id,
        revision=event.revision,
        retention=event.retention,
    )
    return state.model_copy(
        update={
            "attempts": core.AttemptsState(
                attempts=(owner.model_copy(update={"checkpoints": (row,)}),)
            )
        }
    )


@pytest.mark.parametrize("origin", ["dispatch", "resume"])
def test_yielded_decision_completes_on_exact_retained_wip_once(
    origin: Literal["dispatch", "resume"],
) -> None:
    state, event, _ = yielded_checkpoint(origin)
    state = commit_checkpoint(state, event)
    before = state.model_dump_json()
    result = core.step(state, event, reducers=REDUCERS)
    assert result.state.run.receipts[0].completion == core.CompletionStatus.SUCCEEDED
    assert result.state.sessions.invocations[0].phase == core.SessionPhase.SUSPENDED
    assert result.state.sessions.invocations[0].pending_suspension is None
    assert len(result.state.evaluation.continuations) == 1
    replay = core.step(result.state, event, reducers=REDUCERS)
    assert replay.requests == ()
    assert replay.events == ()
    assert replay.state.run.receipts == result.state.run.receipts
    assert replay.state.sessions == result.state.sessions
    assert state.model_dump_json() == before


@pytest.mark.parametrize(
    "mismatch",
    ["invocation", "generation", "request", "revision", "retention", "missing", "origin"],
)
def test_yielded_success_ignores_uncommitted_or_mismatched_checkpoint(mismatch: str) -> None:
    state, event, _ = yielded_checkpoint()
    state = commit_checkpoint(state, event)
    match mismatch:
        case "invocation":
            event = event.model_copy(
                update={
                    "invocation": event.invocation.model_copy(
                        update={"invocation_id": core.InvocationId(root="orphan")}
                    )
                }
            )
        case "generation":
            event = event.model_copy(
                update={"invocation": event.invocation.model_copy(update={"generation": 1})}
            )
        case "request":
            event = event.model_copy(
                update={"request_id": core.RequestId(root="unrelated-checkpoint")}
            )
        case "revision":
            event = event.model_copy(
                update={
                    "revision": event.revision.model_copy(
                        update={"revision_id": core.RevisionId(root="wrong")}
                    )
                }
            )
        case "retention":
            event = event.model_copy(update={"retention": "candidate"})
            state = commit_checkpoint(state, event)
        case "missing":
            owner = state.attempts.attempts[0].model_copy(update={"checkpoints": ()})
            state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
        case "origin":
            invocation = state.sessions.invocations[0]
            assert invocation.observation is not None
            invocation = invocation.model_copy(
                update={
                    "observation": invocation.observation.model_copy(
                        update={"request_id": core.RequestId(root="wrong-origin")}
                    )
                }
            )
            state = state.model_copy(
                update={
                    "sessions": state.sessions.model_copy(update={"invocations": (invocation,)})
                }
            )
    result = core.step(state, event, reducers=REDUCERS)
    assert result.state.run.receipts[0].completion is None


@pytest.mark.parametrize(
    "status",
    [
        core.ObservationStatus.FAILED,
        core.ObservationStatus.REJECTED,
        core.ObservationStatus.CANCELLED,
    ],
)
def test_failed_yield_retention_keeps_decision_and_dependencies_pending(
    status: core.ObservationStatus,
) -> None:
    state, event, request = yielded_checkpoint()
    failure = core.WorkspaceObserved(
        attempt=request.attempt,
        observation=core.Observation(
            event_id=core.EventId(root="retention-failed"),
            request_id=event.request_id,
            scope=request.scope,
            admission_id=request.admission_id,
            sequence=1,
            observed_at=2.0,
            status=status,
            accepted=False,
            terminal=True,
        ),
    )
    failed = core.step(state, failure, reducers=REDUCERS)
    assert failed.state.attempts.attempts[0].phase == core.AttemptPhase.BLOCKED
    assert failed.state.run.receipts[0].completion is None
    dependent = request.model_copy(
        update={"decision_dependencies": (failed.state.run.receipts[0].decision_id,)}
    )
    assert core.dependency_status(failed.state, dependent) == core.DependencyStatus.PENDING
    result = core.step(failed.state, event, reducers=REDUCERS)
    assert result.state.run.receipts[0].completion is None
    assert result.state.attempts.attempts[0].checkpoints == ()


def test_yield_checkpoint_completes_original_turn_not_checkpoint_request_origin() -> None:
    state, event, checkpoint_request = yielded_checkpoint()
    other_id = core.DecisionId(root="checkpoint-decision")
    other = core.DecisionReceipt(
        decision_id=other_id,
        payload_digest="other-origin",
        feedback=core.Accepted(decision_id=other_id),
    )
    checkpoint_request = checkpoint_request.model_copy(update={"decision_id": other_id})
    records = tuple(
        row.model_copy(
            update={
                "request": checkpoint_request,
                "payload_digest": hashlib.sha256(
                    json.dumps(
                        checkpoint_request.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            }
        )
        if row.request_id == event.request_id
        else row
        for row in state.intents.intents
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"receipts": (*state.run.receipts, other)}),
            "intents": state.intents.model_copy(update={"intents": records}),
        }
    )
    state = commit_checkpoint(state, event)
    result = core.step(state, event, reducers=REDUCERS)
    assert result.state.run.receipts[0].completion == core.CompletionStatus.SUCCEEDED
    assert result.state.run.receipts[1].completion is None
