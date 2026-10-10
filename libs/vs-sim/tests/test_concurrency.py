"""Threads and Network: one contract for the real and the simulated implementations."""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from vs_sim.api import OsThreads, UnixNetwork
from vs_sim.api.testing import (
    HANG_GUARD_S,
    NetworkContract,
    NetworkUnderTest,
    SimNetwork,
    SimThreads,
    ThreadsContract,
    ThreadsUnderTest,
    join_or_fail,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api import Connection

_REAL_TICK_S = 0.001


def _read_outcome(connection: Connection) -> bytes | type[OSError]:
    """What one ``recv`` on *connection* ends with: its bytes, or ``OSError`` if closed."""
    try:
        return connection.recv(1)
    except OSError:
        return OSError


class TestOsThreads(ThreadsContract):
    def threads_under_test(self, seed: int) -> ThreadsUnderTest:
        del seed  # a real implementation has one interleaving
        return ThreadsUnderTest(
            OsThreads(), lambda program: program(), tick=_REAL_TICK_S, exact=False
        )


class TestSimThreadsFifo(ThreadsContract):
    def threads_under_test(self, seed: int) -> ThreadsUnderTest:
        del seed  # first-in first-out has one interleaving
        sim = SimThreads()
        return ThreadsUnderTest(sim, sim.run, tick=1.0, exact=True)


class TestSimThreadsSeeded(ThreadsContract):
    def threads_under_test(self, seed: int) -> ThreadsUnderTest:
        sim = SimThreads(schedule_seed=seed)
        return ThreadsUnderTest(sim, sim.run, tick=1.0, exact=True)


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

    def test_closing_a_connection_ends_a_read_blocked_in_another_thread(self) -> None:
        # The reader may reach recv before or after close; nothing here can observe
        # which. Either way the read must end: with end of stream if it was blocked,
        # or with the contract's OSError if this end was already closed.
        network = UnixNetwork()
        address = _unix_paths()("blocked")
        listener = network.listen(address)
        client = network.connect(address, HANG_GUARD_S)
        server = listener.accept(HANG_GUARD_S)
        outcomes: list[bytes | type[OSError]] = []
        reader = threading.Thread(target=lambda: outcomes.append(_read_outcome(server)))
        reader.start()
        server.close()
        join_or_fail(reader)
        assert outcomes in ([b""], [OSError])
        assert client.recv(1, HANG_GUARD_S) == b""
        client.close()
        listener.close()

    def test_a_read_after_close_raises_instead_of_hanging(self) -> None:
        network = UnixNetwork()
        address = _unix_paths()("closed-first")
        listener = network.listen(address)
        client = network.connect(address, HANG_GUARD_S)
        server = listener.accept(HANG_GUARD_S)
        server.close()
        assert _read_outcome(server) is OSError
        client.close()
        listener.close()

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
            SimNetwork(sim), ThreadsUnderTest(sim, sim.run, tick=1.0, exact=True), lambda n: n
        )


class TestSimNetworkSeeded(NetworkContract):
    def network_under_test(self, seed: int) -> NetworkUnderTest:
        sim = SimThreads(schedule_seed=seed)
        return NetworkUnderTest(
            SimNetwork(sim), ThreadsUnderTest(sim, sim.run, tick=1.0, exact=True), lambda n: n
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
