"""Committed Stop proof properties through the public, reloadable kernel."""

from __future__ import annotations

from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core


def _occupied_state(held: int, queued: int) -> core.CoreState:
    state = core.initial_state()
    requests = tuple(
        core.AttemptRequest(
            decision_id=core.DecisionId(root=f"start-{index}"),
            attempt_id=core.AttemptId(root=f"attempt-{index}"),
            item_id=core.ItemId(root=f"item-{index}"),
            generation=0,
            admission_charge=1,
        )
        for index in range(held + queued)
    )
    owners = tuple(
        core.AttemptView(
            attempt_id=request.attempt_id,
            item_id=request.item_id,
            generation=0,
            phase=core.AttemptPhase.ACTIVE if index < held else core.AttemptPhase.QUEUED,
            workspace=core.WorkspacePlan(
                mode=core.WorkspaceMode.ISOLATED_CHILD, base=state.run.facts.baseline
            ),
            budget=core.AttemptBudget(admission_charge=1),
            admission_id=request.decision_id if index < held else None,
            charges=(
                core.ChargeReceipt(
                    charge_id=core.ChargeId(root=f"charge-{index}"),
                    kind=core.ChargeKind.ADMISSION,
                    charged=1,
                ),
            ),
        )
        for index, request in enumerate(requests)
    )
    receipts: list[core.DecisionReceipt] = []
    for owner, request in zip(owners, requests, strict=True):
        decision = core.StartAttempt(
            decision_id=request.decision_id,
            scope=core.Scope(owner=state.run.run_id, generation=0),
            attempt_id=request.attempt_id,
            item_id=request.item_id,
            workspace=owner.workspace,
            budget=owner.budget,
        )
        receipts.append(
            core.DecisionReceipt(
                decision_id=decision.decision_id,
                decision=decision,
                payload_digest=f"canonical-{request.decision_id.root}",
                feedback=core.Accepted(decision_id=decision.decision_id),
            )
        )
    return state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "now_at": 5.0,
                    "deadline_at": 100.0,
                    "limits": core.Limits(max_attempts=20, max_parallel=max(held, 1)),
                    "receipts": tuple(receipts),
                }
            ),
            "attempts": core.AttemptsState(attempts=owners),
            "scheduling": core.SchedulingState(
                admission_closed=True,
                queue=requests[held:],
                slots=tuple(
                    core.Slot(
                        attempt=core.AttemptRef(
                            attempt_id=request.attempt_id, generation=request.generation
                        ),
                        admission_id=request.decision_id,
                        admitted_at=0.0,
                    )
                    for request in requests[:held]
                ),
            ),
        }
    )


def _stop(state: core.CoreState, mode: Literal["drain", "cancel"], identity: str) -> core.Stop:
    return core.Stop(
        decision_id=core.DecisionId(root=identity),
        scope=core.Scope(owner=state.run.run_id, generation=state.run.generation),
        mode=mode,
        result=core.RunResultProposal(outcome="cancelled", reason="committed result"),
    )


def _receipt(stop: core.Stop, proof: str = "accepted") -> core.DecisionReceipt:
    decision = stop
    receipt_id = stop.decision_id
    feedback_id = stop.decision_id
    if proof == "receipt-id":
        receipt_id = core.DecisionId(root="unrelated-receipt")
    elif proof == "feedback-id":
        feedback_id = core.DecisionId(root="unrelated-feedback")
    elif proof == "owner":
        decision = stop.model_copy(
            update={"scope": core.Scope(owner=core.RunId(root="another-run"), generation=0)}
        )
    elif proof == "generation":
        decision = stop.model_copy(
            update={"scope": stop.scope.model_copy(update={"generation": 1})}
        )
    feedback: core.DecisionFeedback = core.Accepted(decision_id=feedback_id)
    if proof == "rejected":
        feedback = core.Rejected(
            decision_id=feedback_id,
            code=core.RejectionCode.CLOSED_SCOPE,
            path=("run",),
            detail="Stop did not commit",
        )
    return core.DecisionReceipt(
        decision_id=receipt_id,
        decision=None if proof == "missing-decision" else decision,
        payload_digest=f"stop-{proof}",
        feedback=feedback,
    )


