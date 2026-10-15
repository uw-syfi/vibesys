"""Unit contracts of the transport's subscription lifetime tracker, on simulated threads."""

from __future__ import annotations

from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.server.support import wait_for_worker

from server.transport.subscriptions import (
    DeliveryWatermark,
    SubscriptionAdmissionClosedError,
    SubscriptionTracker,
    ThreadingSettleWindow,
)
from vs_sim.api.testing import SimThreads, examples, wait_or_fail

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


@settings(deadline=None, max_examples=examples(pr=10, nightly=60))
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


# A stream either writes the target batch after a delay, closes after a delay,
# or stalls (never writes it and never closes within the bound).
DRAIN_BOUND_SECONDS = 2.0
STREAM_ENDINGS = st.tuples(
    st.sampled_from(["delivers", "closes", "stalls"]), st.sampled_from([0.0, 0.5, 1.5])
)


@settings(deadline=None, max_examples=examples(pr=10, nightly=60))
@given(seed=SEEDS, endings=st.lists(STREAM_ENDINGS, max_size=4))
def test_a_drain_waits_exactly_until_every_open_stream_has_the_last_event(
    seed: int | None, endings: list[tuple[str, float]]
) -> None:
    """An exiting server's drain ends when no open stream is behind, or at its bound."""
    target = DeliveryWatermark(store_id="live-store", sequence=7)
    threads = SimThreads(schedule_seed=seed)
    tracker = SubscriptionTracker(threads=threads)

    def main() -> tuple[bool, float, str]:
        opened = [threads.event() for _ in endings]
        release = threads.event()

        def stream(index: int, ending: str, delay: float) -> None:
            with tracker.track() as delivery:
                delivery.delivered(
                    DeliveryWatermark(store_id=target.store_id, sequence=target.sequence - 1)
                )
                opened[index].set()
                threads.sleep(delay)
                if ending == "delivers":
                    delivery.delivered(target)
                if ending != "closes":
                    wait_or_fail(release, "the drain to end")

        workers = [
            threads.spawn(lambda i=i, e=e, d=d: stream(i, e, d), name=f"stream-{i}")
            for i, (e, d) in enumerate(endings)
        ]
        for index, event in enumerate(opened):
            wait_or_fail(event, f"stream {index} to open")
        started = threads.now()
        drained = tracker.wait_until_delivered(target, DRAIN_BOUND_SECONDS)
        waited = threads.now() - started
        try:
            with tracker.track():
                late_admission = "admitted"
        except SubscriptionAdmissionClosedError:
            late_admission = "refused"
        release.set()
        for worker in workers:
            wait_for_worker(worker)
        return drained, waited, late_admission

    drained, waited, late_admission = threads.run(main)

    stalled = any(ending == "stalls" for ending, _ in endings)
    assert drained is not stalled
    expected = DRAIN_BOUND_SECONDS if stalled else max((d for _, d in endings), default=0.0)
    assert waited == expected
    assert late_admission == "refused"


@settings(deadline=None, max_examples=examples(pr=10, nightly=60))
@given(
    seed=SEEDS,
    retired_sequence=st.integers(min_value=0, max_value=1_000),
    target_sequence=st.integers(min_value=1, max_value=1_000),
)
@example(seed=None, retired_sequence=100, target_sequence=30)
def test_a_retired_store_watermark_cannot_satisfy_the_live_store_drain(
    seed: int | None, retired_sequence: int, target_sequence: int
) -> None:
    """A high cursor from store A is not delivery evidence for store B."""
    threads = SimThreads(schedule_seed=seed)
    tracker = SubscriptionTracker(threads=threads)
    target = DeliveryWatermark(store_id="store-B", sequence=target_sequence)

    def main() -> None:
        drained = threads.event()
        with tracker.track() as delivery:

            def drain() -> None:
                if tracker.wait_until_delivered(target, DRAIN_BOUND_SECONDS):
                    drained.set()

            worker = threads.spawn(drain, name="terminal-drain")
            delivery.delivered(DeliveryWatermark(store_id="store-A", sequence=retired_sequence))
            threads.sleep(0.1)
            assert not drained.is_set()
            delivery.delivered(
                DeliveryWatermark(store_id=target.store_id, sequence=target_sequence - 1)
            )
            threads.sleep(0.1)
            assert not drained.is_set()
            delivery.delivered(target)
            wait_or_fail(drained, "the live-store watermark to be delivered")
            wait_for_worker(worker)

    threads.run(main)
