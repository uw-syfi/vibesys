"""The relay core (``server.relay``), on the simulated network and threads.

The pure fold (:func:`advance`) is checked directly over arbitrary event sequences. The
shell (:func:`run_bridge`) runs against :class:`SimNetwork` with Fakes for stdin and
stdout, under seeded schedules, so chunk boundaries, ends and stalls interleave in many
orders while every deadline is virtual time. The same relay against a real server
process is exercised in ``tests/e2e/test_relay.py``.
"""

from __future__ import annotations

import errno
import itertools
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from server.host_api import ChannelEnd, ChannelEndOutcome
from server.relay import (
    EXIT_STATUS,
    BridgeLimits,
    BridgeResult,
    ClientStreams,
    Ended,
    EndReason,
    Flow,
    Peer,
    ReadProgress,
    RelayEvent,
    RelayState,
    Tick,
    WriteFinished,
    WriteStarted,
    advance,
    ending,
    next_deadline,
    relay,
    run_bridge,
)
from vs_sim.api import WorkerStartRefusedError
from vs_sim.api.testing import (
    HANG_GUARD_S,
    RefusingReservableThreads,
    SimNetwork,
    SimThreads,
    examples,
    wait_or_fail,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from vs_sim.api import Condition, Connection, Event, Listener, Lock, WorkerReservation

_LIMITS = BridgeLimits()
_DEADLINE = _LIMITS.write_deadline_seconds
_ADDRESS = "run.sock"

# ----- the pure fold -----

_peers = st.sampled_from(Peer)
_events: st.SearchStrategy[RelayEvent] = st.one_of(
    st.builds(WriteStarted, _peers, st.floats(0, 200)),
    st.builds(WriteFinished, _peers),
    st.builds(Ended, _peers, st.sampled_from(Flow), st.sampled_from(EndReason)),
    st.builds(Tick, st.floats(0, 200)),
)
# ``ReadProgress`` is excluded from ``_events``: with the default read deadline enabled,
# a bare write-stall fold (``test_a_write_in_flight_stalls_exactly_at_its_deadline``)
# would otherwise sometimes land on ``SERVER_UNRESPONSIVE`` instead of a write stall. The
# read-deadline fold has its own property test below, over its own event strategy.
_read_events: st.SearchStrategy[RelayEvent] = st.one_of(
    _events, st.builds(ReadProgress, _peers, st.floats(0, 200))
)


def _fold(events: list[RelayEvent]) -> list[RelayState]:
    states = [RelayState()]
    for event in events:
        states.append(advance(states[-1], event, _LIMITS))
    return states


@given(st.lists(_events, max_size=30))
def test_the_first_decided_outcome_is_final(events: list[RelayEvent]) -> None:
    decided = {state.outcome for state in _fold(events) if state.outcome is not None}
    assert len(decided) <= 1


@given(st.lists(_events, max_size=30), st.floats(1e-3, 100))
@example(events=[WriteStarted(Peer.CLIENT, 88.40148519386226)], early_by=1.0)
def test_a_write_in_flight_stalls_exactly_at_its_deadline(
    events: list[RelayEvent], early_by: float
) -> None:
    state = _fold(events)[-1]
    deadline = next_deadline(state, _LIMITS)
    if deadline is None:
        assert state.outcome is not None or (
            state.client_write_since is None and state.server_write_since is None
        )
        return
    early = advance(state, Tick(deadline - early_by), _LIMITS)
    assert early.outcome is None
    stalled = advance(state, Tick(deadline), _LIMITS)
    assert stalled.outcome in {ChannelEndOutcome.CLIENT_STALLED, ChannelEndOutcome.SERVER_STALLED}


@given(st.lists(_read_events, max_size=30), st.floats(1e-3, 100))
@example(events=[ReadProgress(Peer.SERVER, 12.5)], early_by=1.0)
def test_a_silent_server_read_is_unresponsive_exactly_at_its_deadline(
    events: list[RelayEvent], early_by: float
) -> None:
    """The earliest tracked deadline, write or read, decides on time and no sooner."""
    state = _fold(events)[-1]
    deadline = next_deadline(state, _LIMITS)
    if deadline is None:
        assert state.outcome is not None or (
            state.client_write_since is None
            and state.server_write_since is None
            and state.server_read_at is None
        )
        return
    early = advance(state, Tick(deadline - early_by), _LIMITS)
    assert early.outcome is None
    fired = advance(state, Tick(deadline), _LIMITS)
    assert fired.outcome in {
        ChannelEndOutcome.CLIENT_STALLED,
        ChannelEndOutcome.SERVER_STALLED,
        ChannelEndOutcome.SERVER_UNRESPONSIVE,
    }


@pytest.mark.parametrize("peer", list(Peer))
@pytest.mark.parametrize("flow", list(Flow))
@pytest.mark.parametrize("reason", list(EndReason))
def test_every_end_has_one_classification(peer: Peer, flow: Flow, reason: EndReason) -> None:
    decided = ending(Ended(peer, flow, reason))
    if reason is EndReason.FAILED:
        assert (
            decided
            is {
                Peer.CLIENT: ChannelEndOutcome.CLIENT_FAILED,
                Peer.SERVER: ChannelEndOutcome.SERVER_FAILED,
            }[peer]
        )
    elif peer is Peer.SERVER and flow is Flow.TO_PEER:
        # The server's last frame may still be in flight toward the client.
        assert decided is None
    else:
        assert (
            decided
            is {
                Peer.CLIENT: ChannelEndOutcome.CLIENT_CLOSED,
                Peer.SERVER: ChannelEndOutcome.SERVER_CLOSED,
            }[peer]
        )


def test_a_write_that_finds_the_server_closed_is_not_left_to_stall() -> None:
    state = _fold(
        [WriteStarted(Peer.SERVER, 0.0), Ended(Peer.SERVER, Flow.TO_PEER, EndReason.CLOSED)]
    )[-1]
    assert state.outcome is None
    assert next_deadline(state, _LIMITS) is None
    assert advance(state, Tick(_DEADLINE * 10), _LIMITS).outcome is None


def test_every_outcome_has_its_own_exit_status_and_only_a_client_close_is_zero() -> None:
    assert set(EXIT_STATUS) == set(ChannelEndOutcome)
    assert len(set(EXIT_STATUS.values())) == len(ChannelEndOutcome)
    assert [o for o, status in EXIT_STATUS.items() if status == 0] == [
        ChannelEndOutcome.CLIENT_CLOSED
    ]
    assert {1, 2}.isdisjoint(EXIT_STATUS.values())


@pytest.mark.parametrize("outcome", list(ChannelEndOutcome))
def test_the_report_line_is_one_json_object_naming_outcome_and_status(
    outcome: ChannelEndOutcome,
) -> None:
    line = BridgeResult(outcome, "why").channel_end().line()
    assert line.endswith("\n")
    assert line.count("\n") == 1
    assert json.loads(line) == {
        "outcome": outcome.value,
        "exit_status": EXIT_STATUS[outcome],
        "detail": "why",
    }
    assert ChannelEnd.model_validate_json(line).outcome is outcome


# ----- the shell, on simulated threads and network -----


@dataclass
class _Stdin:
    """Hands out scripted chunks, then waits until ``finish`` before its end of stream."""

    threads: SimThreads
    chunks: list[bytes]
    error: OSError | None = None
    finished: bool = False

    def __post_init__(self) -> None:
        self._cond = self.threads.condition()

    def finish(self) -> None:
        with self._cond:
            self.finished = True
            self._cond.notify_all()

    def read(self, max_bytes: int) -> bytes:
        with self._cond:
            if self.chunks:
                chunk = self.chunks.pop(0)
                if len(chunk) > max_bytes:
                    self.chunks.insert(0, chunk[max_bytes:])
                return chunk[:max_bytes]
            self._cond.wait_for(lambda: self.finished)
            if self.error is not None:
                raise self.error
            return b""


@dataclass
class _Stdout:
    """Records what the relay writes; a stalled one never returns from a write."""

    threads: SimThreads
    stalled: bool = False
    data: bytearray = field(default_factory=bytearray)

    def __post_init__(self) -> None:
        self._cond = self.threads.condition()

    def write(self, data: bytes) -> None:
        with self._cond:
            if self.stalled:
                self._cond.wait_for(lambda: False)
            self.data += data
            self._cond.notify_all()

    def wait_for(self, size: int) -> None:
        with self._cond:
            assert self._cond.wait_for(lambda: len(self.data) >= size, HANG_GUARD_S)


class _ObservedRefusingThreads(RefusingReservableThreads):
    """Record worker ownership and the exact eager/refusal interleaving."""

    def __init__(self, delegate: SimThreads, refusal: str) -> None:
        super().__init__(delegate, {refusal: 1})
        self.events: list[str] = []
        self.reservations: list[_ObservedReservation] = []

    def reserve(
        self, target: Callable[[], object], *, name: str, daemon: bool = True
    ) -> WorkerReservation:
        entered = self.event()
        exited = self.event()

        def observed() -> object:
            self.events.append(f"{name}:entered")
            entered.set()
            try:
                return target()
            finally:
                self.events.append(f"{name}:exited")
                exited.set()

        reservation = _ObservedReservation(
            self,
            super().reserve(observed, name=name, daemon=daemon),
            entered,
            exited,
        )
        self.reservations.append(reservation)
        return reservation

    @property
    def active(self) -> bool:
        return any(reservation.active for reservation in self.reservations)


class _ReservationAcquisitionRefusedThreads(RefusingReservableThreads):
    """Refuse native preparation before the first reservation can be returned."""

    def __init__(self, delegate: SimThreads) -> None:
        super().__init__(delegate, {})

    def reserve(
        self, target: Callable[[], object], *, name: str, daemon: bool = True
    ) -> WorkerReservation:
        del target, daemon
        raise WorkerStartRefusedError(f"vs-sim-worker-starter-for-{name}")


class _NotifyInterruptedCondition:
    """A public ``Condition`` Fake whose first broadcast is interrupted."""

    def __init__(self, delegate: Condition, failure: BaseException) -> None:
        self._delegate = delegate
        self._failure = failure
        self.notification_attempts = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:  # noqa: FBT001, FBT002  # LW-921774 [FBT001, FBT002]; mirrors the public Condition contract under test.
        return self._delegate.acquire(blocking, timeout)

    def release(self) -> None:
        self._delegate.release()

    def __enter__(self) -> object:
        return self._delegate.__enter__()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
        /,
    ) -> None:
        self._delegate.__exit__(exc_type, exc, tb)

    def wait(self, timeout: float | None = None) -> bool:
        return self._delegate.wait(timeout)

    def wait_for(self, predicate: Callable[[], bool], timeout: float | None = None) -> bool:
        return self._delegate.wait_for(predicate, timeout)

    def notify(self, n: int = 1) -> None:
        self._delegate.notify(n)

    def notify_all(self) -> None:
        self.notification_attempts += 1
        if self.notification_attempts == 1:
            raise self._failure
        self._delegate.notify_all()