def _closing(
    state: core.CoreState,
    stop: core.Stop,
    receipts: tuple[core.DecisionReceipt, ...],
    status: core.RunStatus = core.RunStatus.CLOSING,
) -> core.CoreState:
    return state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": status,
                    "result": stop.result,
                    "receipts": (*state.run.receipts, *receipts),
                }
            )
        }
    )


def _step(state: core.CoreState, event: core.CoreEvent) -> core.Transition:
    before = state.model_dump_json()
    restored = core.CoreState.model_validate_json(before)
    transition = core.step(state, event)
    assert transition == core.step(restored, event)
    assert state.model_dump_json() == before
    assert (
        core.CoreState.model_validate_json(transition.state.model_dump_json()) == transition.state
    )
    return transition


def _assert_attempts_boundary(state: core.CoreState, event: core.CoreEvent, kind: str) -> None:
    """Valid scheduling proof reaches the public downstream Attempts boundary."""
    before = state.model_dump_json()
    failure: core.KernelNotImplementedError | None = None
    transition: core.Transition | None = None
    try:
        transition = _step(state, event)
    except core.KernelNotImplementedError as error:
        failure = error
    if failure is not None:
        assert failure.area == core.Area.ATTEMPTS
        assert failure.event_kind == kind
        restored = core.CoreState.model_validate_json(before)
        with pytest.raises(core.KernelNotImplementedError) as repeated:
            core.step(restored, event)
        assert repeated.value.area == failure.area
        assert repeated.value.event_kind == failure.event_kind
    else:
        assert transition is not None
        assert transition.state.scheduling.admission_closed
        assert not any(isinstance(item, core.RunEnded) for item in transition.events)
        if kind == "attempt_admitted":
            assert len(transition.state.scheduling.slots) > len(state.scheduling.slots)
            assert len(transition.state.scheduling.queue) < len(state.scheduling.queue)
        else:
            assert all(
                owner.closure is not None and owner.closure.disposition == "cancel"
                for owner in transition.state.attempts.attempts
            )
    assert state.model_dump_json() == before


@pytest.mark.parametrize(
    "proof",
    ["rejected", "receipt-id", "feedback-id", "owner", "generation", "missing-decision", "absent"],
)
@given(held=st.integers(1, 3), queued=st.integers(1, 4))
def test_cancel_never_retires_episodes_without_committed_stop_proof(
    proof: str, held: int, queued: int
) -> None:
    state = _occupied_state(held, queued)
    stop = _stop(state, "cancel", "cancel")
    state = _closing(state, stop, () if proof == "absent" else (_receipt(stop, proof),))
    transition = _step(state, core.AdmissionControl(action="cancel"))
    assert transition.requests == transition.events == ()
    assert transition.state.scheduling == state.scheduling
    assert transition.state.attempts == state.attempts
    assert transition.state.run.receipts == state.run.receipts


@pytest.mark.parametrize(
    "status", [status for status in core.RunStatus if status != core.RunStatus.CLOSING]
)
@given(held=st.integers(1, 3), queued=st.integers(1, 4))
def test_cancel_authority_cannot_retire_outside_closing_run(
    status: core.RunStatus, held: int, queued: int
) -> None:
    state = _occupied_state(held, queued)
    stop = _stop(state, "cancel", "cancel")
    state = _closing(state, stop, (_receipt(stop),), status)
    transition = _step(state, core.AdmissionControl(action="cancel"))
    assert transition.requests == transition.events == ()
    assert transition.state.scheduling == state.scheduling
    assert transition.state.attempts == state.attempts


@pytest.mark.parametrize(
    "proof",
    ["rejected", "receipt-id", "feedback-id", "owner", "generation", "missing-decision", "absent"],
)
@given(queued=st.integers(1, 4))
def test_drain_cannot_allocate_from_uncommitted_stop_proof(proof: str, queued: int) -> None:
    state = _occupied_state(0, queued)
    stop = _stop(state, "drain", "drain")
    state = _closing(state, stop, () if proof == "absent" else (_receipt(stop, proof),))
    transition = _step(state, core.ClockAdvanced(now_at=6.0))
    assert transition.requests == transition.events == ()
    assert transition.state.scheduling == state.scheduling
    assert transition.state.attempts == state.attempts
    assert transition.state.run.receipts == state.run.receipts


