"""Contract suites for :class:`~vs_sim.concurrency.Threads` and :class:`~vs_sim.network.Network`.

Subclass a suite, name the subclass ``Test<Variant>`` and implement the one factory. The
factory receives the case's seed: a simulator uses it as the schedule seed, so the
property cases below explore one interleaving per seed; a real implementation ignores it.
Every case drives the implementation through its interface only, bounds each wait by
:data:`~vs_sim.waits.HANG_GUARD_S` (a hang guard, never what a case synchronizes on) and
asserts outcomes, never durations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vs_sim.randomness import SeededRandom
from vs_sim.waits import HANG_GUARD_S

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.concurrency import Event, Threads, Worker
    from vs_sim.network import Connection, Network

_CASES = 12
"""Seeded cases per property: the same draws every run."""

type Program = Callable[[], Any]


@dataclass(frozen=True)
class ThreadsUnderTest:
    """A :class:`Threads` and the means to run a program on it."""

    threads: Threads
    run: Callable[[Program], Any]
    """Run the function as the program's first thread and return its result."""
    tick: float
    """A short duration, cheap on this implementation's timeline."""
    exact: bool
    """Whether ``sleep(d)`` moves ``now`` by exactly ``d`` and sleeps overlap."""


def _join(worker: Worker) -> None:
    worker.join(HANG_GUARD_S)
    if worker.is_alive():
        message = f"{worker.name} was still running after {HANG_GUARD_S:g} s"
        raise AssertionError(message)


def _wait(event: Event, what: str) -> None:
    if not event.wait(HANG_GUARD_S):
        message = f"{what} did not happen within {HANG_GUARD_S:g} s"
        raise AssertionError(message)