class _WaitAfterNotifyInterruptedCondition(_NotifyInterruptedCondition):
    """Interrupt the owner's first wait after commit notification succeeds."""

    def __init__(self, delegate: Condition, failure: BaseException) -> None:
        super().__init__(delegate, failure)
        self._interrupt_next_wait = False

    def wait(self, timeout: float | None = None) -> bool:
        if self._interrupt_next_wait:
            self._interrupt_next_wait = False
            raise self._failure
        return self._delegate.wait(timeout)

    def notify_all(self) -> None:
        self.notification_attempts += 1
        self._delegate.notify_all()
        if self.notification_attempts == 1:
            self._interrupt_next_wait = True


class _GateNotifyInterruptedThreads(_ObservedRefusingThreads):
    """Interrupt commit notification or the transition into supervision."""

    def __init__(
        self,
        delegate: SimThreads,
        refusal: str,
        failure: BaseException,
        *,
        interrupt_after_notify: bool = False,
    ) -> None:
        super().__init__(delegate, refusal)
        self._failure = failure
        self._interrupt_after_notify = interrupt_after_notify
        self._condition_count = 0
        self.gate_condition: _NotifyInterruptedCondition | None = None

    def condition(self, lock: Lock | None = None) -> Condition:
        condition = super().condition(lock)
        self._condition_count += 1
        # The startup gate and supervisor deliberately share _Relay's first condition.
        if self._condition_count == 1:
            if self._interrupt_after_notify:
                interrupted = _WaitAfterNotifyInterruptedCondition(condition, self._failure)
            else:
                interrupted = _NotifyInterruptedCondition(condition, self._failure)
            self.gate_condition = interrupted
            return interrupted
        return condition


