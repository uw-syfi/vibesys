"""A run finishes, typed, whichever way a round ends: a refused start or a stop at any request."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.multi.strategy.world import MultiWorld, run_to_end

RUNS = settings(max_examples=40, deadline=None, derandomize=True, database=None)


def test_a_round_core_refuses_to_start_does_not_stall_the_run() -> None:
    # Core grants one attempt; the second round's start comes back rejected.
    run_to_end(MultiWorld(max_rounds=2, attempt_cap=1))


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
    run_to_end(MultiWorld(max_rounds=rounds, attempt_cap=cap, stop_at=stop_at))