class ThreadsContract:
    """Cases for :class:`~vs_sim.concurrency.Threads`. Implement :meth:`threads_under_test`."""

    def threads_under_test(self, seed: int) -> ThreadsUnderTest:
        """A fresh implementation with its harness; ``seed`` picks the interleaving of a simulator."""
        raise NotImplementedError

    def test_a_spawned_target_runs_once_and_join_waits_for_it(self) -> None:
        """Every spawned target runs exactly once; after ``join`` each worker is finished."""
        for seed in range(_CASES):
            subject = self.threads_under_test(seed)
            count = SeededRandom(seed).randint(1, 6)

            def program(subject: ThreadsUnderTest = subject, count: int = count) -> list[int]:
                ran: list[int] = []
                workers = [
                    subject.threads.spawn(lambda i=i: ran.append(i), name=f"w{i}")
                    for i in range(count)
                ]
                for worker in workers:
                    _join(worker)
                assert not any(worker.is_alive() for worker in workers)
                return ran

            assert sorted(subject.run(program)) == list(range(count)), seed

    def test_a_worker_keeps_its_name(self) -> None:
        """The handle reports the name the thread was spawned with."""
        subject = self.threads_under_test(0)

        def program() -> str:
            worker = subject.threads.spawn(lambda: None, name="named-worker")
            _join(worker)
            return worker.name

        assert subject.run(program) == "named-worker"

    def test_join_with_a_timeout_returns_while_the_worker_still_runs(self) -> None:
        """A timed-out ``join`` returns with the worker alive; it ends once released."""
        subject = self.threads_under_test(0)

        def program() -> tuple[bool, bool]:
            threads = subject.threads
            release = threads.event()
            worker = threads.spawn(lambda: release.wait(HANG_GUARD_S), name="parked")
            worker.join(subject.tick)
            alive_after_timeout = worker.is_alive()
            release.set()
            _join(worker)
            return alive_after_timeout, worker.is_alive()

        assert subject.run(program) == (True, False)

    def test_a_lock_gives_mutual_exclusion(self) -> None:
        """Workers that sleep inside the critical section never overlap there, and lose no update."""
        for seed in range(_CASES):
            rng = SeededRandom(seed)
            subject = self.threads_under_test(seed)
            workers_count, rounds = rng.randint(2, 4), rng.randint(1, 3)

            def program(
                subject: ThreadsUnderTest = subject,
                workers_count: int = workers_count,
                rounds: int = rounds,
            ) -> tuple[int, int]:
                threads = subject.threads
                lock = threads.lock()
                state = {"inside": 0, "most": 0, "total": 0}

                def work() -> None:
                    for _ in range(rounds):
                        with lock:
                            state["inside"] += 1
                            state["most"] = max(state["most"], state["inside"])
                            seen = state["total"]
                            threads.sleep(subject.tick)
                            state["total"] = seen + 1
                            state["inside"] -= 1

                for worker in [threads.spawn(work, name=f"w{i}") for i in range(workers_count)]:
                    _join(worker)
                return state["most"], state["total"]

            assert subject.run(program) == (1, workers_count * rounds), seed

    def test_a_held_lock_refuses_a_non_blocking_or_timed_acquire(self) -> None:
        """While another thread holds the lock, ``acquire`` without waiting or with a timeout fails."""
        subject = self.threads_under_test(0)

        def program() -> tuple[bool, bool, bool]:
            threads = subject.threads
            lock, held, release = threads.lock(), threads.event(), threads.event()

            def holder() -> None:
                with lock:
                    held.set()
                    release.wait(HANG_GUARD_S)

            worker = threads.spawn(holder, name="holder")
            _wait(held, "the holder taking the lock")
            refused = (lock.acquire(blocking=False), lock.acquire(timeout=subject.tick))
            release.set()
            _join(worker)
            return (*refused, lock.acquire(blocking=False))

        assert subject.run(program) == (False, False, True)

    def test_only_a_reentrant_lock_can_be_taken_twice_by_its_owner(self) -> None:
        """The owner of an ``rlock`` re-acquires it; the owner of a ``lock`` does not."""
        subject = self.threads_under_test(0)

        def program() -> tuple[bool, bool]:
            plain, reentrant = subject.threads.lock(), subject.threads.rlock()
            with plain:
                second_plain = plain.acquire(blocking=False)
            with reentrant:
                second_reentrant = reentrant.acquire(blocking=False)
                if second_reentrant:
                    reentrant.release()
            return second_plain, second_reentrant

        assert subject.run(program) == (False, True)

    def test_releasing_a_lock_that_is_not_held_is_an_error(self) -> None:
        """``release`` without ``acquire`` raises ``RuntimeError``."""
        subject = self.threads_under_test(0)

        def program() -> None:
            subject.threads.lock().release()

        try:
            subject.run(program)
        except RuntimeError:
            return
        message = "releasing an unheld lock did not raise"
        raise AssertionError(message)

    def test_an_event_wakes_every_waiter_and_times_out_when_unset(self) -> None:
        """``set`` releases all waiters; ``wait`` on a cleared event returns ``False`` after its timeout."""
        for seed in range(_CASES):
            subject = self.threads_under_test(seed)
            count = SeededRandom(seed).randint(1, 5)

            def program(
                subject: ThreadsUnderTest = subject, count: int = count
            ) -> tuple[bool, list[bool], bool]:
                threads = subject.threads
                event = threads.event()
                unset_wait = event.wait(subject.tick)
                woke: list[bool] = []
                workers = [
                    threads.spawn(lambda: woke.append(event.wait(HANG_GUARD_S)), name=f"w{i}")
                    for i in range(count)
                ]
                event.set()
                for worker in workers:
                    _join(worker)
                event.clear()
                return unset_wait, woke, event.wait(subject.tick)

            assert subject.run(program) == (False, [True] * count, False), seed

    def test_a_condition_hands_items_over_without_losing_a_wakeup(self) -> None:
        """A consumer waiting on a predicate receives every item the producer adds, in order."""
        for seed in range(_CASES):
            subject = self.threads_under_test(seed)
            count = SeededRandom(seed).randint(1, 6)

            def program(subject: ThreadsUnderTest = subject, count: int = count) -> list[int]:
                threads = subject.threads
                cond = threads.condition()
                items: list[int] = []
                received: list[int] = []

                def consume() -> None:
                    while len(received) < count:
                        with cond:
                            assert cond.wait_for(lambda: bool(items), HANG_GUARD_S)
                            received.append(items.pop(0))

                consumer = threads.spawn(consume, name="consumer")
                for value in range(count):
                    with cond:
                        items.append(value)
                        cond.notify()
                    threads.sleep(0)
                _join(consumer)
                return received

            assert subject.run(program) == list(range(count)), seed

    def test_notify_one_per_ticket_lets_every_waiter_through(self) -> None:
        """With one ``notify`` per ticket, every waiter eventually takes a ticket."""
        for seed in range(_CASES):
            subject = self.threads_under_test(seed)
            count = SeededRandom(seed).randint(1, 5)

            def program(subject: ThreadsUnderTest = subject, count: int = count) -> int:
                threads = subject.threads
                cond = threads.condition()
                tickets = {"free": 0, "taken": 0}

                def take() -> None:
                    with cond:
                        assert cond.wait_for(lambda: tickets["free"] > 0, HANG_GUARD_S)
                        tickets["free"] -= 1
                        tickets["taken"] += 1

                workers = [threads.spawn(take, name=f"w{i}") for i in range(count)]
                for _ in range(count):
                    with cond:
                        tickets["free"] += 1
                        cond.notify()
                for worker in workers:
                    _join(worker)
                return tickets["taken"]

            assert subject.run(program) == count, seed

    def test_a_condition_wait_times_out_and_keeps_the_lock_contract(self) -> None:
        """A timed wait nobody notifies returns ``False`` with the lock held again; unheld use raises."""
        subject = self.threads_under_test(0)

        def program() -> tuple[bool, bool, bool]:
            cond = subject.threads.condition()
            with cond:
                timed_out = cond.wait(subject.tick)
                reacquired = cond.acquire(blocking=False)
                if reacquired:
                    cond.release()
            errors = []
            for operation in (cond.wait, cond.notify, cond.notify_all):
                try:
                    operation()
                except RuntimeError:
                    errors.append(True)
            return timed_out, reacquired, len(errors) == 3

        assert subject.run(program) == (False, True, True)

    def test_sleep_returns_and_the_clock_never_runs_backwards(self) -> None:
        """Readings of ``now`` around sleeps are in order; on an exact clock a sleep adds its duration."""
        for seed in range(_CASES):
            subject = self.threads_under_test(seed)
            durations = [SeededRandom(seed).random() * subject.tick for _ in range(3)]

            def program(
                subject: ThreadsUnderTest = subject, durations: list[float] = durations
            ) -> list[float]:
                readings = [subject.threads.now()]
                for seconds in durations:
                    subject.threads.sleep(seconds)
                    readings.append(subject.threads.now())
                return readings

            readings = subject.run(program)
            assert readings == sorted(readings), seed
            if subject.exact:
                assert abs(readings[-1] - readings[0] - sum(durations)) < 1e-9, seed

    def test_concurrent_sleeps_overlap_on_an_exact_clock(self) -> None:
        """Workers that sleep at the same time take as long as one, not the sum."""
        subject = self.threads_under_test(0)
        if not subject.exact:
            return

        def program() -> float:
            threads = subject.threads
            start = threads.now()
            workers = [threads.spawn(lambda: threads.sleep(5.0), name=f"w{i}") for i in range(4)]
            for worker in workers:
                _join(worker)
            return threads.now() - start

        assert abs(subject.run(program) - 5.0) < 1e-9