class _ObservedReservation:
    def __init__(
        self,
        owner: _ObservedRefusingThreads,
        reservation: WorkerReservation,
        entered: Event,
        exited: Event,
    ) -> None:
        self._owner = owner
        self._reservation = reservation
        self.entered = entered
        self._exited = exited

    @property
    def name(self) -> str:
        return self._reservation.name

    @property
    def active(self) -> bool:
        return self.entered.is_set() and not self._exited.is_set()

    def start(self) -> None:
        try:
            self._reservation.start()
        except WorkerStartRefusedError:
            self._owner.events.append(f"{self.name}:refused")
            raise

    def settle(self) -> None:
        self._reservation.settle()


class _StartRaisesThreads(_ObservedRefusingThreads):
    """Start one reserved worker, then raise before returning from its start."""

    def __init__(
        self,
        delegate: SimThreads,
        name: str,
        failure: BaseException,
        *,
        settle_name: str | None = None,
        settle_failure: BaseException | None = None,
    ) -> None:
        super().__init__(delegate, "never-refused")
        self._name = name
        self._failure = failure
        self._settle_name = settle_name
        self._settle_failure = settle_failure

    def reserve(
        self, target: Callable[[], object], *, name: str, daemon: bool = True
    ) -> WorkerReservation:
        reservation = super().reserve(target, name=name, daemon=daemon)
        assert isinstance(reservation, _ObservedReservation)
        result: WorkerReservation = reservation
        if name == self._name:
            result = _StartRaisesReservation(self.events, reservation, self._failure)
        if name == self._settle_name:
            assert self._settle_failure is not None
            result = _SettleRaisesReservation(self.events, result, self._settle_failure)
        return result


