"""Unit contracts of the transport's subscription lifetime tracker, on simulated threads."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.server.support import wait_for_worker

from server.transport.subscriptions import SubscriptionTracker, ThreadingSettleWindow
from vs_sim.api.testing import SimThreads, wait_or_fail

SETTLE_SECONDS = 1.0
SEEDS = st.one_of(st.none(), st.integers(0, 2**32))
STREAM_SECONDS = st.sampled_from([0.5, 2.0, 7.0])
# Never equal to the settle window, so a redial never ties with its end.
REDIAL_GAPS = st.sampled_from([0.25, 0.75, 1.5, 4.0])


def _disconnect_wait_seconds(
    seed: int | None, first: float, redials: list[tuple[float, float]]
) -> float:
    """Simulated seconds `wait_for_none_active` blocks for a stream and its later redials."""
    threads = SimThreads(schedule_seed=seed)
    tracker = SubscriptionTracker(threads=threads)

    def main() -> float:
        opened = threads.event()

        def client() -> None:
            with tracker.track():
                opened.set()
                threads.sleep(first)
            for gap, duration in redials:
                threads.sleep(gap)
                with tracker.track():
                    threads.sleep(duration)

        worker = threads.spawn(client, name="client")
        wait_or_fail(opened, "the first stream to open")
        started = threads.now()
        tracker.wait_for_none_active(settle_seconds=SETTLE_SECONDS)
        waited = threads.now() - started
        wait_for_worker(worker)
        return waited

    return threads.run(main)


def _bridged(redials: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The leading redials that land inside the settle window of the stream before them."""
    bridged = []
    for redial in redials:
        if redial[0] >= SETTLE_SECONDS:
            break
        bridged.append(redial)
    return bridged


def _expected_wait_seconds(first: float, redials: list[tuple[float, float]]) -> float:
    """The wait ends one settle window after the last stream a redial bridged to."""
    return first + sum(gap + duration for gap, duration in redials) + SETTLE_SECONDS


@settings(deadline=None, max_examples=60)
@given(
    seed=SEEDS,
    first=STREAM_SECONDS,
    redials=st.lists(st.tuples(REDIAL_GAPS, STREAM_SECONDS), max_size=3),
)
def test_disconnect_wait_bridges_exactly_the_redials_inside_the_settle_window(
    seed: int | None, first: float, redials: list[tuple[float, float]]
) -> None:
    bridged = _bridged(redials)
    # The client also makes the first redial that misses the window; it must not extend the wait.
    attempted = redials[: len(bridged) + 1]

    assert _disconnect_wait_seconds(seed, first, attempted) == _expected_wait_seconds(
        first, bridged
    )


@given(seed=SEEDS)
def test_a_wait_with_no_stream_ever_opened_returns_after_one_settle_window(
    seed: int | None,
) -> None:
    threads = SimThreads(schedule_seed=seed)
    tracker = SubscriptionTracker(threads=threads)

    def main() -> float:
        started = threads.now()
        tracker.wait_for_none_active(settle_seconds=SETTLE_SECONDS)
        return threads.now() - started

    assert threads.run(main) == SETTLE_SECONDS


def test_the_threading_settle_window_satisfies_the_window_contract() -> None:
    """A predicate that already holds ends the wait True; an empty window ends it False."""
    threads = SimThreads()
    condition = threads.condition()
    window = ThreadingSettleWindow()

    def main() -> tuple[bool, bool]:
        with condition:
            return (
                window.wait_for(condition, lambda: True, 0.0),
                window.wait_for(condition, lambda: False, 0.0),
            )

    assert threads.run(main) == (True, False)
