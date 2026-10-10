"""The server's locks, conditions and waits come from the injected ``Threads``.

On the simulator a journal wait that times out costs no wall time, and whether it times
out depends only on when the writer records, never on the machine's speed or on which
thread the schedule seed runs first.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st

from server.events import EventType
from vs_sim.api.testing import SimThreads

from .support import build_server_parts

if TYPE_CHECKING:
    import pytest

SEEDS = st.one_of(st.none(), st.integers(0, 2**32))
# Writer delays and waiter timeouts never coincide, so no timer ties.
WRITER_DELAYS = st.sampled_from([0.5, 3.0, 40.0])
WAIT_TIMEOUTS = st.sampled_from([1.0, 10.0, 100.0])


@settings(deadline=None, max_examples=40)
@given(seed=SEEDS, delay=WRITER_DELAYS, timeout=WAIT_TIMEOUTS)
def test_a_journal_wait_sees_a_record_exactly_when_it_lands_inside_the_timeout(
    seed: int | None, delay: float, timeout: float, tmp_path_factory: pytest.TempPathFactory
) -> None:
    threads = SimThreads(schedule_seed=seed)
    parts = build_server_parts(tmp_path_factory.mktemp("run") / "logs", threads=threads)
    cursor = parts.journal.latest_sequence

    def write() -> None:
        threads.sleep(delay)
        parts.journal.record(EventType.OUTPUT, "late")

    def wait() -> bool:
        threads.spawn(write, name="writer")
        return parts.journal.wait_for_change(cursor, timeout)

    assert threads.run(wait) is (delay < timeout)