class _StartRaisesReservation:
    def __init__(
        self,
        events: list[str],
        reservation: _ObservedReservation,
        failure: BaseException,
    ) -> None:
        self._events = events
        self._reservation = reservation
        self._failure = failure

    @property
    def name(self) -> str:
        return self._reservation.name

    def start(self) -> None:
        self._reservation.start()
        wait_or_fail(self._reservation.entered, f"{self.name} target entering")
        self._events.append(f"{self.name}:raised")
        raise self._failure

    def settle(self) -> None:
        self._reservation.settle()


class _SettleRaisesReservation:
    """Interrupt one settlement attempt, then delegate the required retry."""

    def __init__(
        self,
        events: list[str],
        reservation: WorkerReservation,
        failure: BaseException,
    ) -> None:
        self._events = events
        self._reservation = reservation
        self._failure = failure
        self._interrupted = False

    @property
    def name(self) -> str:
        return self._reservation.name

    def start(self) -> None:
        self._reservation.start()

    def settle(self) -> None:
        if not self._interrupted:
            self._interrupted = True
            self._events.append(f"{self.name}:settle-interrupted")
            raise self._failure
        self._reservation.settle()


class _CatastrophicStartError(BaseException):
    """A non-Exception failure at the worker-start boundary."""


class _GateNotificationInterrupted(BaseException):
    """An asynchronous exit inside the commit-to-supervision transition."""


@dataclass
class _ObservedConnection:
    """A connection Fake that exposes relay I/O and exact ownership release."""

    sent: bytearray = field(default_factory=bytearray)
    receives: int = 0
    close_calls: int = 0

    def send(self, data: bytes, timeout: float | None = None) -> None:
        del timeout
        self.sent += data

    def recv(self, max_bytes: int, timeout: float | None = None) -> bytes:
        del max_bytes, timeout
        self.receives += 1
        return b""

    def peer_closed(self) -> bool:
        return False

    def close(self) -> None:
        self.close_calls += 1


@pytest.mark.parametrize("failed_name", ["relay-upstream", "relay-downstream"])
def test_relay_reports_copy_worker_start_refusal(failed_name: str) -> None:
    """Either copy-worker refusal is a typed, closed relay outcome rather than an exception."""
    simulator = SimThreads(schedule_seed=0)
    threads = RefusingReservableThreads(simulator, {failed_name: 1})
    connection = _ObservedConnection()

    result = simulator.run(
        lambda: relay(
            connection,
            client=ClientStreams(
                _Stdin(simulator, [b"{}\n"], finished=True),
                _Stdout(simulator),
            ),
            threads=threads,
        )
    )

    assert result == BridgeResult(
        ChannelEndOutcome.CONNECT_FAILED,
        "relay copy worker start was refused",
    )
    assert connection.close_calls == 1


def test_dispatcher_bootstrap_refusal_is_a_closed_relay_outcome() -> None:
    """Reservation acquisition failure is inside startup ownership and crosses no bytes."""
    simulator = SimThreads(schedule_seed=0)
    threads = _ReservationAcquisitionRefusedThreads(simulator)
    connection = _ObservedConnection()
    stdin = _Stdin(simulator, [b'{"type":"command.stop"}\n'], finished=True)
    stdout = _Stdout(simulator)

    result = simulator.run(
        lambda: relay(
            connection,
            client=ClientStreams(stdin, stdout),
            threads=threads,
        )
    )

    assert result == BridgeResult(
        ChannelEndOutcome.CONNECT_FAILED,
        "relay copy worker start was refused",
    )
    assert stdin.chunks == [b'{"type":"command.stop"}\n']
    assert connection.sent == b""
    assert connection.receives == 0
    assert stdout.data == b""
    assert connection.close_calls == 1


def test_first_worker_refusal_aborts_relay_startup_without_crossing_bytes() -> None:
    """First-worker refusal closes once and returns typed evidence without a worker."""
    simulator = SimThreads(schedule_seed=0)
    threads = _ObservedRefusingThreads(simulator, "relay-upstream")
    connection = _ObservedConnection()
    stdin = _Stdin(simulator, [b'{"type":"command.stop"}\n'], finished=True)
    stdout = _Stdout(simulator)

    result, active_at_return = simulator.run(
        lambda: (
            relay(
                connection,
                client=ClientStreams(stdin, stdout),
                threads=threads,
            ),
            threads.active,
        )
    )

    assert result == BridgeResult(
        ChannelEndOutcome.CONNECT_FAILED,
        "relay copy worker start was refused",
    )
    assert result.exit_status == EXIT_STATUS[ChannelEndOutcome.CONNECT_FAILED]
    assert stdin.chunks == [b'{"type":"command.stop"}\n']
    assert connection.sent == b""
    assert connection.receives == 0
    assert stdout.data == b""
    assert connection.close_calls == 1
    assert not active_at_return


