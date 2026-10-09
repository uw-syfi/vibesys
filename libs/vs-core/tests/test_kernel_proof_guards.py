"""Canonical proof guards through the published kernel and trace seam."""

import hashlib
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core


def receipt(decision: core.Decision) -> core.DecisionReceipt:
    """Persist a canonical decision as ingress would, independent of mutations."""
    payload = json.dumps(
        decision.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=hashlib.sha256(payload.encode()).hexdigest(),
        feedback=core.Accepted(decision_id=decision.decision_id),
    )


def stop(
    state: core.CoreState, name: str, depends_on: tuple[core.DecisionId, ...] = ()
) -> core.Stop:
    """Canonical run-scoped control with an explicit result."""
    return core.Stop(
        decision_id=core.DecisionId(root=name),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        mode="drain",
        result=core.RunResultProposal(outcome="cancelled", reason="proof guard"),
        depends_on=depends_on,
    )


@pytest.mark.parametrize("defect", ["absent", "feedback", "decision", "duplicate", "digest"])
@given(identity=st.text(alphabet="abcdef0123456789", min_size=1, max_size=20))
def test_dependency_dispatch_requires_exact_owner_receipt(defect: str, identity: str) -> None:
    state = core.initial_state()
    command = stop(state, identity)
    accepted = receipt(command)
    rows = (accepted,)
    if defect == "absent":
        rows = ()
    elif defect == "duplicate":
        rows = (accepted, accepted)
    elif defect == "feedback":
        rows = (
            accepted.model_copy(
                update={"feedback": core.Accepted(decision_id=core.DecisionId(root="foreign"))}
            ),
        )
    elif defect == "decision":
        rows = (
            accepted.model_copy(
                update={
                    "decision": command.model_copy(
                        update={"decision_id": core.DecisionId(root="foreign")}
                    )
                }
            ),
        )
    else:
        rows = (accepted.model_copy(update={"payload_digest": "foreign"}),)
    state = state.model_copy(update={"run": state.run.model_copy(update={"receipts": rows})})
    request = core.InspectRequest(
        request_id=core.RequestId(root="query"),
        scope=command.scope,
        target=core.RequestId(root="target"),
        decision_id=command.decision_id,
        deadline_at=10.0,
    )
    assert core.dependency_status(state, request) == core.DependencyStatus.FAILED
    assert request.request_id is not None
    canonical = json.dumps(
        request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    intent = core.Intent(
        request_id=request.request_id,
        request=request,
        payload_digest=hashlib.sha256(canonical.encode()).hexdigest(),
        lifecycle=core.LifecycleClass.QUERY,
        phase=core.IntentPhase.PREPARED,
        reconcile_deadline_at=20.0,
    )
    ready = state.intents.model_copy(
        update={
            "intents": (intent,),
            "recovery": state.intents.recovery.model_copy(
                update={"phase": core.RecoveryPhase.READY}
            ),
        }
    )
    with pytest.raises(core.ContractError, match="dependency"):
        core.step(
            state.model_copy(update={"intents": ready}),
            core.DispatchAuthorized(request_id=request.request_id),
        )
    assert (
        core.dependency_status(state, request.model_copy(update={"decision_id": None}))
        == core.DependencyStatus.SUCCEEDED
    )


def test_completed_dependency_with_foreign_feedback_cannot_wake_dispatch() -> None:
    state = core.initial_state()
    command = stop(state, "prerequisite")
    accepted = receipt(command).model_copy(
        update={
            "feedback": core.Accepted(decision_id=core.DecisionId(root="foreign")),
            "completion": core.CompletionStatus.SUCCEEDED,
        }
    )
    state = state.model_copy(update={"run": state.run.model_copy(update={"receipts": (accepted,)})})
    request = core.InspectRequest(
        scope=command.scope,
        target=core.RequestId(root="target"),
        deadline_at=10.0,
        decision_dependencies=(command.decision_id,),
    )
    assert core.dependency_status(state, request) == core.DependencyStatus.FAILED
    decision = stop(state, "dependent", (command.decision_id,))
    result = core.step(state, core.DecisionSubmitted(decision=decision, expected_revision=0))
    assert isinstance(result.events[0], core.Rejected)
    assert result.events[0].code == core.RejectionCode.DEPENDENCY


def test_completion_rejects_mismatched_feedback_before_publishing_wake() -> None:
    state = core.initial_state()
    command = stop(state, "completion")
    accepted = receipt(command).model_copy(
        update={
            "feedback": core.Accepted(decision_id=core.DecisionId(root="foreign")),
        }
    )
    state = state.model_copy(update={"run": state.run.model_copy(update={"receipts": (accepted,)})})
    ingress = core.AttemptEvaluationHistoryUpdated(
        attempt=core.AttemptRef(attempt_id=core.AttemptId(root="attempt"), generation=0),
        history=core.AttemptEvaluationHistory(),
    )
    trace = core.ReducerTrace(
        frames=(
            core.TraceFrame(
                signal=ingress,
                change=core.AttemptsChange(
                    state=state.attempts,
                    signals=(
                        core.DecisionCompleted(
                            decision_id=command.decision_id, status=core.CompletionStatus.SUCCEEDED
                        ),
                    ),
                ),
            ),
        )
    )
    with pytest.raises(core.ContractError, match="accepted decision"):
        core.trace_step(state, ingress, trace)
    assert state.run.receipts[0].completion is None


@pytest.mark.parametrize("later_accepted", [False, True])
def test_run_finality_keeps_first_accepted_stop_dependencies(*, later_accepted: bool) -> None:
    state = core.initial_state()
    prerequisite = core.StartAttempt(
        decision_id=core.DecisionId(root="prerequisite"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        attempt_id=core.AttemptId(root="attempt"),
        item_id=core.ItemId(root="item"),
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
    )
    first = stop(state, "first", (prerequisite.decision_id,))
    later = stop(state, "later")
    later_row = receipt(later)
    if not later_accepted:
        later_row = later_row.model_copy(
            update={
                "feedback": core.Rejected(
                    decision_id=later.decision_id,
                    code=core.RejectionCode.CLOSED_SCOPE,
                    path=("scope",),
                    detail="rejected later Stop",
                ),
            }
        )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": core.RunStatus.CLOSING,
                    "result": first.result,
                    "receipts": (receipt(prerequisite), receipt(first), later_row),
                }
            )
        }
    )
    clock = core.ClockAdvanced(now_at=1.0)
    trace = core.ReducerTrace(
        frames=(
            core.TraceFrame(
                signal=clock,
                change=core.SchedulingChange(state=state.scheduling, signals=(core.RunDrained(),)),
            ),
        )
    )
    result = core.trace_step(state, clock, trace)
    assert result.state.run.status == core.RunStatus.CLOSING
    assert not any(isinstance(event, core.RunEnded) for event in result.events)
    completed = state.run.receipts[0].model_copy(
        update={"completion": core.CompletionStatus.SUCCEEDED}
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "receipts": (completed, *state.run.receipts[1:]),
                }
            )
        }
    )
    result = core.trace_step(state, clock, trace)
    assert result.state.run.status == core.RunStatus.TERMINAL
    assert isinstance(result.events[-1], core.RunEnded)


