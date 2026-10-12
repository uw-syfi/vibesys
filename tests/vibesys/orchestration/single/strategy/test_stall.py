"""A run finishes, typed, whichever way a round ends: a refused start or a stop at any request."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.strategy_capture import captured
from tests.vibesys.orchestration.single.strategy.world import SingleWorld, run_to_end

from vibesys.orchestration.single.strategy.api import SingleStrategyState
from vs_core.api import Rejected, RejectionCode

RUNS = settings(max_examples=40, deadline=None, derandomize=True, database=None)


def test_a_round_core_refuses_to_start_does_not_stall_the_run() -> None:
    # Core grants one attempt; the second round's start comes back rejected.
    run_to_end(SingleWorld(max_rounds=2, attempt_cap=1))


@RUNS
@given(
    rounds=st.integers(1, 3),
    cap=st.none() | st.integers(1, 4),
    stop_at=st.none() | st.integers(0, 60),
)
def test_every_way_a_round_ends_leads_to_a_typed_terminal_state(
    rounds: int, cap: int | None, stop_at: int | None
) -> None:
    """Starts core refuses and a stop at any request never leave the run waiting on a round."""
    run_to_end(SingleWorld(max_rounds=rounds, attempt_cap=cap, stop_at=stop_at))


CAPTURE = captured(SingleWorld(max_rounds=1), SingleStrategyState)


@given(code=st.sampled_from(RejectionCode))
def test_a_rejected_settlement_ends_the_round_instead_of_stranding_it(code: RejectionCode) -> None:
    """Core refusing a `Withdraw` sends no `AttemptSettled`; the round must still end."""
    outstanding = [
        (state, view)
        for state, view in CAPTURE.pairs
        if state.round is not None and state.round.settle_sent and state.round.awaiting is not None
    ]
    assert outstanding
    for state, view in outstanding:
        assert state.round is not None
        assert state.round.awaiting is not None
        rejected = Rejected(
            decision_id=state.round.awaiting, code=code, path=("target",), detail="refused"
        )
        strategy = CAPTURE.over(state)
        proposal = strategy.bind(strategy.on_event(view, rejected)).decide(view)
        assert proposal.state.round is None
        assert proposal.state.closed == state.closed + 1