def test_second_worker_refusal_aborts_an_eager_first_worker_before_io() -> None:
    """The first worker may enter before the second refusal, but it cannot read."""
    simulator = SimThreads(schedule_seed=0)
    threads = _ObservedRefusingThreads(simulator, "relay-downstream")
    connection = _ObservedConnection()
    stdin = _Stdin(simulator, [b'{"type":"command.stop"}\n'], finished=True)

    result, active_at_return = simulator.run(
        lambda: (
            relay(
                connection,
                client=ClientStreams(stdin, _Stdout(simulator)),
                threads=threads,
            ),
            threads.active,
        )
    )

    assert result.outcome is ChannelEndOutcome.CONNECT_FAILED
    assert threads.events.index("relay-upstream:entered") < threads.events.index(
        "relay-downstream:refused"
    )
    assert stdin.chunks == [b'{"type":"command.stop"}\n']
    assert connection.sent == b""
    assert connection.receives == 0
    assert connection.close_calls == 1
    assert not active_at_return


def test_aborted_startup_retries_notification_after_publishing_its_decision() -> None:
    """A failed abort broadcast is retried, so an eager copy worker can settle."""
    simulator = SimThreads(schedule_seed=0)
    failure = _GateNotificationInterrupted("abort notification interrupted")
    threads = _GateNotifyInterruptedThreads(
        simulator,
        "relay-downstream",
        failure,
    )
    connection = _ObservedConnection()
    stdin = _Stdin(simulator, [b'{"type":"command.stop"}\n'], finished=True)
    stdout = _Stdout(simulator)

    result, active_at_return = simulator.run(
        lambda: (
            relay(
                connection,
                client=ClientStreams(stdin, stdout),
                threads=threads,
            ),
            threads.active,
        )
    )

    assert result == BridgeResult(
        ChannelEndOutcome.CONNECT_FAILED,
        "relay copy worker start was refused",
    )
    assert threads.gate_condition is not None
    assert threads.gate_condition.notification_attempts == 2
    assert stdin.chunks == [b'{"type":"command.stop"}\n']
    assert connection.sent == b""
    assert connection.receives == 0
    assert stdout.data == b""
    assert connection.close_calls == 1
    assert not active_at_return


def test_interrupted_commit_publication_aborts_before_workers_cross_bytes() -> None:
    """A failed commit broadcast remains unobserved and can become an owned abort."""
    simulator = SimThreads(schedule_seed=0)
    failure = _GateNotificationInterrupted("commit notification interrupted")
    threads = _GateNotifyInterruptedThreads(simulator, "never-refused", failure)
    connection = _ObservedConnection()
    stdin = _Stdin(simulator, [b"pending-command\n"], finished=False)
    stdout = _Stdout(simulator)

    with pytest.raises(_GateNotificationInterrupted) as raised:
        simulator.run(
            lambda: relay(
                connection,
                client=ClientStreams(stdin, stdout),
                threads=threads,
            )
        )

    assert raised.value is failure
    assert threads.gate_condition is not None
    assert threads.gate_condition.notification_attempts == 2
    assert threads.events == [
        "relay-upstream:entered",
        "relay-downstream:entered",
        "relay-upstream:exited",
        "relay-downstream:exited",
    ]
    assert stdin.chunks == [b"pending-command\n"]
    assert connection.sent == b""
    assert connection.receives == 0
    assert stdout.data == b""
    assert connection.close_calls == 1
    assert not threads.active


def test_interrupted_commit_to_supervision_transition_aborts_unobserved_workers() -> None:
    """An exit after commit notification but before the first wait cannot leak workers."""
    simulator = SimThreads(schedule_seed=0)
    failure = _GateNotificationInterrupted("commit-to-supervision interrupted")
    threads = _GateNotifyInterruptedThreads(
        simulator,
        "never-refused",
        failure,
        interrupt_after_notify=True,
    )
    connection = _ObservedConnection()
    stdin = _Stdin(simulator, [b"pending-command\n"], finished=False)
    stdout = _Stdout(simulator)

    with pytest.raises(_GateNotificationInterrupted) as raised:
        simulator.run(
            lambda: relay(
                connection,
                client=ClientStreams(stdin, stdout),
                threads=threads,
            )
        )

    assert raised.value is failure
    assert threads.gate_condition is not None
    assert threads.gate_condition.notification_attempts == 2
    assert threads.events == [
        "relay-upstream:entered",
        "relay-downstream:entered",
        "relay-upstream:exited",
        "relay-downstream:exited",
    ]
    assert stdin.chunks == [b"pending-command\n"]
    assert connection.sent == b""
    assert connection.receives == 0
    assert stdout.data == b""
    assert connection.close_calls == 1
    assert not threads.active