@pytest.mark.parametrize("status", list(core.CompletionStatus))
def test_dependency_completion_never_wakes_a_foreign_feedback_command(
    status: core.CompletionStatus,
) -> None:
    state = core.initial_state()
    command = stop(state, "prerequisite")
    dependent = core.Withdraw(
        decision_id=core.DecisionId(root="dependent"),
        scope=command.scope,
        target=core.AttemptRef(attempt_id=core.AttemptId(root="attempt"), generation=0),
        disposition=core.Settle(
            assessments=(), eligible=False, retention="discard", outcome="failed"
        ),
        depends_on=(command.decision_id,),
    )
    foreign = receipt(dependent).model_copy(
        update={
            "feedback": core.Accepted(decision_id=core.DecisionId(root="foreign")),
        }
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "receipts": (receipt(command), foreign),
                }
            )
        }
    )
    assert isinstance(dependent.target, core.AttemptRef)
    ingress = core.AttemptEvaluationHistoryUpdated(
        attempt=dependent.target,
        history=core.AttemptEvaluationHistory(),
    )
    trace = core.ReducerTrace(
        frames=(
            core.TraceFrame(
                signal=ingress,
                change=core.AttemptsChange(
                    state=state.attempts,
                    signals=(
                        core.DecisionCompleted(decision_id=command.decision_id, status=status),
                    ),
                ),
            ),
        )
    )
    result = core.trace_step(state, ingress, trace)
    assert result.state.run.receipts[1] == foreign
    assert result.state.settlement == state.settlement


@pytest.mark.parametrize("episode", [None, core.DecisionId(root="foreign")])
def test_kernel_event_cause_requires_the_recorded_observation_episode(
    episode: core.DecisionId | None,
) -> None:
    state = core.initial_state()
    scope = core.Scope(owner=core.AttemptId(root="attempt"), generation=0)
    spec = core.SessionSpec(
        session_id=core.SessionId(root="session"),
        role_id=core.RoleId(root="worker"),
        policy="fresh",
        lifetime="ephemeral",
        access=core.Access.READ_ONLY,
    )
    request = core.EnsureSession(
        request_id=core.RequestId(root="request"),
        scope=scope,
        admission_id=core.DecisionId(root="canonical"),
        deadline_at=10.0,
        spec=spec,
    )
    assert request.request_id is not None
    serialized = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    intent = core.Intent(
        request_id=request.request_id,
        request=request,
        payload_digest=hashlib.sha256(serialized.encode()).hexdigest(),
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        phase=core.IntentPhase.DISPATCHED,
        reconcile_deadline_at=10.0,
    )
    state = state.model_copy(update={"intents": core.IntentsState(intents=(intent,))})
    event = core.SessionObserved(
        session_id=spec.session_id,
        observation=core.Observation(
            event_id=core.EventId(root="observation"),
            request_id=request.request_id,
            scope=scope,
            admission_id=episode,
            sequence=1,
            observed_at=1.0,
            status=core.ObservationStatus.UNKNOWN,
        ),
    )
    trace = core.ReducerTrace(
        frames=(core.TraceFrame(signal=event, change=core.SessionsChange(state=state.sessions)),)
    )
    with pytest.raises(core.ContractError, match="episode"):
        core.trace_step(state, event, trace)
