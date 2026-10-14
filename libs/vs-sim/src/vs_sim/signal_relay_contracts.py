"""Contract suites for :class:`~vs_sim.signal_relay.SignalRelay` and ``MainThreadWake``.

Subclass :class:`SignalRelayContract`, name the subclass ``Test<Variant>`` and implement
:meth:`~SignalRelayContract.subject`. The real ``WakeupFdSignalRelay`` (in ``tests/e2e``,
in a child process: delivery changes process-wide state) and ``FakeSignalRelay`` pass the
same cases. Cases wait on what they observe, never on a duration.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_sim.waits import HANG_GUARD_S

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.concurrency import Event, Threads
    from vs_sim.signal_relay import MainThreadWake, SignalRelay


@dataclass(frozen=True)
class RelayUnderTest:
    """One implementation with the way a test makes a signal arrive."""

    relay: SignalRelay
    numbers: tuple[int, int]
    """Two distinct signal numbers the relay can watch."""
    deliver: Callable[[int], object]
    threads: Threads
    """Provides the events the cases wait on."""
    run: Callable[[Callable[[], None]], None]
    """Run a case body (in a child process when delivery changes process-wide state)."""
    wait: Callable[[Event, str], None]
    """Wait for an event a signal sets, failing with the message if it never is (a hang guard)."""


class SignalRelayContract:
    """Cases for :class:`~vs_sim.signal_relay.SignalRelay`."""

    def subject(self) -> RelayUnderTest:
        """A fresh relay with its harness."""
        raise NotImplementedError

    def _case(self, case: Callable[[RelayUnderTest], None]) -> None:
        self.subject().run(lambda: case(self.subject()))

    def test_a_watched_signal_reaches_the_callback_with_its_number(self) -> None:
        """Each watched signal that arrives calls ``on_signal`` with its number."""

        def case(subject: RelayUnderTest) -> None:
            first, second = subject.numbers
            arrived = subject.threads.event()
            received: list[int] = []

            def on_signal(number: int) -> None:
                received.append(number)
                arrived.set()

            with subject.relay.relay([first, second], on_signal):
                subject.deliver(first)
                subject.wait(arrived, "the first relayed signal")
                arrived.clear()
                subject.deliver(second)
                subject.wait(arrived, "the second relayed signal")
            assert received == [first, second]

        self._case(case)

    def test_leaving_the_block_ends_the_relay(self) -> None:
        """The block exits promptly with nothing pending, and can be entered again."""

        def case(subject: RelayUnderTest) -> None:
            first = subject.numbers[0]
            for _ in range(2):
                arrived = subject.threads.event()
                with subject.relay.relay([first], lambda _number, event=arrived: event.set()):
                    subject.deliver(first)
                    subject.wait(arrived, "a relayed signal")

        self._case(case)


@dataclass(frozen=True)
class MainThreadWakeUnderTest:
    """One wake implementation with the way its sent wake is taken."""

    wake: MainThreadWake
    answer: Callable[[], object]
    """Let the armed thread take a sent wake (the real one takes it unprompted)."""
    threads: Threads
    """Starts the sending thread and provides the events the cases wait on."""
    run: Callable[[Callable[[], None]], None]
    """Run a case body (in a child process when a wake changes process-wide state)."""
    wait: Callable[[Event, str], None]
    """Wait for an event the wake sets, failing with the message if it never is (a hang guard)."""


class MainThreadWakeContract:
    """Cases for :class:`~vs_sim.signal_relay.MainThreadWake`."""

    def subject(self) -> MainThreadWakeUnderTest:
        """A fresh wake with its harness."""
        raise NotImplementedError

    def _case(self, case: Callable[[MainThreadWakeUnderTest], None]) -> None:
        self.subject().run(lambda: case(self.subject()))

    def test_a_wake_from_another_thread_runs_the_callback_on_the_armed_thread(self) -> None:
        """The callback runs on the thread that armed the wake, not on the sender."""

        def case(subject: MainThreadWakeUnderTest) -> None:
            woken = subject.threads.event()
            on: list[int] = []

            def on_wake() -> None:
                on.append(threading.get_ident())
                woken.set()

            with subject.wake.armed(on_wake) as send:
                sender = subject.threads.spawn(send, name="wake-sender")
                sender.join(HANG_GUARD_S)
                subject.answer()
                subject.wait(woken, "the main thread taking the wake")
            assert on == [threading.get_ident()]

        self._case(case)

    def test_the_wake_can_be_armed_again_after_its_block(self) -> None:
        """Leaving the block ends that arming; a new block wakes as the first did."""

        def case(subject: MainThreadWakeUnderTest) -> None:
            for _ in range(2):
                woken = subject.threads.event()
                with subject.wake.armed(woken.set) as send:
                    send()
                    subject.answer()
                    subject.wait(woken, "the wake")

        self._case(case)