@pytest.mark.parametrize("failed_name", ["relay-upstream", "relay-downstream"])
def test_started_then_interrupted_worker_settles_before_start_failure_propagates(
    failed_name: str,
) -> None:
    """Every ambiguously started worker settles while the exact interruption propagates."""
    simulator = SimThreads(schedule_seed=0)
    failure = _CatastrophicStartError(f"catastrophic {failed_name} start")
    threads = _StartRaisesThreads(simulator, failed_name, failure)
    connection = _ObservedConnection()
    stdin = _Stdin(simulator, [b'{"type":"command.stop"}\n'], finished=True)
    stdout = _Stdout(simulator)
    active_when_raised: list[bool] = []

    def interrupted() -> BridgeResult:
        try:
            return relay(
                connection,
                client=ClientStreams(stdin, stdout),
                threads=threads,
            )
        except BaseException:
            active_when_raised.append(threads.active)
            raise

    with pytest.raises(_CatastrophicStartError) as raised:
        simulator.run(interrupted)

    assert raised.value is failure
    assert str(raised.value) == f"catastrophic {failed_name} start"
    assert threads.events.index(f"{failed_name}:entered") < threads.events.index(
        f"{failed_name}:raised"
    )
    assert stdin.chunks == [b'{"type":"command.stop"}\n']
    assert connection.sent == b""
    assert connection.receives == 0
    assert stdout.data == b""
    assert connection.close_calls == 1
    assert active_when_raised == [False]


def test_cleanup_interruption_does_not_replace_start_failure_or_strand_workers() -> None:
    """An interruption while aborting startup is retried before the original failure escapes."""
    simulator = SimThreads(schedule_seed=0)
    failure = _CatastrophicStartError("downstream start interrupted")
    cleanup_interruption = _CatastrophicStartError("upstream settlement interrupted")
    threads = _StartRaisesThreads(
        simulator,
        "relay-downstream",
        failure,
        settle_name="relay-upstream",
        settle_failure=cleanup_interruption,
    )
    connection = _ObservedConnection()
    stdin = _Stdin(simulator, [b"{}\n"], finished=True)
    active_when_raised: list[bool] = []

    def interrupted() -> BridgeResult:
        try:
            return relay(
                connection,
                client=ClientStreams(stdin, _Stdout(simulator)),
                threads=threads,
            )
        except BaseException:
            active_when_raised.append(threads.active)
            raise

    with pytest.raises(_CatastrophicStartError) as raised:
        simulator.run(interrupted)

    assert raised.value is failure
    assert any(
        "relay startup cleanup was interrupted 1 time(s)" in note for note in raised.value.__notes__
    )
    assert "relay-upstream:settle-interrupted" in threads.events
    assert active_when_raised == [False]
    assert stdin.chunks == [b"{}\n"]
    assert connection.sent == b""
    assert connection.receives == 0
    assert connection.close_calls == 1


def _split(data: bytes, cuts: list[int]) -> list[bytes]:
    points = sorted({cut % (len(data) + 1) for cut in cuts} | {0, len(data)})
    return [data[a:b] for a, b in itertools.pairwise(points) if b > a]


def _receive_exactly(connection: Connection, size: int) -> bytes:
    received = bytearray()
    while len(received) < size:
        chunk = connection.recv(size - len(received), HANG_GUARD_S)
        assert chunk, "the relay closed before the expected bytes"
        received += chunk
    return bytes(received)


_frames = st.lists(
    st.binary(max_size=40).map(lambda body: body.replace(b"\n", b" ") + b"\n"),
    min_size=1,
    max_size=6,
).map(b"".join)
_cuts = st.lists(st.integers(0, 10_000), max_size=8)
_seeds = st.integers(0, 2**16)


@settings(max_examples=examples(pr=10, nightly=40), deadline=None)
@given(_frames, _cuts, _frames, _cuts, _seeds)
def test_bytes_cross_unchanged_both_ways_and_a_client_close_closes_the_socket(
    upstream: bytes, up_cuts: list[int], downstream: bytes, down_cuts: list[int], seed: int
) -> None:
    threads = SimThreads(schedule_seed=seed)
    network = SimNetwork(threads)
    stdin = _Stdin(threads, _split(upstream, up_cuts))
    stdout = _Stdout(threads)
    seen: dict[str, bytes] = {}

    def serve(listener: Listener) -> None:
        connection = listener.accept(HANG_GUARD_S)
        seen["upstream"] = _receive_exactly(connection, len(upstream))
        for chunk in _split(downstream, down_cuts):
            connection.send(chunk)
        stdout.wait_for(len(downstream))
        stdin.finish()
        seen["after_close"] = connection.recv(1, HANG_GUARD_S)

    def main() -> BridgeResult:
        listener = network.listen(_ADDRESS)
        server = threads.spawn(lambda: serve(listener), name="server")
        result = run_bridge(
            network=network, address=_ADDRESS, client=ClientStreams(stdin, stdout), threads=threads
        )
        server.join(HANG_GUARD_S)
        return result

    result = threads.run(main)
    assert result.outcome is ChannelEndOutcome.CLIENT_CLOSED
    assert seen == {"upstream": upstream, "after_close": b""}
    assert bytes(stdout.data) == downstream


