"""The ``CommandRunner.execute`` contract: cases every implementation must pass.

The contract (also documented in :mod:`vs_sandbox.command_execution`):

* An empty command is rejected without running: exit code 1, the reason on
  ``stderr``. A non-positive timeout raises ``ValueError``.
* ``output == stdout + stderr``, and ``len(output)`` never exceeds the cap.
  Past the cap ``truncated`` is true and the output carries the truncation
  marker.
* A normal exit reports the command's status; a signalled command reports
  ``128 + N``, never a negative number.
* A timeout stops the command's whole process tree, keeps the output written
  before the stop, ends ``stderr`` with the timeout notice, and reports exit
  code 124 (GNU ``timeout``'s status; the benchmark failure classification
  reads it as a stage timeout, the candidate exceeding its budget).
* A cancel stops the whole process tree the same way, keeps the partial
  output, and returns ``cancelled=True``.

A suite holds one implementation to the contract by subclassing
:class:`CommandRunnerContract` and providing a ``harness`` fixture. The
implementations that spawn real processes (``LocalShellRunner``, ``DockerSandbox``
over ``FakeDockerEngine`` and over a real daemon) run it from
``tests/e2e/test_command_runner_contract_real.py``, the real-system tier; the
pure ``FakeCommandRunner`` runs it on simulated threads from
``libs/vs-sandbox/tests/test_sandbox_contract.py``. A ``Harness`` turns a
behavior ("print this, then exit 7", "hang with a child") into a command for its
sandbox; the cases assert only the contract.

No case depends on a wall-clock race. A cancel is sent only after the command
reports it is running, and the process tree is checked through an end-of-file
that every one of the command's processes must have caused. The only elapsed
time is the two-second timeout of the timeout case, whose command can never
finish before it (a simulated implementation spends no wall time on it).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import pytest

from vs_sandbox.api import CommandResult, CommandRunner
from vs_sim.api.testing import HANG_GUARD_S

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api import Event, Threads

CAP = 200
TIMEOUT_SECONDS = 2
PARTIAL_STDOUT = "partial-out"
PARTIAL_STDERR = "partial-err"
_TRUNCATION_MARKER = "...[truncated]..."
_SIGKILL_STATUS = 137
_SIGTERM_STATUS = 143
_NONZERO = 7


class Harness(Protocol):
    """How one implementation runs the behaviors the contract probes."""

    @property
    def sandbox(self) -> CommandRunner:
        """Return the sandbox under test."""
        ...

    @property
    def threads(self) -> Threads:
        """Return the threads the case's concurrent ``execute`` calls and cancel events use."""
        ...

    def run[T](self, program: Callable[[], T]) -> T:
        """Run *program* as the case's first thread on :attr:`threads` and return its result."""
        ...

    def emit(self, stdout: str, stderr: str, exit_code: int) -> str:
        """Return a command that prints *stdout*, *stderr*, then exits."""
        ...

    def killed_by(self, signal_number: int) -> str:
        """Return a command whose own process dies of *signal_number*."""
        ...

    def hang(self, *, announce: bool) -> str:
        """Return a command that prints partial output, starts a child, and blocks.

        With *announce*, the command reports it is running before it blocks.
        """
        ...

    def wait_until_running(self) -> None:
        """Block until an announcing hang command is running."""
        ...

    def release_waiter(self) -> None:
        """Unblock :meth:`wait_until_running` if the command can no longer announce."""
        ...

    def processes_gone(self) -> bool:
        """Report whether every process the hang command started has exited."""
        ...


class _Execution:
    """One ``execute`` call running on its own thread."""

    def __init__(self, harness: Harness, command: str, *, timeout: int, cancel: Event) -> None:
        self._result: CommandResult | None = None
        self._error: BaseException | None = None

        def run() -> None:
            try:
                self._result = harness.sandbox.execute(command, timeout=timeout, cancel=cancel)
            except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-731014 [BLE001]; the case re-raises whatever the worker raised.
                self._error = error
            finally:
                # A command that ended without announcing must not leave the
                # case blocked waiting for the announcement.
                harness.release_waiter()

        self._worker = harness.threads.spawn(run, name="execute")

    def result(self) -> CommandResult:
        self._worker.join(HANG_GUARD_S)
        assert not self._worker.is_alive(), f"{self._worker.name} did not end"
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _assert_streams_compose_output(result: CommandResult) -> None:
    assert result.output == result.stdout + result.stderr


