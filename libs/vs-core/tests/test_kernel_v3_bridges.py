"""Kernel notifications and canonical authority independent of leaf policy."""

import hashlib
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core


def accepted(decision: core.Decision) -> core.DecisionReceipt:
    return core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=hashlib.sha256(
            json.dumps(
                decision.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
        feedback=core.Accepted(decision_id=decision.decision_id),
    )


def prerequisite(
    state: core.CoreState, name: str, depends_on: tuple[core.DecisionId, ...] = ()
) -> core.StartAttempt:
    return core.StartAttempt(
        decision_id=core.DecisionId(root=name),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        attempt_id=core.AttemptId(root=name),
        item_id=core.ItemId(root=name),
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
        depends_on=depends_on,
    )


@pytest.mark.parametrize("status", tuple(core.CompletionStatus))
@given(chained=st.booleans())
def test_request_free_settlement_wakes_for_direct_and_transitive_completion(
    status: core.CompletionStatus, *, chained: bool
) -> None:
    state = core.initial_state()
    first = prerequisite(state, "first")
    intermediate = prerequisite(state, "intermediate", (first.decision_id,))
    trigger = intermediate if chained else first
    disposition = core.Settle(assessments=(), eligible=False, retention="discard", outcome="failed")
    decision = core.Withdraw(
        decision_id=core.DecisionId(root="settle"),
        scope=first.scope,
        target=core.AttemptRef(attempt_id=first.attempt_id, generation=0),
        disposition=disposition,
        depends_on=(trigger.decision_id,),
    )
    assert isinstance(decision.target, core.AttemptRef)
    pending = core.Settlement(
        settlement_id=core.SettlementId(root="settlement"),
        attempt=decision.target,
        candidate=None,
        assessments=(),
        eligible=False,
        retention="discard",
        outcome="failed",
    )
    receipts = tuple(
        accepted(row) for row in ((first, intermediate, decision) if chained else (first, decision))
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"receipts": receipts}),
            "settlement": core.SettlementState(pending=(pending,)),
        }
    )
    ingress = core.AttemptEvaluationHistoryUpdated(
        attempt=core.AttemptRef(attempt_id=first.attempt_id, generation=0),
        history=core.AttemptEvaluationHistory(),
    )
    completion = core.DecisionCompleted(decision_id=first.decision_id, status=status)
    frames = [
        core.TraceFrame(
            signal=ingress, change=core.AttemptsChange(state=state.attempts, signals=(completion,))
        )
    ]
    if not chained or status != core.CompletionStatus.SUCCEEDED:
        wake = core.SettlementDependencyResolved(
            decision_id=trigger.decision_id,
            status=status if not chained else core.CompletionStatus.FAILED,
        )
        frames.append(
            core.TraceFrame(signal=wake, change=core.SettlementChange(state=state.settlement))
        )
    result = core.trace_step(state, ingress, core.ReducerTrace(frames=tuple(frames)))
    assert result.requests == ()
    assert result.state.run.receipts[0].completion == status
    assert result.state.settlement == state.settlement
    assert core.CoreState.model_validate_json(result.state.model_dump_json()) == result.state


@given(now=st.integers(min_value=0, max_value=10000))
def test_stop_control_records_canonical_result_before_run_drain_and_replays(now: int) -> None:
    state = core.initial_state()
    proposal = core.RunResultProposal(outcome="cancelled", reason="explicit stop")
    control = core.ControlInput(control_id=core.ControlId(root="stop"), action="stop")
    event = core.RunControlEvent(control=control, now_at=float(now), result=proposal)
    drain = core.AdmissionControl(action="drain")
    result = core.trace_step(
        state,
        event,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=drain,
                    change=core.SchedulingChange(
                        state=state.scheduling, signals=(core.RunDrained(),)
                    ),
                ),
            )
        ),
    )
    assert result.state.run.status == core.RunStatus.TERMINAL
    assert result.state.run.result == proposal
    receipt = result.state.run.receipts[0]
    assert isinstance(receipt.decision, core.Stop)
    assert isinstance(receipt.feedback, core.Accepted)
    assert receipt.decision_id == core.DecisionId(root="control:stop")
    assert receipt.decision.result == proposal
    assert result.state.run.controls == (control,)
    ended = [row for row in result.events if isinstance(row, core.RunEnded)]
    assert len(ended) == 1
    assert ended[0].result == proposal
    assert result.events[-1] == ended[0]
    restored = core.CoreState.model_validate_json(result.state.model_dump_json())
    replay = core.step(restored, event)
    assert replay.requests == replay.events == ()
    conflicting = event.model_copy(
        update={"result": core.RunResultProposal(outcome="failure", reason="changed")}
    )
    with pytest.raises(core.ContractError, match="result conflict"):
        core.step(restored, conflicting)


@given(identity=st.text(alphabet="abcdef0123456789", min_size=1, max_size=20))
def test_queued_reopen_retirement_uses_queue_admission_over_old_parked_owner(identity: str) -> None:
    state = core.initial_state()
    attempt = core.AttemptRef(attempt_id=core.AttemptId(root="attempt"), generation=0)
    queued_id = core.DecisionId(root=f"queued:{identity}")
    owner = core.AttemptView(
        attempt_id=attempt.attempt_id,
        item_id=core.ItemId(root="item"),
        generation=0,
        phase=core.AttemptPhase.PARKED,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
        admission_id=core.DecisionId(root="old-admission"),
    )
    queue = core.AttemptReopenRequest(
        decision_id=queued_id, request_id=core.RequestId(root="reopen"), attempt=attempt
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "scheduling": core.SchedulingState(queue=(queue,)),
        }
    )
    decision = core.Withdraw(
        decision_id=core.DecisionId(root="withdraw"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        target=attempt,
        disposition=core.Cancel(),
    )
    signal = core.RetireRequested(
        attempt=attempt,
        disposition="cancel",
        requested_at=state.run.now_at,
        authority=core.RequestId(root="withdraw:withdraw"),
        admission_id=queued_id,
    )
    result = core.trace_step(
        state,
        core.DecisionSubmitted(decision=decision, expected_revision=0),
        core.ReducerTrace(
            frames=(
                core.TraceFrame(signal=signal, change=core.AttemptsChange(state=state.attempts)),
            )
        ),
    )
    assert isinstance(result.events[0], core.Accepted)
    assert result.state.attempts.attempts[0].admission_id == owner.admission_id
    assert result.requests == ()
