"""Native worker reservations preserve startup ownership across refusal, signals and fork.

These cases belong in the real tier because the guarantees cross CPython's native-thread
and POSIX boundaries. The deterministic ``ReservableThreads`` contract in ``libs/vs-sim``
covers the implementation-independent lifecycle; this file proves the exact native
behavior that a simulator cannot represent faithfully.
"""

from __future__ import annotations

import os
import signal
import threading
from functools import partial
from typing import TYPE_CHECKING

from server.host_api import ChannelEndOutcome
from server.relay import ClientStreams, relay
from vs_sim.api import OsThreads
from vs_sim.api.testing import (
    HANG_GUARD_S,
    OsThreadsBootstrapProbe,
    run_in_child,
    wait_or_fail,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType

    from vs_sim.api import Event, Worker, WorkerReservation


# How long the settlement signaler waits for an acknowledgement before it re-sends.
# Only the number of sends depends on it, never the outcome: see
# ``_interrupt_settlement_once``.
_RESIGNAL_S = 0.01


class _SettlementInterrupted(BaseException):
    """A real signal interrupted the main thread while it settled a worker."""


class _UnreachedSource:
    """A byte source that records an invalid read past refused relay startup."""

    def __init__(self) -> None:
        self.reads = 0

    def read(self, max_bytes: int) -> bytes:
        del max_bytes
        self.reads += 1
        return b"should-not-cross"


class _UnreachedSink:
    """A byte sink that records an invalid write past refused relay startup."""

    def __init__(self) -> None:
        self.writes = 0

    def write(self, data: bytes) -> None:
        del data
        self.writes += 1


class _UnreachedConnection:
    """An owned connection whose relay I/O remains untouched on bootstrap refusal."""

    def __init__(self) -> None:
        self.io_calls = 0
        self.close_calls = 0

    def send(self, data: bytes, timeout: float | None = None) -> None:
        del data, timeout
        self.io_calls += 1

    def recv(self, max_bytes: int, timeout: float | None = None) -> bytes:
        del max_bytes, timeout
        self.io_calls += 1
        return b""

    def peer_closed(self) -> bool:
        return False

    def close(self) -> None:
        self.close_calls += 1


def _join_worker(worker: Worker, what: str) -> None:
    worker.join(HANG_GUARD_S)
    if worker.is_alive():
        message = f"timed out waiting for {what}"
        raise AssertionError(message)


def _starter_threads() -> int:
    # test-isolation: this real-tier ownership regression counts the implementation's
    # process-lifetime worker because public reservation behavior cannot expose an orphan.
    return sum(worker.name == "vs-sim-worker-starter" for worker in threading.enumerate())


def _construct_without_reserving() -> tuple[int, int]:
    before = _starter_threads()
    OsThreads()
    return before, _starter_threads()


def test_plain_os_threads_construction_does_not_launch_a_reservation_worker() -> None:
    """General lock/event users pay no reservation-only thread or failure boundary."""
    assert run_in_child(_construct_without_reserving) == (0, 0)


def _relay_with_refused_dispatcher_bootstrap() -> tuple[str, str, int, int, int, int]:
    """Refuse the first real ``OsThreads`` dispatcher and report the public outcome."""
    bootstrap = OsThreadsBootstrapProbe(refusals=1)
    connection = _UnreachedConnection()
    incoming = _UnreachedSource()
    outgoing = _UnreachedSink()

    result = relay(
        connection,
        client=ClientStreams(incoming, outgoing),
        threads=bootstrap.threads,
    )

    return (
        result.outcome.value,
        result.detail,
        bootstrap.attempts,
        incoming.reads,
        connection.io_calls + outgoing.writes,
        connection.close_calls,
    )


def test_refused_os_dispatcher_ends_relay_without_a_second_bootstrap_attempt() -> None:
    """A closed first reservation refusal cannot retry native bootstrap downstream."""
    assert run_in_child(_relay_with_refused_dispatcher_bootstrap) == (
        ChannelEndOutcome.CONNECT_FAILED.value,
        "relay copy worker start was refused",
        1,
        0,
        0,
        1,
    )


def _has_concurrency_frame(frame: FrameType | None, method: str) -> bool:
    while frame is not None:
        if frame.f_code.co_name == method and frame.f_code.co_filename.endswith(
            "/vs_sim/concurrency.py"
        ):
            return True
        frame = frame.f_back
    return False


def _is_owner_wait(frame: FrameType | None, method: str) -> bool:
    return (
        frame is not None
        and frame.f_code.co_name == "wait"
        and frame.f_code.co_filename.endswith("/threading.py")
        and _has_concurrency_frame(frame, method)
    )


def _is_owner_block(frame: FrameType | None, method: str) -> bool:
    """Whether ``frame`` is the blocking ``Condition.wait`` under the owner's ``method``."""
    return (
        frame is not None
        and frame.f_code is threading.Condition.wait.__code__
        and _has_concurrency_frame(frame, method)
    )


def _inside_owner_wait(frame: FrameType | None, method: str) -> bool:
    """Whether ``frame`` or one of its callers is a threading wait under ``method``.

    A Python signal handler runs at the owner's next evaluation check, which may be in
    a callee of the wait (a ``Condition`` helper, or this file's trace hook) rather than
    in the ``wait`` frame itself.
    """
    while frame is not None:
        if _is_owner_wait(frame, method):
            return True
        frame = frame.f_back
    return False


def _publish_owner_wait(
    owner_id: int,
    method: str,
    reached: threading.Event,
    *,
    boundary: Callable[[FrameType | None, str], bool] = _is_owner_wait,
) -> Callable[[FrameType, str, object], object]:
    """Return a trace hook that publishes the exact owner-wait boundary once."""

    def publish(frame: FrameType, _event: str, _argument: object) -> object:
        if threading.get_ident() == owner_id and boundary(frame, method):
            reached.set()
        return publish

    return publish


def _interrupt_lazy_starter_bootstrap() -> tuple[bool, bool, int, bool]:
    """Make SIGALRM pending after ownership publication but before mask restoration."""
    threads = OsThreads()
    signal_sent = False
    boundary_observed = False

    def signal_from_dispatcher(
        _frame: FrameType,
        _event: str,
        _argument: object,
    ) -> object:
        nonlocal signal_sent
        if threading.current_thread().name == "vs-sim-worker-starter" and not signal_sent:
            signal_sent = True
            # The dispatcher inherited the blocked mask. A process-directed signal
            # therefore stays pending until the owner restores its old mask.
            os.kill(os.getpid(), signal.SIGALRM)
        return signal_from_dispatcher

    def interrupt_bootstrap(_number: int, frame: FrameType | None) -> None:
        nonlocal boundary_observed
        boundary_observed = (
            frame is not None
            and frame.f_code.co_name == "pthread_sigmask"
            and _has_concurrency_frame(frame, "get")
        )
        if not boundary_observed:
            message = "SIGALRM landed outside dispatcher mask restoration"
            raise AssertionError(message)
        raise _SettlementInterrupted

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_trace = threading.gettrace()
    signal.signal(signal.SIGALRM, interrupt_bootstrap)
    threading.settrace_all_threads(signal_from_dispatcher)
    interrupted = False
    try:
        try:
            threads.reserve(lambda: None, name="interrupted-bootstrap")
        except _SettlementInterrupted:
            interrupted = True

        finished = threads.event()
        retried = threads.reserve(finished.set, name="reservation-after-interruption")
        retried.start()
        retried.settle()
        return interrupted, boundary_observed, _starter_threads(), finished.is_set()
    finally:
        threading.settrace_all_threads(previous_trace)
        signal.signal(signal.SIGALRM, previous_handler)


def test_interrupted_lazy_bootstrap_retains_one_reachable_dispatcher() -> None:
    """Pending delivery after native launch cannot orphan the process singleton."""
    assert run_in_child(_interrupt_lazy_starter_bootstrap) == (True, True, 1, True)


def _interrupt_settlement_once(
    threads: OsThreads,
    reservation: WorkerReservation,
    main_thread_id: int,
) -> tuple[bool, bool]:
    """Interrupt the owner exactly once while it blocks in the target-completion wait.

    One signal cannot be aimed at a blocked thread: the owner publishes its wait before
    it parks, and a signal that arrives in between runs only the C-level handler, after
    which the owner sleeps through it without ``EINTR``. Only the target's end would
    then wake it, so the interruption would be lost. The signaler therefore re-sends to
    the owner thread until the handler acknowledges; every send lands inside the
    blocking wait, and the handler acts on the first one only.
    """
    boundary_observed = False
    handled = False
    acknowledged = threading.Event()
    wait_reached = threading.Event()

    def interrupt_settlement(_number: int, frame: FrameType | None) -> None:
        nonlocal boundary_observed, handled
        if handled:
            # A re-send that crossed the acknowledgement; the owner already unwound.
            return
        handled = True
        acknowledged.set()
        boundary_observed = _inside_owner_wait(frame, "settle")
        if not boundary_observed:
            message = "SIGALRM landed outside reservation settlement"
            raise AssertionError(message)
        raise _SettlementInterrupted

    def signal_during_settlement() -> None:
        assert wait_reached.wait(HANG_GUARD_S)
        for _ in range(int(HANG_GUARD_S / _RESIGNAL_S)):
            signal.pthread_kill(main_thread_id, signal.SIGALRM)
            if acknowledged.wait(_RESIGNAL_S):
                return
        message = "the owner never handled SIGALRM"
        raise AssertionError(message)

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_trace = threading.gettrace()
    signal.signal(signal.SIGALRM, interrupt_settlement)
    threading.settrace_all_threads(
        _publish_owner_wait(main_thread_id, "settle", wait_reached, boundary=_is_owner_block)
    )
    signaler = threads.spawn(signal_during_settlement, name="signal-during-settlement")
    interrupted = False
    try:
        try:
            reservation.settle()
        except _SettlementInterrupted:
            interrupted = True
        _join_worker(signaler, "the settlement signaler")
    finally:
        threading.settrace_all_threads(previous_trace)
        signal.signal(signal.SIGALRM, previous_handler)
    return interrupted, boundary_observed


def _exercise_native_reservation() -> tuple[bool, bool, bool]:
    """Run in a child because handlers and masks are process-wide test state."""
    threads = OsThreads()
    expected_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    observed_masks: list[set[int]] = []
    release_observed: list[bool] = []
    entered = threads.event()
    release = threads.event()
    finished = threads.event()

    def target() -> None:
        observed_masks.append(signal.pthread_sigmask(signal.SIG_BLOCK, set()))
        entered.set()
        try:
            release_observed.append(release.wait(HANG_GUARD_S))
        finally:
            finished.set()

    reservation = threads.reserve(target, name="interruptible-settlement")
    reservation.start()
    wait_or_fail(entered, "the reserved target entering")
    main_thread_id = threading.get_ident()
    interrupted, boundary_observed = _interrupt_settlement_once(
        threads, reservation, main_thread_id
    )
    wait_reached = threading.Event()
    previous_trace = threading.gettrace()
    threading.settrace_all_threads(_publish_owner_wait(main_thread_id, "settle", wait_reached))
    try:

        def release_during_settlement() -> None:
            assert wait_reached.wait(HANG_GUARD_S)
            release.set()

        # A CPython join interrupted above can falsely report the thread stopped. Keep
        # the target blocked until a helper observes the retry waiting for its target.
        releaser = threads.spawn(release_during_settlement, name="release-during-settlement")
        reservation.settle()
        _join_worker(releaser, "the target releaser")
        completed_when_settled = finished.is_set()
    finally:
        threading.settrace_all_threads(previous_trace)
        release.set()
        reservation.settle()

    parent_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    child_mask_restored = observed_masks == [expected_mask]
    parent_mask_restored = parent_mask == expected_mask
    masks_and_release_are_sound = (
        boundary_observed
        and child_mask_restored
        and parent_mask_restored
        and release_observed == [True]
    )
    return interrupted, completed_when_settled, masks_and_release_are_sound


def test_native_reservation_settlement_uses_target_completion_evidence() -> None:
    """An interrupted native wait stays retryable and neither thread leaks its signal mask."""
    assert run_in_child(_exercise_native_reservation) == (True, True, True)


def _exercise_interrupted_start() -> tuple[bool, bool, bool, bool]:
    """Have the target itself interrupt the owner while ``start`` awaits its outcome."""
    threads = OsThreads()
    main_thread_id = threading.get_ident()
    entered = threads.event()
    finished = threads.event()
    wait_reached = threading.Event()
    signal_sent = threading.Event()
    boundary_observed = False

    def target() -> None:
        entered.set()
        try:
            assert wait_reached.wait(HANG_GUARD_S)
            os.kill(os.getpid(), signal.SIGALRM)
            signal_sent.set()
        finally:
            finished.set()

    def hold_outcome_publication(
        frame: FrameType,
        _event: str,
        _argument: object,
    ) -> object:
        if threading.get_ident() == main_thread_id and _is_owner_wait(frame, "start"):
            wait_reached.set()
        if (
            threading.current_thread().name == "vs-sim-worker-starter"
            and frame.f_code.co_name == "dispatch"
            and entered.is_set()
            and not signal_sent.is_set()
        ):
            assert signal_sent.wait(HANG_GUARD_S)
        return hold_outcome_publication

    def interrupt_start(_number: int, frame: FrameType | None) -> None:
        nonlocal boundary_observed
        boundary_observed = _inside_owner_wait(frame, "start")
        if not boundary_observed:
            message = "target SIGALRM landed outside the reservation start outcome wait"
            raise AssertionError(message)
        raise _SettlementInterrupted

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_trace = threading.gettrace()
    signal.signal(signal.SIGALRM, interrupt_start)
    reservation = threads.reserve(target, name="interrupted-native-start")
    threading.settrace_all_threads(hold_outcome_publication)
    interrupted = False
    try:
        try:
            reservation.start()
        except _SettlementInterrupted:
            interrupted = True
        target_entered = entered.is_set()
        reservation.settle()
        completed_when_settled = finished.is_set()
    finally:
        threading.settrace_all_threads(previous_trace)
        signal.signal(signal.SIGALRM, previous_handler)
        reservation.settle()
    return interrupted, boundary_observed, target_entered, completed_when_settled


def test_native_reservation_start_outcome_survives_caller_interruption() -> None:
    """A signal cannot interrupt native start ownership; settlement still proves target end."""
    assert run_in_child(_exercise_interrupted_start) == (True, True, True, True)


def _reserve_after_fork(threads: OsThreads, parent_finished: Event) -> tuple[bool, bool]:
    """Use an inherited facade while only the fresh child dispatcher can run."""
    child_finished = threads.event()
    reservation = threads.reserve(child_finished.set, name="reserved-after-fork")
    reservation.start()
    reservation.settle()
    return child_finished.is_set(), parent_finished.is_set()


def test_a_child_drops_the_parent_dispatcher_and_its_queue_after_fork() -> None:
    """A child creates a fresh dispatcher while parent workers stay only in the parent."""
    threads = OsThreads()
    parent_entered = threads.event()
    parent_release = threads.event()
    parent_finished = threads.event()

    def parent_target() -> None:
        parent_entered.set()
        try:
            parent_release.wait(HANG_GUARD_S)
        finally:
            parent_finished.set()

    parent_reservation = threads.reserve(parent_target, name="parent-before-fork")
    parent_reservation.start()
    wait_or_fail(parent_entered, "the parent target entering")
    try:
        result = run_in_child(partial(_reserve_after_fork, threads, parent_finished))
    finally:
        parent_release.set()
        parent_reservation.settle()

    assert result == (True, False)
    assert parent_finished.is_set()
