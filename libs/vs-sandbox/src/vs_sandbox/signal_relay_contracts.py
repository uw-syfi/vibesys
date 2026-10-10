"""Contract suite for :class:`~vs_sandbox.signal_relay.SignalRelay`.

Subclass :class:`SignalRelayContract`, name the subclass ``Test<Variant>`` and implement
:meth:`~SignalRelayContract.subject`. The real ``WakeupFdSignalRelay`` (in ``tests/e2e``,
in a child process: delivery changes process-wide state) and ``FakeSignalRelay`` pass the
same cases. Cases wait on what they observe, never on a duration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sandbox.signal_relay import SignalRelay
    from vs_sim.api import Event, Threads


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
    """Cases for :class:`~vs_sandbox.signal_relay.SignalRelay`."""

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
