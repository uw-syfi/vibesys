"""A job whose settlement core refuses ends the run with a typed failure instead of stranding."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from tests.support.strategy_capture import captured
from tests.vibesys.orchestration.issue_queue.strategy.world import (
    IssueQueueWorld,
    evaluated,
    implemented,
    judged,
)

from vibesys.orchestration.issue_queue.strategy.api import IssueQueueStrategyState
from vs_core.api import Rejected, RejectionCode

CAPTURE = captured(
    IssueQueueWorld(implementer=[implemented()], judge=[judged()], perf=[evaluated()]),
    IssueQueueStrategyState,
)


@given(code=st.sampled_from(RejectionCode))
def test_a_rejected_settlement_ends_the_job_instead_of_stranding_it(code: RejectionCode) -> None:
    """Core refusing a `Withdraw` sends no `AttemptSettled`; the job must still end."""
    outstanding = [
        (state, view)
        for state, view in CAPTURE.pairs
        if state.job is not None and state.job.settle_sent and state.job.awaiting is not None
    ]
    assert outstanding
    for state, view in outstanding:
        assert state.job is not None
        assert state.job.awaiting is not None
        rejected = Rejected(
            decision_id=state.job.awaiting, code=code, path=("target",), detail="refused"
        )
        strategy = CAPTURE.over(state)
        proposal = strategy.bind(strategy.on_event(view, rejected)).decide(view)
        assert proposal.state.job is None
        assert proposal.state.ending is not None
        assert proposal.state.ending.outcome == "failure"
