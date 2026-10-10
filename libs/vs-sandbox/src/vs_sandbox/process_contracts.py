"""Contract suite for :class:`~vs_sandbox.process_execution.StoppableProcess`.

Subclass :class:`StoppableProcessContract`, name the subclass ``Test<Variant>`` and
implement :meth:`~StoppableProcessContract.harness`. The real ``PopenProcess`` and the
simulated ``FakeStoppableProcess`` pass the same cases. Cases assert outcomes and signal
effects, never durations; every wait that does not synchronize on the process is bounded
by a hang guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vs_sandbox.process_execution import KILL, TERMINATE

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sandbox.process_execution import StoppableProcess

_GUARD_SECONDS = 60.0
_STILL_RUNNING_PROBE_SECONDS = 0.05


@dataclass(frozen=True)
class ProcessHarness:
    """How one implementation starts the processes the contract describes."""

    run: Callable[[Callable[[], Any]], Any]
    """Run a case body (a simulated main thread, or the body itself) and return its result."""
    exits_with: Callable[[str, str, int], StoppableProcess]
    """A process that writes *stdout* and *stderr* and exits with the status."""
    runs_until_signalled: Callable[[], StoppableProcess]
    """A process that works until a signal ends it, with the default ``SIGTERM`` action."""
    ignores_term: Callable[[], StoppableProcess]
    """A running process whose ``SIGTERM`` is already ignored when this returns."""


class StoppableProcessContract:
    """Cases for :class:`~vs_sandbox.process_execution.StoppableProcess`."""

    def harness(self) -> ProcessHarness:
        """A fresh harness for one case."""
        raise NotImplementedError

    def test_an_exit_reports_its_output_and_status(self) -> None:
        """Waiting for a process that exits returns its streams, then its status."""
        harness = self.harness()

        def case() -> None:
            process = harness.exits_with("out", "err", 7)
            assert process.wait(_GUARD_SECONDS) == ("out", "err")
            assert process.returncode == 7

        harness.run(case)

    def test_a_wait_that_times_out_reports_none_and_can_be_repeated(self) -> None:
        """A wait on a running process returns ``None``; waiting again after the exit succeeds."""
        harness = self.harness()

        def case() -> None:
            process = harness.runs_until_signalled()
            assert process.wait(_STILL_RUNNING_PROBE_SECONDS) is None
            process.signal_group(KILL)
            assert process.wait(_GUARD_SECONDS) is not None

        harness.run(case)

    def test_sigterm_ends_a_default_process_with_a_negative_status(self) -> None:
        """``SIGTERM`` ends a process that does not handle it; the status is ``-SIGTERM``."""
        harness = self.harness()

        def case() -> None:
            process = harness.runs_until_signalled()
            process.signal_group(TERMINATE)
            assert process.wait(_GUARD_SECONDS) is not None
            assert process.returncode == -TERMINATE

        harness.run(case)

    def test_sigkill_ends_a_process_that_ignores_sigterm(self) -> None:
        """``SIGTERM`` does not end it; ``SIGKILL`` does, with status ``-SIGKILL``."""
        harness = self.harness()

        def case() -> None:
            process = harness.ignores_term()
            process.signal_group(TERMINATE)
            assert process.wait(_STILL_RUNNING_PROBE_SECONDS) is None
            process.signal_group(KILL)
            assert process.wait(_GUARD_SECONDS) is not None
            assert process.returncode == -KILL

        harness.run(case)

    def test_a_signal_after_the_exit_changes_nothing(self) -> None:
        """Signalling a finished process is harmless and keeps its status."""
        harness = self.harness()

        def case() -> None:
            process = harness.exits_with("", "", 3)
            assert process.wait(_GUARD_SECONDS) is not None
            process.signal_group(KILL)
            assert process.wait(None) == ("", "")
            assert process.returncode == 3

        harness.run(case)