@pytest.mark.parametrize("first_mode", ["drain", "cancel"])
@given(queued=st.integers(1, 4))
def test_first_committed_stop_owns_drain_execution(
    first_mode: Literal["drain", "cancel"], queued: int
) -> None:
    state = _occupied_state(0, queued)
    first = _stop(state, first_mode, "first")
    later = _stop(state, "cancel" if first_mode == "drain" else "drain", "later")
    state = _closing(state, first, (_receipt(first), _receipt(later)))
    event = core.ClockAdvanced(now_at=6.0)
    if first_mode == "drain":
        _assert_attempts_boundary(state, event, "attempt_admitted")
    else:
        transition = _step(state, event)
        assert transition.requests == transition.events == ()
        assert transition.state.scheduling == state.scheduling


@pytest.mark.parametrize("proof", ["rejected", "receipt-id", "feedback-id", "owner", "generation"])
@given(held=st.integers(1, 3), queued=st.integers(1, 4))
def test_invalid_stop_payloads_cannot_block_first_valid_cancel(
    proof: str, held: int, queued: int
) -> None:
    state = _occupied_state(held, queued)
    invalid = _stop(state, "drain", "invalid")
    first = _stop(state, "cancel", "first-valid")
    state = _closing(state, first, (_receipt(invalid, proof), _receipt(first)))
    _assert_attempts_boundary(state, core.AdmissionControl(action="cancel"), "retire_requested")


@pytest.mark.parametrize(
    "proof",
    ["rejected", "receipt-id", "feedback-id", "owner", "generation", "missing-decision", "absent"],
)
@given(
    status=st.sampled_from(tuple(core.RunStatus)),
    increments=st.lists(st.integers(0, 10), min_size=1, max_size=10),
)
def test_empty_occupancy_never_ends_run_without_committed_stop_proof(
    proof: str, status: core.RunStatus, increments: list[int]
) -> None:
    state = _occupied_state(0, 0)
    stop = _stop(state, "drain", "unproved-drain")
    state = _closing(state, stop, () if proof == "absent" else (_receipt(stop, proof),), status)
    for increment in increments:
        for event in (
            core.ClockAdvanced(now_at=state.run.now_at + increment),
            core.AdmissionControl(action="drain"),
        ):
            transition = _step(state, event)
            assert transition.requests == transition.events == ()
            assert transition.state.run.status == status
            assert transition.state.run.receipts == state.run.receipts
            state = transition.state


@pytest.mark.parametrize(
    "status", [status for status in core.RunStatus if status != core.RunStatus.CLOSING]
)
@given(mode=st.sampled_from(["drain", "cancel"]), at=st.integers(5, 100))
def test_empty_occupancy_cannot_end_run_outside_closing_status(
    status: core.RunStatus, mode: Literal["drain", "cancel"], at: int
) -> None:
    state = _occupied_state(0, 0)
    stop = _stop(state, mode, "committed-stop")
    state = _closing(state, stop, (_receipt(stop),), status)
    for event in (core.ClockAdvanced(now_at=float(at)), core.AdmissionControl(action="drain")):
        transition = _step(state, event)
        assert transition.requests == transition.events == ()
        assert transition.state.run.status == status
        state = transition.state


@pytest.mark.parametrize(
    "status", [status for status in core.RunStatus if status != core.RunStatus.PAUSED]
)
@given(held=st.integers(0, 3), queued=st.integers(1, 4))
def test_pause_control_requires_committed_paused_run_status(
    status: core.RunStatus, held: int, queued: int
) -> None:
    state = _occupied_state(held, queued)
    state = state.model_copy(update={"run": state.run.model_copy(update={"status": status})})
    before = state.model_dump_json()
    with pytest.raises(core.ContractValidationError, match=r"admission_control.*run.status"):
        core.step(core.CoreState.model_validate_json(before), core.AdmissionControl(action="pause"))
    assert state.model_dump_json() == before


@given(queued=st.integers(1, 4), at=st.integers(5, 99))
def test_committed_pause_inhibits_execution_after_intake_closure(queued: int, at: int) -> None:
    state = _occupied_state(0, queued)
    state = state.model_copy(
        update={"run": state.run.model_copy(update={"status": core.RunStatus.PAUSED})}
    )
    for event in (core.AdmissionControl(action="pause"), core.ClockAdvanced(now_at=float(at))):
        transition = _step(state, event)
        assert transition.requests == transition.events == ()
        assert transition.state.scheduling == state.scheduling
        state = transition.state