@settings(max_examples=examples(pr=10, nightly=40), deadline=None)
@given(_frames, _cuts, _seeds)
def test_a_server_close_is_reported_only_after_its_last_frame_reaches_stdout(
    final: bytes, cuts: list[int], seed: int
) -> None:
    """A protocol error is a frame then a close (WP-PROTOCOL-ERROR); the frame is never lost.

    The client keeps writing after the server closed, so the upstream loop finds the
    socket closed in some schedules before the downstream loop relays the final frame.
    """
    threads = SimThreads(schedule_seed=seed)
    network = SimNetwork(threads)
    stdin = _Stdin(threads, [b'{"type":"command.pause"}\n'] * 4)
    stdout = _Stdout(threads)

    def serve(listener: Listener) -> None:
        connection = listener.accept(HANG_GUARD_S)
        connection.recv(1, HANG_GUARD_S)
        for chunk in _split(final, cuts):
            connection.send(chunk)
        connection.close()

    def main() -> BridgeResult:
        listener = network.listen(_ADDRESS)
        threads.spawn(lambda: serve(listener), name="server")
        return run_bridge(
            network=network, address=_ADDRESS, client=ClientStreams(stdin, stdout), threads=threads
        )

    result = threads.run(main)
    assert result.outcome is ChannelEndOutcome.SERVER_CLOSED
    assert result.exit_status == EXIT_STATUS[ChannelEndOutcome.SERVER_CLOSED]
    assert bytes(stdout.data) == final


def test_a_stdout_that_stops_draining_ends_the_relay_at_the_write_deadline() -> None:
    threads = SimThreads()
    network = SimNetwork(threads)
    stdin = _Stdin(threads, [])
    stdout = _Stdout(threads, stalled=True)
    peer: dict[str, bytes] = {}

    def serve(listener: Listener) -> None:
        connection = listener.accept(HANG_GUARD_S)
        connection.send(b'{"type":"subscribed"}\n')
        peer["after_stall"] = connection.recv(1, HANG_GUARD_S * 4)

    def main() -> tuple[BridgeResult, float]:
        started_at = threads.now()
        listener = network.listen(_ADDRESS)
        server = threads.spawn(lambda: serve(listener), name="server")
        result = run_bridge(
            network=network, address=_ADDRESS, client=ClientStreams(stdin, stdout), threads=threads
        )
        ended_at = threads.now() - started_at
        server.join(HANG_GUARD_S)
        return result, ended_at

    result, ended_at = threads.run(main)
    assert result.outcome is ChannelEndOutcome.CLIENT_STALLED
    assert ended_at == pytest.approx(_DEADLINE)
    # The server sees the client gone instead of being pinned behind it.
    assert peer == {"after_stall": b""}


class _UnreadSocket:
    """A network whose connections never finish a send, like a server that stopped reading."""

    def __init__(self, threads: SimThreads, network: SimNetwork) -> None:
        self._threads = threads
        self._network = network

    def connect(self, address: str, timeout: float | None = None) -> Connection:
        return _NeverSends(self._threads, self._network.connect(address, timeout))