class CommandRunnerContract:
    """Cases for ``CommandRunner.execute``; a subclass supplies the ``harness`` fixture."""

    def test_id_is_a_nonempty_stable_string(self, harness: Harness) -> None:
        assert isinstance(harness.sandbox.id, str)
        assert harness.sandbox.id
        assert harness.sandbox.id == harness.sandbox.id

    def test_streams_and_status_of_a_normal_exit_are_kept_apart(self, harness: Harness) -> None:
        result = harness.sandbox.execute(harness.emit("hello\n", "warn\n", 0))

        assert isinstance(result, CommandResult)
        assert (result.exit_code, result.stdout, result.stderr) == (0, "hello\n", "warn\n")
        assert not result.truncated
        assert not result.cancelled
        _assert_streams_compose_output(result)

    def test_a_nonzero_status_is_reported_unchanged(self, harness: Harness) -> None:
        result = harness.sandbox.execute(harness.emit("out", "err", _NONZERO))

        assert result.exit_code == _NONZERO
        assert (result.stdout, result.stderr) == ("out", "err")
        _assert_streams_compose_output(result)

    @pytest.mark.parametrize(
        ("signal_number", "status"), [(9, _SIGKILL_STATUS), (15, _SIGTERM_STATUS)]
    )
    def test_a_signalled_command_reports_128_plus_the_signal(
        self, harness: Harness, signal_number: int, status: int
    ) -> None:
        result = harness.sandbox.execute(harness.killed_by(signal_number))

        assert result.exit_code == status
        assert not result.cancelled
        _assert_streams_compose_output(result)

    def test_an_empty_command_is_rejected_with_exit_code_one(self, harness: Harness) -> None:
        result = harness.sandbox.execute("")

        assert result.exit_code == 1
        assert result.stdout == ""
        assert result.stderr
        assert not result.cancelled
        _assert_streams_compose_output(result)

    @pytest.mark.parametrize("timeout", [0, -1])
    def test_a_non_positive_timeout_is_rejected(self, harness: Harness, timeout: int) -> None:
        with pytest.raises(ValueError, match="timeout must be positive"):
            harness.sandbox.execute(harness.emit("x", "", 0), timeout=timeout)

    def test_output_within_the_cap_is_not_truncated(self, harness: Harness) -> None:
        result = harness.sandbox.execute(harness.emit("a" * (CAP // 2), "b" * (CAP // 2), 0))

        assert not result.truncated
        assert (len(result.stdout), len(result.stderr)) == (CAP // 2, CAP // 2)
        _assert_streams_compose_output(result)

    def test_output_past_the_cap_is_cut_marked_and_flagged(self, harness: Harness) -> None:
        result = harness.sandbox.execute(harness.emit("a" * (CAP * 3), "", 0))

        assert result.truncated
        assert len(result.output) <= CAP
        assert _TRUNCATION_MARKER in result.output
        assert result.stdout.startswith("a" * 10)
        _assert_streams_compose_output(result)

    def test_a_failed_command_keeps_the_end_of_its_diagnostics_past_the_cap(
        self, harness: Harness
    ) -> None:
        diagnostics = "".join(f"line {number}\n" for number in range(CAP))
        result = harness.sandbox.execute(harness.emit("", diagnostics, 1))

        assert result.truncated
        assert len(result.output) <= CAP
        assert result.stderr.endswith(diagnostics[-20:])
        _assert_streams_compose_output(result)

    def test_an_unset_cancel_event_leaves_the_command_alone(self, harness: Harness) -> None:
        result = harness.sandbox.execute(
            harness.emit("done", "", 0), cancel=harness.threads.event()
        )

        assert result.exit_code == 0
        assert not result.cancelled
        assert result.stdout == "done"

    def test_a_timeout_stops_the_process_tree_and_keeps_partial_output(
        self, harness: Harness
    ) -> None:
        result = harness.sandbox.execute(harness.hang(announce=False), timeout=TIMEOUT_SECONDS)

        assert result.exit_code == 124
        assert not result.cancelled
        assert result.stdout == PARTIAL_STDOUT
        assert result.stderr.startswith(PARTIAL_STDERR)
        assert result.stderr.endswith(f"timed out after {TIMEOUT_SECONDS} seconds.\n")
        _assert_streams_compose_output(result)
        assert harness.processes_gone()

    def test_a_cancel_stops_the_process_tree_and_keeps_partial_output(
        self, harness: Harness
    ) -> None:
        def program() -> CommandResult:
            cancel = harness.threads.event()
            execution = _Execution(
                harness, harness.hang(announce=True), timeout=3600, cancel=cancel
            )
            harness.wait_until_running()
            cancel.set()
            return execution.result()

        result = harness.run(program)

        assert result.cancelled
        assert isinstance(result.exit_code, int)
        assert result.stdout == PARTIAL_STDOUT
        assert result.stderr.startswith(PARTIAL_STDERR)
        _assert_streams_compose_output(result)
        assert harness.processes_gone()

    def test_a_cancel_set_before_the_call_cancels_it(self, harness: Harness) -> None:
        cancel = harness.threads.event()
        cancel.set()

        result = harness.sandbox.execute(harness.hang(announce=False), timeout=3600, cancel=cancel)

        assert result.cancelled
        _assert_streams_compose_output(result)
        assert harness.processes_gone()
