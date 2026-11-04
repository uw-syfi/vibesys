"""Canonical proof guards through the published kernel and trace seam."""

import hashlib
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core


def receipt(decision: core.Command) -> core.DecisionReceipt:
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


def stop(state: core.CoreState, name: str) -> core.Finish:
    """Canonical run-scoped control with an explicit result."""
    return core.Finish(
        decision_id=core.DecisionId(root=name),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        mode="drain",
        result=core.RunResultProposal(outcome="cancelled", reason="proof guard"),
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
    )
    assert request.request_id is not None
    canonical = json.dumps(
        request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    record = core.RequestRecord(
        request_id=request.request_id,
        request=request,
        payload_digest=hashlib.sha256(canonical.encode()).hexdigest(),
        lifecycle=core.LifecycleClass.QUERY,
        phase=core.RequestPhase.PREPARED,
        until=core.Until(at=20.0),
        addressed_by=(),
    )
    ready = state.records.model_copy(
        update={
            "records": (record,),
        }
    )
    with pytest.raises(core.ContractError, match="dependency"):
        core.step(
            state.model_copy(update={"records": ready}),
            core.DispatchAuthorized(request_id=request.request_id),
        )


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
        spec=spec,
    )
    assert request.request_id is not None
    serialized = json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    record = core.RequestRecord(
        request_id=request.request_id,
        request=request,
        payload_digest=hashlib.sha256(serialized.encode()).hexdigest(),
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        phase=core.RequestPhase.DISPATCHED,
        until=core.Until(at=10.0),
        addressed_by=(),
    )
    state = state.model_copy(update={"records": core.RecordsState(records=(record,))})
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