class _NeverSends:
    def __init__(self, threads: SimThreads, inner: Connection) -> None:
        self._cond = threads.condition()
        self._inner = inner
        self._closed = False
        self.unsent = b""

    def send(self, data: bytes, timeout: float | None = None) -> None:
        del timeout  # this fake holds every send until close
        with self._cond:
            self.unsent = data
            self._cond.wait_for(lambda: self._closed)
            raise BrokenPipeError(errno.EPIPE, "closed while stalled")

    def recv(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._inner.recv(max_bytes, timeout)

    def peer_closed(self) -> bool:
        return self._inner.peer_closed()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()
        self._inner.close()


def test_a_server_that_stops_reading_ends_the_relay_at_the_write_deadline() -> None:
    threads = SimThreads()
    network = SimNetwork(threads)
    stdin = _Stdin(threads, [b'{"type":"command.pause"}\n'])
    stdout = _Stdout(threads)

    def main() -> tuple[BridgeResult, float]:
        started_at = threads.now()
        listener = network.listen(_ADDRESS)
        result = run_bridge(
            network=_UnreadSocket(threads, network),
            address=_ADDRESS,
            client=ClientStreams(stdin, stdout),
            threads=threads,
        )
        listener.close()
        return result, threads.now() - started_at

    result, ended_at = threads.run(main)
    assert result.outcome is ChannelEndOutcome.SERVER_STALLED
    assert ended_at == pytest.approx(_DEADLINE)
    assert "40s" in result.detail
    assert result.exit_status == EXIT_STATUS[ChannelEndOutcome.SERVER_STALLED]


def test_a_server_that_goes_silent_on_read_is_unresponsive_within_the_read_deadline() -> None:
    """A server that never sends, but never closes or stalls a write either, is unresponsive.

    The client's one chunk still reaches the socket because the simulated network's
    buffer is unbounded, so the write side makes real progress while the server-to-client
    read stays silent: this reaches ``SERVER_UNRESPONSIVE`` through the read deadline
    alone, not through a write stall.
    """
    threads = SimThreads()
    network = SimNetwork(threads)
    limits = BridgeLimits(read_deadline_seconds=5.0)
    stdin = _Stdin(threads, [b'{"type":"command.pause"}\n'])
    stdout = _Stdout(threads)

    def main() -> tuple[BridgeResult, float]:
        started_at = threads.now()
        listener = network.listen(_ADDRESS)
        result = run_bridge(
            network=network,
            address=_ADDRESS,
            client=ClientStreams(stdin, stdout),
            threads=threads,
            limits=limits,
        )
        listener.close()
        return result, threads.now() - started_at

    result, ended_at = threads.run(main)
    assert result.outcome is ChannelEndOutcome.SERVER_UNRESPONSIVE
    assert ended_at == pytest.approx(limits.read_deadline_seconds)
    assert result.exit_status == EXIT_STATUS[ChannelEndOutcome.SERVER_UNRESPONSIVE]
    # Regression for the formatting bug: the detail names the deadline that actually
    # fired (the read one) rather than always citing ``write_deadline_seconds``.
    assert "5s" in result.detail
    assert "40s" not in result.detail
    assert stdout.data == b""


def test_a_stdin_failure_is_a_client_failure() -> None:
    threads = SimThreads()
    network = SimNetwork(threads)
    stdin = _Stdin(threads, [], error=OSError(errno.EIO, "input/output error"), finished=True)

    def main() -> BridgeResult:
        listener = network.listen(_ADDRESS)
        try:
            return run_bridge(
                network=network,
                address=_ADDRESS,
                client=ClientStreams(stdin, _Stdout(threads)),
                threads=threads,
            )
        finally:
            listener.close()

    assert threads.run(main).outcome is ChannelEndOutcome.CLIENT_FAILED


class _RefusingNetwork:
    def __init__(self, error: OSError) -> None:
        self._error = error
        self.dials: list[tuple[str, float | None]] = []

    def connect(self, address: str, timeout: float | None = None) -> Connection:
        self.dials.append((address, timeout))
        raise self._error


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (ConnectionRefusedError(errno.ECONNREFUSED, "nothing listens"), ChannelEndOutcome.RUN_GONE),
        (FileNotFoundError(errno.ENOENT, "no such file"), ChannelEndOutcome.RUN_GONE),
        (PermissionError(errno.EACCES, "permission denied"), ChannelEndOutcome.CONNECT_DENIED),
        (TimeoutError("timed out"), ChannelEndOutcome.CONNECT_FAILED),
        (OSError(errno.ENOTSOCK, "not a socket"), ChannelEndOutcome.CONNECT_FAILED),
    ],
)
def test_a_failed_connect_names_why_without_relaying(
    error: OSError, outcome: ChannelEndOutcome
) -> None:
    threads = SimThreads()
    stdin = _Stdin(threads, [b"never read\n"])
    network = _RefusingNetwork(error)
    result = threads.run(
        lambda: run_bridge(
            network=network,
            address=_ADDRESS,
            client=ClientStreams(stdin, _Stdout(threads)),
            threads=threads,
        )
    )
    assert result.outcome is outcome
    assert _ADDRESS in result.detail
    assert stdin.chunks == [b"never read\n"]
    assert network.dials == [(_ADDRESS, _LIMITS.connect_timeout_seconds)]


def test_dialing_an_unbound_address_on_the_simulated_network_is_run_gone() -> None:
    threads = SimThreads()
    network = SimNetwork(threads)
    result = threads.run(
        lambda: run_bridge(
            network=network,
            address=_ADDRESS,
            client=ClientStreams(_Stdin(threads, []), _Stdout(threads)),
            threads=threads,
        )
    )
    assert result.outcome is ChannelEndOutcome.RUN_GONE
