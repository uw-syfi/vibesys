"""One gate decides whether core takes decisions now, later, only to wind down, or never."""

from __future__ import annotations

import pytest

import vs_core.api as core

from .proof_digest import value_digest

_HELD = (core.RunStatus.PAUSED, core.RunStatus.BLOCKED)


def _with_status(status: core.RunStatus) -> core.CoreState:
    state = core.initial_state()
    return state.model_copy(update={"run": state.run.model_copy(update={"status": status})})


def _proposal(state: core.CoreState) -> core.ProposeWinner:
    return core.ProposeWinner(
        decision_id=core.DecisionId(root="propose"),
        scope=core.Scope(owner=state.run.run_id, generation=state.run.generation),
        selection=core.TrustedBaseline(revision=core.RevisionRef.of_git_commit("abc123")),
    )


@pytest.mark.parametrize(
    ("status", "gate"),
    [
        (core.RunStatus.RUNNING, core.DecisionGate.OPEN),
        (core.RunStatus.PAUSED, core.DecisionGate.HELD),
        (core.RunStatus.BLOCKED, core.DecisionGate.HELD),
        (core.RunStatus.CLOSING, core.DecisionGate.DRAINING),
        (core.RunStatus.TERMINAL, core.DecisionGate.CLOSED),
    ],
)
def test_the_gate_follows_the_run_status(status: core.RunStatus, gate: core.DecisionGate) -> None:
    assert core.decision_gate(_with_status(status)) is gate


def test_a_run_with_a_result_is_draining_whatever_its_status() -> None:
    state = _with_status(core.RunStatus.RUNNING)
    result = core.RunResultProposal(outcome="cancelled", reason="stop")
    state = state.model_copy(update={"run": state.run.model_copy(update={"result": result})})

    assert core.decision_gate(state) is core.DecisionGate.DRAINING


@pytest.mark.parametrize("status", _HELD)
def test_a_held_run_refuses_for_now_and_leaves_no_receipt(status: core.RunStatus) -> None:
    state = _with_status(status)
    decision = _proposal(state)

    transition = core.step(state, core.DecisionSubmitted(decision=decision, expected_revision=0))

    (rejection,) = transition.events
    assert isinstance(rejection, core.Rejected)
    assert rejection.code is core.RejectionCode.HELD
    assert core.rejection_outlook(rejection.code) is core.RejectionOutlook.NOT_NOW
    assert transition.state.run.receipts == ()


def test_the_same_decision_is_judged_again_once_the_run_resumes() -> None:
    held = _with_status(core.RunStatus.PAUSED)
    decision = _proposal(held)
    refused = core.step(held, core.DecisionSubmitted(decision=decision, expected_revision=0))
    running = refused.state.model_copy(
        update={"run": refused.state.run.model_copy(update={"status": core.RunStatus.RUNNING})}
    )

    again = core.step(running, core.DecisionSubmitted(decision=decision, expected_revision=0))

    assert core.decision_gate(running) is core.DecisionGate.OPEN
    # Judged on its merits now: whatever it gets, it is not the held refusal.
    assert not any(
        isinstance(event, core.Rejected) and event.code is core.RejectionCode.HELD
        for event in again.events
    )


@pytest.mark.parametrize("status", [core.RunStatus.CLOSING, core.RunStatus.TERMINAL])
def test_a_draining_or_closed_run_refuses_for_good_and_records_it(status: core.RunStatus) -> None:
    state = _with_status(status)

    transition = core.step(
        state, core.DecisionSubmitted(decision=_proposal(state), expected_revision=0)
    )

    (rejection,) = transition.events
    assert isinstance(rejection, core.Rejected)
    assert rejection.code is core.RejectionCode.CLOSED_SCOPE
    assert core.rejection_outlook(rejection.code) is core.RejectionOutlook.NEVER
    assert len(transition.state.run.receipts) == 1


def test_a_stale_view_refusal_recorded_by_an_older_journal_still_replays_as_a_no_op() -> None:
    """Before the held/stale split a stale-view refusal left a receipt; old journals hold them.

    Resubmitting that decision after a resume must stay idempotent (no second feedback, no
    state change), exactly as it did when the receipt was written. Only refusals made from
    now on leave no receipt.
    """
    state = _with_status(core.RunStatus.RUNNING)
    decision = _proposal(state)
    old_receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=None,
        payload_digest=value_digest(decision),
        feedback=core.Rejected(
            decision_id=decision.decision_id,
            code=core.RejectionCode.STALE_VIEW,
            path=("expected_revision",),
            detail="view revision changed",
        ),
    )
    journal = state.model_copy(
        update={"run": state.run.model_copy(update={"receipts": (old_receipt,)})}
    )
    reloaded = core.CoreState.model_validate_json(journal.model_dump_json())

    replayed = core.step(
        reloaded, core.DecisionSubmitted(decision=decision, expected_revision=reloaded.revision)
    )

    assert replayed.events == ()
    assert replayed.state.run.receipts == reloaded.run.receipts


def test_a_new_stale_view_refusal_leaves_no_receipt_so_the_decision_can_be_proposed_again() -> None:
    state = _with_status(core.RunStatus.RUNNING)
    decision = _proposal(state)

    stale = core.step(state, core.DecisionSubmitted(decision=decision, expected_revision=99))

    (rejection,) = stale.events
    assert isinstance(rejection, core.Rejected)
    assert rejection.code is core.RejectionCode.STALE_VIEW
    assert stale.state.run.receipts == ()
