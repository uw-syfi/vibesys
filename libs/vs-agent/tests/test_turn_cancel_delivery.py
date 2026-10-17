"""A cancelled turn is interrupted once: by the cancel itself, or by its first event.

``LaunchedSession.cancel`` interrupts the library's running turn; a turn whose caller
cancelled it before the library ran it is stopped at its first event instead. Before, the
cancel did not record that it had reached the running turn, so the turn's next event
interrupted it again.
"""

from __future__ import annotations

import agentshim
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import CapacityStops

# test-isolation: cancel delivery is state shared by a launched session and its event handler
from vs_agent.shim_turns import SteerLedger, TurnEvents
from vs_sim.api.testing import SimThreads


@given(
    before=st.integers(min_value=0, max_value=5),
    after=st.integers(min_value=0, max_value=5),
)
def test_a_cancelled_turn_is_interrupted_once(before: int, after: int) -> None:
    """``before`` events reach the turn, then the caller cancels it, then ``after`` more."""
    threads = SimThreads()
    events = TurnEvents(SteerLedger(threads))
    turn = CapacityStops(threads)
    interrupts: list[str] = []
    running = False

    def interrupt() -> None:
        # The library's interrupt reaches only a turn it is running.
        if running:
            interrupts.append("interrupt")

    events.begin_turn(structured=False, stop=interrupt, turn=turn)
    for _ in range(before):
        running = True
        events.on_event(agentshim.AssistantText(text="working"))
    turn.cancel()
    events.cancel(interrupt)
    for _ in range(after):
        running = True
        events.on_event(agentshim.AssistantText(text="working"))

    assert events.cancelled
    reached = before > 0 or after > 0
    assert interrupts == (["interrupt"] if reached else [])
