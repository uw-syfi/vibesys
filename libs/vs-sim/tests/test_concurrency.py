"""Threads and Network: one contract for the real and the simulated implementations."""

from __future__ import annotations

import errno
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_sim.api import OsThreads, UnixNetwork, WorkerStartRefusedError
from vs_sim.api.testing import (
    HANG_GUARD_S,
    NetworkContract,
    NetworkUnderTest,
    RefusingReservableThreads,
    RefusingThreads,
    ReservableThreadsContract,
    ReservableThreadsUnderTest,
    SimNetwork,
    SimThreads,
    ThreadsContract,
    ThreadsUnderTest,
    join_or_fail,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_REAL_TICK_S = 0.001
#: A socket-sized buffer, so the simulated network blocks a sender as a real one does.


class TestOsThreads(ThreadsContract, ReservableThreadsContract):
    def threads_under_test(self, seed: int) -> ThreadsUnderTest:
        del seed  # a real implementation has one interleaving
        return ThreadsUnderTest(
            OsThreads(), lambda program: program(), tick=_REAL_TICK_S, exact=False
        )

    def reservable_threads_under_test(self, seed: int) -> ReservableThreadsUnderTest:
        del seed
        return ReservableThreadsUnderTest(OsThreads(), lambda program: program())


class TestSimThreadsFifo(ThreadsContract, ReservableThreadsContract):
    def threads_under_test(self, seed: int) -> ThreadsUnderTest:
        del seed  # first-in first-out has one interleaving
        sim = SimThreads()
        return ThreadsUnderTest(sim, sim.run, tick=1.0, exact=True)

    def reservable_threads_under_test(self, seed: int) -> ReservableThreadsUnderTest:
        del seed
        sim = SimThreads()
        return ReservableThreadsUnderTest(sim, sim.run)


class TestSimThreadsSeeded(ThreadsContract, ReservableThreadsContract):
    def threads_under_test(self, seed: int) -> ThreadsUnderTest:
        sim = SimThreads(schedule_seed=seed)
        return ThreadsUnderTest(sim, sim.run, tick=1.0, exact=True)

    def reservable_threads_under_test(self, seed: int) -> ReservableThreadsUnderTest:
        sim = SimThreads(schedule_seed=seed)
        return ReservableThreadsUnderTest(sim, sim.run)


class TestRefusingThreads(ThreadsContract):
    def threads_under_test(self, seed: int) -> ThreadsUnderTest:
        sim = SimThreads(schedule_seed=seed)
        return ThreadsUnderTest(RefusingThreads(sim, {}), sim.run, tick=1.0, exact=True)


class TestRefusingReservableThreads(ThreadsContract, ReservableThreadsContract):
    def threads_under_test(self, seed: int) -> ThreadsUnderTest:
        sim = SimThreads(schedule_seed=seed)
        return ThreadsUnderTest(RefusingReservableThreads(sim, {}), sim.run, tick=1.0, exact=True)

    def reservable_threads_under_test(self, seed: int) -> ReservableThreadsUnderTest:
        sim = SimThreads(schedule_seed=seed)
        return ReservableThreadsUnderTest(RefusingReservableThreads(sim, {}), sim.run)


def test_refusing_threads_consumes_only_the_scripted_starts() -> None:
    sim = SimThreads()
    threads = RefusingThreads(sim, {"client": 1})
    ran: list[str] = []

    def main() -> None:
        with pytest.raises(WorkerStartRefusedError, match="client"):
            threads.spawn(lambda: ran.append("refused"), name="client")
        threads.spawn(lambda: ran.append("started"), name="client")

    sim.run(main)
    assert ran == ["started"]


def test_refusing_reservable_threads_refuses_a_start_without_running_its_target() -> None:
    sim = SimThreads()
    threads = RefusingReservableThreads(sim, {"client": 1})
    ran: list[str] = []

    def main() -> None:
        refused = threads.reserve(lambda: ran.append("refused"), name="client")
        with pytest.raises(WorkerStartRefusedError, match="client"):
            refused.start()
        with pytest.raises(RuntimeError, match="cannot be started"):
            refused.start()
        refused.settle()
        started = threads.reserve(lambda: ran.append("started"), name="client")
        started.start()
        started.settle()

    sim.run(main)
    assert ran == ["started"]


def _outcome_of(call: Callable[[], object]) -> object:
    try:
        return call()
    except (OSError, ValueError) as error:
        return error


def _is_raw_bad_descriptor(outcome: object) -> bool:
    # The network reports a closed connection as "connection closed"; the system's own EBADF
    # means a descriptor was closed under a call that was still using it.
    return (
        isinstance(outcome, OSError)
        and outcome.errno == errno.EBADF
        and outcome.strerror != "connection closed"
    )


def _unix_paths() -> Callable[[str], str]:
    # Unix socket paths are short-limited, so the directory lives directly under the system
    # temporary directory rather than under pytest's long per-test path.
    root = Path(tempfile.mkdtemp(prefix="vs-sim-"))
    return lambda name: str(root / name)


class TestUnixNetwork(NetworkContract):
    def network_under_test(self, seed: int) -> NetworkUnderTest:
        del seed  # a real implementation has one interleaving
        address = _unix_paths()
        threads = ThreadsUnderTest(
            OsThreads(), lambda program: program(), tick=_REAL_TICK_S, exact=False
        )
        return NetworkUnderTest(UnixNetwork(), threads, address)

    def test_concurrent_connection_close_ends_read_with_eof_or_error(self) -> None:
        network = UnixNetwork()
        address = _unix_paths()("blocked")
        listener = network.listen(address)
        client = network.connect(address, HANG_GUARD_S)
        server = listener.accept(HANG_GUARD_S)
        outcomes: list[bytes | OSError] = []

        def read_until_closed() -> None:
            try:
                outcomes.append(server.recv(1))
            except OSError as error:
                outcomes.append(error)

        reader = threading.Thread(target=read_until_closed)
        reader.start()
        server.close()
        join_or_fail(reader)
        assert len(outcomes) == 1
        assert outcomes[0] == b"" or isinstance(outcomes[0], OSError)
        assert client.recv(1, HANG_GUARD_S) == b""
        client.close()
        listener.close()

    def test_closing_with_calls_in_flight_closes_the_descriptor_after_the_last_call(self) -> None:
        """Calls waiting on a closed connection see its shutdown, and the descriptor is released.

        The first call to leave closing the descriptor under the others hands them the system's
        ``EBADF`` where they should see EOF or a broken pipe, and a descriptor nobody closes
        stays in ``/dev/fd`` for good.
        """
        network = UnixNetwork()
        paths = _unix_paths()
        pause = OsThreads()
        big = b"x" * (8 << 20)

        def one_round(n: int) -> list[object]:
            listener = network.listen(paths(f"round{n}"))
            client = network.connect(paths(f"round{n}"), HANG_GUARD_S)
            peer = listener.accept(HANG_GUARD_S)
            listener.close()
            outcomes: list[object] = []
            calls = [lambda: client.recv(1)] * 3 + [lambda: client.send(big)] * 3
            workers = [
                threading.Thread(target=lambda call=call: outcomes.append(_outcome_of(call)))
                for call in calls
            ]
            for worker in workers:
                worker.start()
            pause.sleep(
                10 * _REAL_TICK_S
            )  # Let the calls park; the outcomes hold if they have not.
            closers = [threading.Thread(target=client.close) for _ in range(2)]
            for closer in closers:
                closer.start()
            try:
                for thread in (*workers, *closers):
                    join_or_fail(thread)
            finally:
                client.close()
                peer.close()
            return outcomes

        def open_descriptors() -> int:
            return len(list(Path("/dev/fd").iterdir()))

        one_round(0)  # Opens whatever the process opens once.
        before = open_descriptors()
        outcomes = [outcome for n in range(1, 21) for outcome in one_round(n)]
        assert open_descriptors() == before
        assert all(outcome == b"" or isinstance(outcome, OSError) for outcome in outcomes)
        assert not [outcome for outcome in outcomes if _is_raw_bad_descriptor(outcome)]

    def test_closing_a_listener_leaves_a_socket_file_a_later_listener_replaced(self) -> None:
        path = _unix_paths()("replaced")
        first = UnixNetwork().listen(path)
        Path(path).unlink()
        second = UnixNetwork().listen(path)
        first.close()
        assert Path(path).exists()
        second.close()
        assert not Path(path).exists()


class TestSimNetworkFifo(NetworkContract):
    def network_under_test(self, seed: int) -> NetworkUnderTest:
        del seed  # first-in first-out has one interleaving
        sim = SimThreads()
        return NetworkUnderTest(
            SimNetwork(sim),
            ThreadsUnderTest(sim, sim.run, tick=1.0, exact=True),
            lambda n: n,
        )


class TestSimNetworkSeeded(NetworkContract):
    def network_under_test(self, seed: int) -> NetworkUnderTest:
        sim = SimThreads(schedule_seed=seed)
        return NetworkUnderTest(
            SimNetwork(sim),
            ThreadsUnderTest(sim, sim.run, tick=1.0, exact=True),
            lambda n: n,
        )


class TestSimNetworkOverOsThreads(NetworkContract):
    """The in-memory network also works for real threads."""

    def network_under_test(self, seed: int) -> NetworkUnderTest:
        del seed  # real threads have one interleaving
        threads = OsThreads()
        return NetworkUnderTest(
            SimNetwork(threads),
            ThreadsUnderTest(threads, lambda program: program(), tick=_REAL_TICK_S, exact=False),
            lambda n: n,
        )
