"""Unit contracts of the transport's subscription lifetime tracker."""

import threading

from tests.server.support import DEADLOCK_GUARD_S, FakeSettleWindow

from server.transport.subscriptions import SubscriptionTracker, ThreadingSettleWindow


def _wait_in_thread(tracker: SubscriptionTracker) -> tuple[threading.Thread, threading.Event]:
    returned = threading.Event()

    def wait_for_none() -> None:
        tracker.wait_for_none_active(settle_seconds=1.0)
        returned.set()

    waiter = threading.Thread(target=wait_for_none, daemon=True)
    waiter.start()
    return waiter, returned


def test_disconnect_wait_bridges_a_redial_inside_the_settle_window() -> None:
    settle = FakeSettleWindow()
    tracker = SubscriptionTracker(settle)
    first = tracker.track()
    first.__enter__()
    waiter, returned = _wait_in_thread(tracker)

    first.__exit__(None, None, None)
    settle.await_window(1)
    # The redial lands inside the first window, which ends the window without
    # the wait returning: only a window that opens after the redial closes can.
    redial = tracker.track()
    redial.__enter__()
    settle.await_window_end(1)
    assert not returned.is_set()
    redial.__exit__(None, None, None)
    settle.await_window(2)
    assert not returned.is_set()

    settle.elapse()
    assert returned.wait(timeout=DEADLOCK_GUARD_S)
    waiter.join(timeout=DEADLOCK_GUARD_S)
    assert not waiter.is_alive()


def test_disconnect_wait_returns_when_the_settle_window_elapses_with_no_stream() -> None:
    settle = FakeSettleWindow()
    tracker = SubscriptionTracker(settle)
    waiter, returned = _wait_in_thread(tracker)

    settle.await_window(1)
    settle.elapse()

    assert returned.wait(timeout=DEADLOCK_GUARD_S)
    waiter.join(timeout=DEADLOCK_GUARD_S)
    assert not waiter.is_alive()


def test_a_wait_does_not_open_a_window_while_a_stream_is_active() -> None:
    settle = FakeSettleWindow()
    tracker = SubscriptionTracker(settle)
    with tracker.track():
        waiter, returned = _wait_in_thread(tracker)
        assert not returned.is_set()
    settle.await_window(1)
    settle.elapse()

    assert returned.wait(timeout=DEADLOCK_GUARD_S)
    waiter.join(timeout=DEADLOCK_GUARD_S)


def test_the_threading_settle_window_satisfies_the_window_contract() -> None:
    """A predicate that already holds ends the wait True; an empty window ends it False."""
    condition = threading.Condition()
    window = ThreadingSettleWindow()
    with condition:
        assert window.wait_for(condition, lambda: True, 0.0) is True
        assert window.wait_for(condition, lambda: False, 0.0) is False
