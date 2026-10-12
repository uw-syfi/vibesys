"""A workstream whose settlement core refuses ends with a typed failure instead of stranding."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from tests.support.strategy_capture import captured
from tests.vibesys.orchestration.dynamic.strategy.world import DynamicWorld

from vibesys.orchestration.dynamic.strategy.api import DynamicStrategyState
from vs_core.api import Rejected, RejectionCode

CAPTURE = captured(DynamicWorld(), DynamicStrategyState)


@given(code=st.sampled_from(RejectionCode))
def test_a_rejected_settlement_ends_the_workstream_instead_of_stranding_it(
    code: RejectionCode,
) -> None:
    """Core refusing a `Withdraw` sends no `AttemptSettled`; the workstream must still end."""
    outstanding = [
        (state, view, record)
        for state, view in CAPTURE.pairs
        for record in state.attempts
        if record.settle_sent and record.awaiting is not None and record.phase.value != "done"
    ]
    assert outstanding
    for state, view, record in outstanding:
        assert record.awaiting is not None
        rejected = Rejected(
            decision_id=record.awaiting, code=code, path=("target",), detail="refused"
        )
        strategy = CAPTURE.over(state)
        proposal = strategy.bind(strategy.on_event(view, rejected)).decide(view)
        ended = next(item for item in proposal.state.attempts if item.attempt == record.attempt)
        assert ended.phase.value == "done"
        assert ended.failure is not None