@dataclass(frozen=True)
class NetworkUnderTest:
    """A network, the threads it blocks with, and a source of bindable addresses."""

    network: Network
    threads: ThreadsUnderTest
    address: Callable[[str], str]
    """An address this network can bind, for a short name."""


def _recv_exact(connection: Connection, count: int) -> bytes:
    received = bytearray()
    while len(received) < count:
        chunk = connection.recv(count - len(received), HANG_GUARD_S)
        if not chunk:
            break
        received += chunk
    return bytes(received)


class NetworkContract:
    """Cases for :class:`~vs_sim.network.Network`. Implement :meth:`network_under_test`."""

    def network_under_test(self, seed: int) -> NetworkUnderTest:
        """A fresh network with its harness."""
        raise NotImplementedError

    def test_bytes_arrive_whole_and_in_order(self) -> None:
        """A server echoes whatever a client sends, however the stream is chunked."""
        for seed in range(_CASES):
            rng = SeededRandom(seed)
            subject = self.network_under_test(seed)
            chunks = [
                bytes(rng.randint(0, 255) for _ in range(rng.randint(1, 9)))
                for _ in range(rng.randint(1, 5))
            ]
            total = sum(len(chunk) for chunk in chunks)

            def program(
                subject: NetworkUnderTest = subject,
                chunks: list[bytes] = chunks,
                total: int = total,
            ) -> bytes:
                threads = subject.threads.threads
                listener = subject.network.listen(subject.address("echo"))

                def serve() -> None:
                    connection = listener.accept(HANG_GUARD_S)
                    connection.send(_recv_exact(connection, total))
                    connection.close()

                server = threads.spawn(serve, name="server")
                client = subject.network.connect(subject.address("echo"), HANG_GUARD_S)
                for chunk in chunks:
                    client.send(chunk)
                echoed = _recv_exact(client, total)
                _wait_closed(client)
                client.close()
                _join(server)
                listener.close()
                return echoed

            assert subject.threads.run(program) == b"".join(chunks), seed

    def test_dialing_an_address_nobody_listens_on_is_refused(self) -> None:
        """``connect`` to an unbound address raises ``ConnectionRefusedError``."""
        subject = self.network_under_test(0)

        def program() -> None:
            subject.network.connect(subject.address("nobody"), subject.threads.tick)

        try:
            subject.threads.run(program)
        except ConnectionRefusedError:
            return
        message = "connect to an unbound address did not raise ConnectionRefusedError"
        raise AssertionError(message)

    def test_an_address_in_use_cannot_be_bound_until_its_listener_closes(self) -> None:
        """A second ``listen`` on a live address raises ``OSError``; after ``close`` it works."""
        subject = self.network_under_test(0)

        def program() -> tuple[bool, bool]:
            address = subject.address("busy")
            first = subject.network.listen(address)
            try:
                subject.network.listen(address)
            except OSError:
                refused = True
            else:
                refused = False
            first.close()
            first.close()
            subject.network.listen(address).close()
            return refused, True

        assert subject.threads.run(program) == (True, True)

    def test_accept_and_recv_time_out_and_a_closed_listener_refuses_accept(self) -> None:
        """Waiting for a client or for bytes that never come raises ``TimeoutError``."""
        subject = self.network_under_test(0)

        def program() -> tuple[bool, bool, bool]:
            address = subject.address("quiet")
            listener = subject.network.listen(address)
            try:
                listener.accept(subject.threads.tick)
            except TimeoutError:
                accept_timed_out = True
            else:
                accept_timed_out = False
            client = subject.network.connect(address, HANG_GUARD_S)
            server_side = listener.accept(HANG_GUARD_S)
            try:
                client.recv(1, subject.threads.tick)
            except TimeoutError:
                recv_timed_out = True
            else:
                recv_timed_out = False
            server_side.close()
            client.close()
            listener.close()
            try:
                listener.accept(subject.threads.tick)
            except OSError:
                closed_refuses = True
            else:
                closed_refuses = False
            return accept_timed_out, recv_timed_out, closed_refuses

        assert subject.threads.run(program) == (True, True, True)

    def test_a_peer_that_closed_leaves_its_data_readable_and_then_reads_eof(self) -> None:
        """``peer_closed`` stays false while data is unread; EOF (``b""``) follows the last byte."""
        subject = self.network_under_test(0)

        def program() -> tuple[bool, bool, bytes, bytes, bool]:
            threads = subject.threads.threads
            address = subject.address("bye")
            listener = subject.network.listen(address)
            sent = threads.event()

            def serve() -> None:
                connection = listener.accept(HANG_GUARD_S)
                connection.send(b"last words")
                connection.close()
                sent.set()

            server = threads.spawn(serve, name="server")
            client = subject.network.connect(address, HANG_GUARD_S)
            _wait(sent, "the server closing")
            open_before = client.peer_closed()
            data = _recv_exact(client, len(b"last words"))
            eof = client.recv(1, HANG_GUARD_S)
            closed_after = client.peer_closed()
            _join(server)
            client.close()
            listener.close()
            return open_before, closed_after, data, eof, True

        open_before, closed_after, data, eof, _ = subject.threads.run(program)
        assert (open_before, closed_after, data, eof) == (False, True, b"last words", b"")

    def test_sending_to_a_closed_peer_eventually_fails(self) -> None:
        """Writes to a hung-up peer raise ``OSError`` (broken pipe or reset) within a few attempts."""
        subject = self.network_under_test(0)

        def program() -> bool:
            threads = subject.threads.threads
            address = subject.address("gone")
            listener = subject.network.listen(address)
            hung_up = threads.event()

            def serve() -> None:
                listener.accept(HANG_GUARD_S).close()
                hung_up.set()

            server = threads.spawn(serve, name="server")
            client = subject.network.connect(address, HANG_GUARD_S)
            _wait(hung_up, "the server hanging up")
            failed = False
            for _ in range(64):
                try:
                    client.send(b"x" * 1024)
                except OSError:
                    failed = True
                    break
            _join(server)
            client.close()
            listener.close()
            return failed

        assert subject.threads.run(program)


def _wait_closed(connection: Connection) -> None:
    """Read until the peer's EOF, so a case ends only when the peer has finished."""
    while connection.recv(1, HANG_GUARD_S):
        pass
