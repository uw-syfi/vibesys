"""A cancelled turn is interrupted once: by the cancel itself, or by its first event.

``LaunchedSession.cancel`` interrupts the library's running turn; a turn whose caller
cancelled it before the library ran it is stopped at its first event instead. Before, the
cancel did not record that it had reached the running turn, so the turn's next event
interrupted it again.
"""

from __future__ import annotations

import agentshim
from hypothesis import example, given
from hypothesis import strategies as st

from vs_agent.api import CapacityStops

# test-isolation: cancel delivery is state shared by a launched session and its event handler
from vs_agent.shim_turns import SteerLedger, TurnEvents
from vs_sim.api.testing import HANG_GUARD_S, SimThreads


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


@given(
    gaps=st.lists(st.integers(min_value=0, max_value=3), min_size=1, max_size=5),
    cancel_at=st.integers(min_value=0, max_value=12),
    seed=st.integers(0, 2**16),
)
# A schedule that lets the library's first event see the cancel's mark before the cancel
# checks whether the turn runs: before, both stopped the turn.
@example(gaps=[0], cancel_at=0, seed=474)
def test_a_cancel_racing_the_turns_events_stops_the_running_turn_once(
    gaps: list[int], cancel_at: int, seed: int
) -> None:
    """A cancel on one thread while the library delivers the turn's events on another.

    The caller cancels as ``LaunchedSession.cancel`` does: ``events.cancel(interrupt,
    turn)``. The turn record has a cancel listener that takes a lock, as the capacity
    gate's subscription does, so the cancel can be preempted after it marks the turn. The
    library's interrupt reaches the turn once the library has delivered an event. Whatever
    interleaving the seed picks, the running turn is stopped exactly once: by the cancel's
    interrupt, or by the event that saw the mark first. Before, an event between the mark
    and the cancel's check stopped the turn and the cancel interrupted it again.
    """
    threads = SimThreads(schedule_seed=seed)
    events = TurnEvents(SteerLedger(threads))
    turn = CapacityStops(threads)
    listener_lock = threads.lock()

    def listener() -> None:
        with listener_lock:
            pass

    turn.on_cancel(listener)
    reached: list[str] = []
    library_running = False

    def interrupt() -> None:
        if library_running:
            reached.append("interrupt")

    events.begin_turn(structured=False, stop=lambda: reached.append("stop"), turn=turn)

    def library() -> None:
        nonlocal library_running
        for gap in gaps:
            threads.sleep(float(gap))
            library_running = True
            events.on_event(agentshim.AssistantText(text="working"))

    def caller() -> None:
        threads.sleep(float(cancel_at))
        events.cancel(interrupt, turn)

    def main() -> None:
        workers = [
            threads.spawn(library, name="library", daemon=False),
            threads.spawn(caller, name="caller", daemon=False),
        ]
        for worker in workers:
            worker.join(HANG_GUARD_S)

    threads.run(main)

    assert events.cancelled
    assert turn.cancelled
    assert len(reached) == 1
